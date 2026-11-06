"""Проверочный controller 7.3 с pipe RPC к отдельному migration broker.

Обычного deploy CLI нет. Production paths и внешние операции child закрыты
audit guard. Реальный Linux запуск требует отдельно проверенных CI/image и
snapshot receipts; модуль не получает GitHub tokens и не проверяет CI через сеть.
"""

from contextlib import closing, contextmanager, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid


CANDIDATE = "cabc0e24d28947123868db8a60f6b55095feb63e"
PREVIOUS = "dd71c2ba42a0744b5b654cedcf73009ce8879058"
CI_RUN = 42249037867
SOURCE_DIGEST = "5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c"
PRIVATE_PARENT = Path("/var/lib/compadre-assistant-stage7-20261106")
# Ограничение одной строки IPC не допускает передачи DB rows/логов вместо контракта.
MAX_LINE = 8192
# Точные байты native receipts после независимого review; config их не задаёт.
NATIVE_RECEIPTS = {
    "ci-receipt.json": "914166c01b089cab426d5f2705b4040c47d334deecc1779b629892f6640fd72d",
    "image-receipt.json": "42c8fffe8b4753545dfd65244c5100626570661a07b346a9bd6cbcdb97727d4e",
    "synthetic-receipt.json": "5d0545e18b316635ee558472ee0258beec12a7196821a1575960e1595b5cf69b",
}


def validate_native_receipts(root, config):
    """Связывает реальный config с reviewed native receipts, а не с флагом CLI."""
    receipts = {}
    for name, expected in NATIVE_RECEIPTS.items():
        path = root / name
        if path.resolve() != path or not path.is_file() or path.stat().st_nlink != 1 or digest(path) != expected:
            raise ValueError("HARNESS_NATIVE_RECEIPT_BYTES")
        receipts[name] = json.loads(path.read_text(encoding="utf-8"))
    ci, image, synthetic = (receipts[name] for name in NATIVE_RECEIPTS)
    fields = ("candidate_sha", "previous_sha", "run_id", "run_attempt", "full_ci_success",
              "schema_fingerprint", "snapshot_digest", "policy_sha", "artifact_digest", "artifact_id")
    if (any(config[key] != ci[key] for key in fields) or not ci["linux_native_verifier"]
            or image["candidate_sha"] != config["candidate_sha"] or image["oci_revision"] != CANDIDATE
            or image["image_id"] != config["image_id"] or synthetic["stage"] != "success"
            or synthetic["real_flock"] is not True or synthetic["real_polling"] is not False
            or config["previous_image_id"] != "sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8"):
        raise ValueError("HARNESS_NATIVE_RECEIPT_BINDING")


def digest(path):
    """Возвращает SHA256 обычного файла без чтения его содержимого в отчёт."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def receipt_id(config):
    """Связывает запросы с неизменным набором внешних admission metadata."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(config, root):
    """Закрывает harness до полного exact-pair CI и canonical private root.

    Args:
        config: Проверенный оператором receipt без credentials или пользовательских строк.
        root: Новый private каталог сценария, отличный от production.

    Raises:
        ValueError: Receipt неполон, изменился или относится к другой паре.
    """
    required = {"candidate_sha", "previous_sha", "run_id", "run_attempt", "full_ci_success", "repository",
                "synthetic", "source_digest", "image_id", "previous_image_id", "schema_fingerprint", "snapshot_digest",
                "policy_sha", "artifact_digest", "artifact_id", "nonce", "control", "schema_before", "schema_after"}
    if set(config) != required:
        raise ValueError("HARNESS_RECEIPT_KEYS")
    if (root.resolve() != root or not root.is_dir() or config["candidate_sha"] != CANDIDATE
            or config["previous_sha"] != PREVIOUS or config["run_id"] != CI_RUN
            or config["run_attempt"] != 1 or config["full_ci_success"] is not True
            or config["repository"] != "karateka-git/compadre-assistant"):
        raise ValueError("HARNESS_ADMISSION")
    if config["synthetic"]:
        local_temporary = root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        private_synthetic = (root.parent.parent == PRIVATE_PARENT and root.parent.name.startswith("admission-cabc0e2-")
                             and root.name.startswith("synthetic-cabc0e2-"))
        if not local_temporary and not private_synthetic:
            raise ValueError("HARNESS_SYNTHETIC_ROOT")
    elif (root.parent != PRIVATE_PARENT or not root.name.startswith("controller-proof-cabc0e2-")
          or config["source_digest"] != SOURCE_DIGEST or sys.platform != "linux" or os.geteuid() != 0):
        raise ValueError("HARNESS_PRIVATE_ROOT")
    if not config["synthetic"]:
        validate_native_receipts(root, config)
    for name, pattern in (("image_id", r"sha256:[0-9a-f]{64}"), ("previous_image_id", r"sha256:[0-9a-f]{64}"),
                          ("schema_fingerprint", r"[0-9a-f]{64}"), ("source_digest", r"[0-9a-f]{64}"),
                          ("snapshot_digest", r"[0-9a-f]{64}"), ("policy_sha", r"[0-9a-f]{40}"),
                          ("artifact_digest", r"sha256:[0-9a-f]{64}"), ("nonce", r"[0-9a-f]{32}")):
        if re.fullmatch(pattern, str(config[name])) is None:
            raise ValueError("HARNESS_RECEIPT_FIELD")
    if type(config["artifact_id"]) is not int or config["artifact_id"] <= 0:
        raise ValueError("HARNESS_ARTIFACT")
    for key in ("schema_before", "schema_after"):
        if (not isinstance(config[key], list) or not config[key]
                or any(not isinstance(name, str) or re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", name) is None
                       for name in config[key])):
            raise ValueError("HARNESS_MANIFEST")
    if config["schema_after"][:len(config["schema_before"])] != config["schema_before"]:
        raise ValueError("HARNESS_MANIFEST_PREFIX")
    source = root / "fixtures/source.db"
    if (source.resolve() != source or source.stat().st_nlink != 1
            or digest(source) != config["source_digest"] or any(Path(str(source) + s).exists() for s in ("-wal", "-shm", "-journal"))):
        raise ValueError("HARNESS_SOURCE")


def verify_snapshot(root, config):
    """До imports сверяет control с manifest из закреплённого tar стандартной библиотекой."""
    if config["synthetic"]:
        return
    control = root / "control"
    if Path(config["control"]) != control or control.resolve() != control:
        raise ValueError("HARNESS_CONTROL_PATH")
    archive = root / "candidate.tar"
    if digest(archive) != config["snapshot_digest"]:
        raise ValueError("HARNESS_SNAPSHOT_DIGEST")
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if (len(members) > 512 or sum(item.size for item in members) > 16 * 1024 * 1024
                or any(not item.isfile() or item.name.startswith("/") or ".." in Path(item.name).parts
                       for item in members) or len({item.name for item in members}) != len(members)):
            raise ValueError("HARNESS_ARCHIVE")
        manifest = json.loads(source.extractfile("controller-manifest.json").read())
        if manifest["controller_sha"] != CANDIDATE:
            raise ValueError("HARNESS_SNAPSHOT_SHA")
        if {item.name for item in members} != set(manifest["files"]) | {"controller-manifest.json"}:
            raise ValueError("HARNESS_ARCHIVE_MEMBERS")
        for item in members:
            path = control / item.name
            if (path.resolve() != path or not path.is_file() or path.stat().st_nlink != 1
                    or hashlib.sha256(source.extractfile(item).read()).hexdigest() != digest(path)):
                raise ValueError("HARNESS_CONTROL_BYTES")
            if item.name in manifest["files"] and digest(path) != manifest["files"][item.name]["sha256"]:
                raise ValueError("HARNESS_MANIFEST_BYTES")
    if {str(p.relative_to(control)).replace("\\", "/") for p in control.rglob("*") if p.is_file()} != {
            item.name for item in members}:
        raise ValueError("HARNESS_CONTROL_EXTRAS")


def checked_request(root, config, request, sequence):
    """Привязывает единственную migration операцию к case/sequence/копии/image.

    Returns:
        Канонический путь отдельной writable копии.

    Raises:
        ValueError: Команда, receipt, путь или идентичность файла не совпали.
    """
    expected = {"operation", "receipt", "sequence", "path", "device", "inode", "before_digest", "image_id", "candidate_sha"}
    if (set(request) != expected or request["operation"] != "migrate" or request["sequence"] != sequence
            or sequence not in (1, 2) or request["receipt"] != receipt_id(config)
            or request["image_id"] != config["image_id"] or request["candidate_sha"] != CANDIDATE):
        raise ValueError("HARNESS_REQUEST")
    path = Path(request["path"])
    case = root / "scenarios/happy"
    valid = (path == case / "preflight/candidate/app.db") if sequence == 1 else (
        path.parent.parent == case / "checkout/data" and path.parent.name.startswith(".candidate-")
        and re.fullmatch(r"app-[0-9a-f]{32}\.db", path.name))
    if (not valid or path.resolve() != path or not path.is_file() or path.stat().st_nlink != 1
            or (path.stat().st_dev, path.stat().st_ino) != (request["device"], request["inode"])
            or digest(path) != request["before_digest"]):
        raise ValueError("HARNESS_COPY_IDENTITY")
    return path


def serve_request(root, config, request, sequence, migrator):
    """Проверяет RPC и передаёт broker только точный image/SHA и private copy.

    Args:
        root: Канонический корень case.
        config: Reviewed admission receipt.
        request: Одна ограниченная JSON операция, полученная по private pipe.
        sequence: Следующий разрешённый номер 1 либо 2.
        migrator: scripts.stage7_migration.migrate либо явный synthetic callback.

    Returns:
        Receipt завершённой миграции без DB rows или process output.
    """
    validate(config, root)
    verify_snapshot(root, config)
    path = checked_request(root, config, request, sequence)
    identity = (path.stat().st_dev, path.stat().st_ino)
    migrator(root, path, config["image_id"], CANDIDATE)
    if ((path.stat().st_dev, path.stat().st_ino) != identity or path.stat().st_nlink != 1
            or digest(root / "fixtures/source.db") != config["source_digest"]):
        raise ValueError("HARNESS_BROKER_RESULT")
    return {"operation": "migrated", "receipt": receipt_id(config), "sequence": sequence,
            "path": str(path), "image_id": config["image_id"], "candidate_sha": CANDIDATE,
            "device": identity[0], "inode": identity[1], "after_digest": digest(path)}


def child(root):
    """В guarded процессе проходит preflight, final snapshot, switch и model boot."""
    config = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    validate(config, root)
    verify_snapshot(root, config)
    control = Path(config["control"])
    if not config["synthetic"] and control != root / "control":
        raise ValueError("HARNESS_CONTROL_PATH")
    sys.path.insert(0, str(control))
    from scripts.stage7_transport import install_guard, IsolationError
    install_guard(root)
    from scripts import deploy_prod as prod, deploy_prepare as prep, deploy_database_switch as switch
    from scripts import predeploy_compatibility as compatibility, deploy_runtime as runtime
    if config["synthetic"] and os.name != "posix":
        locked = False

        @contextmanager
        def synthetic_lock(*args):
            """Моделирует занятость lock только в локальном Windows synthetic test."""
            nonlocal locked
            if locked:
                raise prep.PreparationError("synthetic lock busy")
            locked = True
            try:
                yield
            finally:
                locked = False

        prep.deploy_lock = synthetic_lock
    import socket
    guard_checks = (lambda: subprocess.run(["docker", "ps"]), lambda: socket.socket(),
                    lambda: (prod.STATE_DIR / "state.json").read_bytes(),
                    lambda: (root.parent / ("escape-" + config["nonce"])).write_bytes(b"forbidden"))
    for action in guard_checks:
        try:
            action()
        except IsolationError:
            pass
        else:
            raise ValueError("HARNESS_GUARD_NOT_INSTALLED")
    if not config["synthetic"]:
        from scripts import controller_snapshot
        manifest = json.loads((control / controller_snapshot.MANIFEST).read_text(encoding="utf-8"))
        if manifest["controller_sha"] != CANDIDATE:
            raise ValueError("HARNESS_SNAPSHOT_SHA")
        controller_snapshot.verify_directory(control, manifest, require_root=True)
        if digest(root / "candidate.tar") != config["snapshot_digest"]:
            raise ValueError("HARNESS_SNAPSHOT_DIGEST")
    case = root / "scenarios/happy"
    checkout, state_dir = case / "checkout", case / "state"
    (checkout / "data").mkdir(parents=True, mode=0o700)
    state_dir.mkdir(mode=0o700)
    sequence = 0

    def migrate(image, directory, *, basename="app.db"):
        nonlocal sequence
        sequence += 1
        path = directory / basename
        request = {"operation": "migrate", "receipt": receipt_id(config), "sequence": sequence,
                   "path": str(path), "device": path.stat().st_dev, "inode": path.stat().st_ino,
                   "before_digest": digest(path), "image_id": image, "candidate_sha": CANDIDATE}
        checked_request(root, config, request, sequence)
        sys.__stdout__.write(json.dumps(request) + "\n")
        sys.__stdout__.flush()
        raw = sys.stdin.readline(MAX_LINE + 1)
        response = json.loads(raw)
        expected = {"operation": "migrated", "receipt": request["receipt"], "sequence": sequence,
                    "path": str(path), "image_id": image, "candidate_sha": CANDIDATE,
                    "device": path.stat().st_dev, "inode": path.stat().st_ino, "after_digest": digest(path)}
        if len(raw) > MAX_LINE or response != expected:
            raise ValueError("HARNESS_RESPONSE")

    class ModelHost(prod.Host):
        """Моделирует только config/create/start; неизвестная команда закрывает case."""

        def __init__(self):
            super().__init__(checkout, state_dir)
            self.commands, self.starts = [], 0
            self.found = self.make_containers("/app/data/app.db", "exited", config["previous_image_id"])
            self.migrate_copy = migrate

        def make_containers(self, path, status, image):
            """Формирует inspect только двух model IDs без credentials."""
            return {name: {"Id": f"stage7-model-{config['nonce']}-{name}", "Image": image,
                    "State": {"Running": status == "running", "Status": status}, "RestartCount": 0,
                    "Config": {"Env": ["DATABASE_PATH=" + path]},
                    "HostConfig": {"RestartPolicy": {"Name": "no"}},
                    "Mounts": [{"Type": "bind", "Source": str(checkout / "data"), "Destination": "/app/data", "RW": True}]}
                    for name in prep.CHANNELS}

        def run(self, *args, env=None):
            """Отвергает любую команду вне точной in-memory модели Docker."""
            self.commands.append(args)
            state = prep.read_state(state_dir, checkout) or {}
            candidate = state.get("candidate", {})
            if args == ("docker", "compose", "config", "--format", "json"):
                if env != self.database_env(candidate["tag"], state["new"]["container_path"]):
                    raise ValueError("HARNESS_CONFIG_ENV")
                return json.dumps({"services": {name: {"image": candidate["tag"], "pull_policy": "never",
                    "restart": "no", "environment": {"DATABASE_PATH": state["new"]["container_path"]}} for name in prep.CHANNELS}})
            if args == ("docker", "compose", "create", "--no-build", "--pull", "never", "--force-recreate", *prep.CHANNELS):
                if self.starts or env != self.database_env(candidate["tag"], state["new"]["container_path"]):
                    raise ValueError("HARNESS_CREATE")
                self.found = self.make_containers(state["new"]["container_path"], "created", config["image_id"])
                return ""
            if args == ("docker", "start", *(self.found[name]["Id"] for name in prep.CHANNELS)):
                if self.starts or state.get("admission_may_have_started") is not True:
                    raise ValueError("HARNESS_START")
                self.starts += 1
                for item in self.found.values():
                    item["State"] = {"Running": True, "Status": "running"}
                return ""
            raise ValueError("HARNESS_UNKNOWN_COMMAND")

        def containers(self):
            """Возвращает только in-memory inspect модели."""
            return self.found

        def image(self, reference):
            """Подтверждает exact model image/pin, не вызывает Docker."""
            for sha, image in ((CANDIDATE, config["image_id"]), (PREVIOUS, config["previous_image_id"])):
                if reference in (image, prep.pinned_tag(sha)):
                    return {"Id": image, "Config": {"Labels": {prep.OCI_REVISION: sha}}}
            raise ValueError("HARNESS_UNKNOWN_IMAGE")

        def unit_state(self, unit):
            """Моделирует остановленные unit только внутри private case."""
            if unit not in (prod.SERVICE, prod.TIMER, prod.BACKUP_SERVICE):
                raise ValueError("HARNESS_UNKNOWN_UNIT")
            return "inactive"

        def unit_enabled(self, unit):
            """Моделирует выключенный private backup timer."""
            if unit != prod.TIMER:
                raise ValueError("HARNESS_UNKNOWN_TIMER")
            return False

        def require_no_duplicate_polling(self):
            """Требует ровно две model записи без настоящих credentials."""
            if set(self.found) != set(prep.CHANNELS):
                raise ValueError("HARNESS_DUPLICATE")

        def require_no_foreign_database_mounts(self, own_ids):
            """Проверяет полный собственный inventory модели без fallback."""
            if own_ids != {item["Id"] for item in self.found.values()}:
                raise ValueError("HARNESS_FOREIGN_WRITER")

        def require_switch_rollout(self):
            """Сверяет external receipt модели; это не effective production units."""
            validate(config, root)

        def activate_unit(self):
            """Вызывает настоящий read-only adoption под занятым private lock."""
            runtime.start(self)

        def verify_both(self, image, sha):
            """Проверяет модель пары; health/polling в этом case не исполняются."""
            if image != config["image_id"] or sha != CANDIDATE or not all(i["State"]["Running"] for i in self.found.values()):
                raise ValueError("HARNESS_PAIR")

        def timer_restore(self, value):
            """Проверяет private timer intent, не вызывает systemd."""
            if value != {"enabled": True, "active": True}:
                raise ValueError("HARNESS_TIMER")

    host = ModelHost()
    before, after = config["schema_before"], config["schema_after"]
    source = root / "fixtures/source.db"
    # Это только initial materialization закрытой fixture, не controller backup.
    # mode=ro SQLite на WAL-mode main создаёт sidecars у source; byte copy
    # сохраняет исходный no-sidecars инвариант. Реальные backups идут ниже.
    validate(config, root)
    old = checkout / "data/app.db"
    with source.open("rb") as original, old.open("xb") as output:
        os.chmod(old, 0o600)
        shutil.copyfileobj(original, output)
        output.flush()
        os.fsync(output.fileno())
    if digest(old) != config["source_digest"]:
        raise ValueError("HARNESS_INITIAL_COPY")
    validate(config, root)
    original_hash = digest(old)
    state = {"old_sha": PREVIOUS, "old_image_id": config["previous_image_id"], "old_tag": prep.pinned_tag(PREVIOUS),
             "schema_before": before, "schema_after": after, "controller_sha": CANDIDATE,
             "candidate": {"sha": CANDIDATE, "image_id": config["image_id"], "tag": prep.pinned_tag(CANDIDATE),
                           "version": "stage7-model", "run_id": CI_RUN, "run_attempt": 1},
             "compatibility_evidence": {key: config[key] for key in ("artifact_id", "artifact_digest", "policy_sha", "schema_fingerprint")},
             "timer_before": {"enabled": True, "active": True}, "last_good": None}

    def record(stage, **changes):
        state.update(changes, stage=stage)
        prep.write_state(state_dir, checkout, state)

    with prep.deploy_lock(state_dir, checkout), redirect_stdout(sys.stderr):
        host.require_switch_rollout()
        preflight = case / "preflight"
        preflight.mkdir(mode=0o700)
        compatibility.verify_copy(old, preflight, before, after, config["schema_fingerprint"],
                                  lambda directory: migrate(config["image_id"], directory))
        # Остановка только модели следует за полным preflight; действующие боты
        # проверяет и останавливает оператор отдельно до реального Linux case.
        record("ready", model_preflight_receipt=receipt_id(config))
        host.require_switch_rollout()
        host.marker(True)
        record("writers_stopped")
        switch.register_initial(host, state, record, prod._atomic_text)
        switch.transition(host, state, record, prod._atomic_text)
        if sequence != 2 or switch.read_selected(host, state) != "new" or host.starts != 1:
            raise ValueError("HARNESS_SWITCH")
        new = Path(state["new"]["host_path"])
        marker = "stage7-" + config["nonce"]
        with closing(sqlite3.connect(new)) as connection:
            connection.execute("INSERT INTO app_users(display_name) VALUES(?)", (marker,))
            connection.commit()
        runtime.finalize(host, state, record, "success")
        count = len(host.commands)
        runtime.start(host)
        runtime.start(host)
        if count != len(host.commands) or host.starts != 1 or digest(old) != original_hash:
            raise ValueError("HARNESS_SECOND_WRITER")
        with closing(sqlite3.connect(new)) as connection:
            if (connection.execute("SELECT count(*) FROM app_users WHERE display_name=?", (marker,)).fetchone() != (1,)
                    or connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                    or connection.execute("PRAGMA foreign_key_check").fetchall()):
                raise ValueError("HARNESS_WRITE_CHECK")
        if digest(source) != config["source_digest"]:
            raise ValueError("HARNESS_SOURCE_CHANGED")
    return {"operation": "complete", "receipt": receipt_id(config), "migrations": sequence,
            "model_starts": host.starts, "stage": state["stage"], "selected": "new", "row_committed": True,
            "source_unchanged": True, "real_polling": False, "real_flock": os.name == "posix",
            "guard_checks": len(guard_checks), "synthetic": config["synthetic"]}


def run_case(root, config, migrator):
    """Запускает guarded child и обслуживает только две migration RPC операции.

    Реальный migrator передаётся reviewed Linux bootstrap отдельно. До его
    вызова обязательны полный CI, image/snapshot evidence и разрешённое окно.
    """
    validate(config, root)
    verify_snapshot(root, config)
    (root / "receipt.json").write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    os.chmod(root / "receipt.json", 0o600)
    (root / "scenarios").mkdir(mode=0o700)
    lines = queue.Queue()
    with (root / "controller.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8", close_fds=True,
            env={"PATH": "/usr/bin:/bin" if os.name == "posix" else os.environ.get("PATH", ""),
                 "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                 "TEMP": tempfile.gettempdir(), "TMP": tempfile.gettempdir()})

        def reader():
            while True:
                line = process.stdout.readline(MAX_LINE + 1)
                lines.put(line)
                if not line or len(line) > MAX_LINE:
                    return

        threading.Thread(target=reader, daemon=True).start()
        sequence = 0
        try:
            while True:
                line = lines.get(timeout=60)
                if not line or len(line) > MAX_LINE:
                    raise ValueError("HARNESS_CHILD_PIPE")
                message = json.loads(line)
                if message.get("operation") == "complete":
                    if (sequence != 2 or message.get("receipt") != receipt_id(config)
                            or message.get("stage") != "success" or message.get("model_starts") != 1
                            or message.get("selected") != "new" or message.get("row_committed") is not True
                            or message.get("source_unchanged") is not True or message.get("real_polling") is not False
                            or message.get("guard_checks") != 4 or message.get("synthetic") != config["synthetic"]
                            or digest(root / "fixtures/source.db") != config["source_digest"]):
                        raise ValueError("HARNESS_COMPLETION")
                    if process.wait(timeout=30) != 0:
                        raise ValueError("HARNESS_CHILD_EXIT")
                    (root / "result.json").write_text(json.dumps(message, sort_keys=True), encoding="utf-8")
                    return message
                sequence += 1
                with (root / "transport.jsonl").open("a", encoding="utf-8") as audit:
                    audit.write(json.dumps({"event": "received", "request": message}, sort_keys=True) + "\n")
                    audit.flush()
                    os.fsync(audit.fileno())
                response = serve_request(root, config, message, sequence, migrator)
                with (root / "transport.jsonl").open("a", encoding="utf-8") as audit:
                    audit.write(json.dumps({"event": "completed", "result": response}, sort_keys=True) + "\n")
                    audit.flush()
                    os.fsync(audit.fileno())
                process.stdin.write(json.dumps(response) + "\n")
                process.stdin.flush()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            process.stdin.close()
            process.stdout.close()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--child":
        raise SystemExit("Только private child; используйте reviewed run_case bootstrap")
    result = child(Path(sys.argv[2]))
    print(json.dumps(result), flush=True)
