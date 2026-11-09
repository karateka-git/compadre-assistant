"""Проверяет v2 recovery на настоящих SQLite/WAL с fake Docker/systemd."""

from contextlib import closing, ExitStack, nullcontext
from dataclasses import asdict, replace
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts.ci_repository_compatibility import ReleaseTagEvidence
from scripts.select_deploy_release import SelectedRelease
from tests.test_deploy_database_switch import SwitchHost, MANIFEST, IMAGE, SHA
from tests.test_predeploy_compatibility import ArtifactAPI, POLICY_SHA


OLD_SHA = "a" * 40
OLD_IMAGE = "sha256:" + "1" * 64
ZERO_TIME = "0001-01-01T00:00:00Z"


def annotated_release(host, schema_fingerprint: str | None = None) -> ArtifactAPI:
    """Готовит локальный Git-тег, reviewed policy и API ответ для изолированного кандидата."""
    api = ArtifactAPI()
    api.policy["previous_sha"] = OLD_SHA
    api.trusted["repository_compatibility_policy.json"] = json.dumps(api.policy).encode()
    prod._atomic_text(host.state_dir / "compatibility-policy.sha", POLICY_SHA)
    if schema_fingerprint is None:
        with closing(sqlite3.connect(host.selected_database())) as db:
            schema_fingerprint = prod.compatibility.schema_fingerprint(db)

    def git(*args):
        return subprocess.check_output(
            ("git", "-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args),
            cwd=host.checkout, text=True, env=os.environ | {
                "GIT_AUTHOR_DATE": "2026-11-04T00:00:00Z", "GIT_COMMITTER_DATE": "2026-11-04T00:00:00Z",
            },
        ).strip()

    git("init", "--quiet", "--object-format=sha1")
    workflows = host.checkout / ".github/workflows"
    workflows.mkdir(parents=True)
    for name in prod.compatibility.WORKFLOWS:
        (workflows / name).write_bytes(api.workflow_bytes)
    git("add", ".github")
    git("commit", "--quiet", "-m", "Fixture")
    selected = host.selected
    evidence = ReleaseTagEvidence(OLD_SHA, POLICY_SHA, selected.run_id, selected.run_attempt, schema_fingerprint)
    git("tag", "-a", "-m", evidence.message(), selected.version)
    host.selected = replace(selected, sha=git("rev-parse", "HEAD"), compatibility=evidence,
                            tag_object_sha=git("rev-parse", f"refs/tags/{selected.version}"))
    return api


class Crash(BaseException):
    """Имитирует прекращение controller без обработки обычного исключения."""


class RecoveryHost(SwitchHost):
    """Подменяет системный транспорт, сохраняя реальные writers guards и pin."""

    def __init__(self, root):
        """Создаёт исходную БД, fake pin, Release и приватные policy файлы."""
        super().__init__(root)
        self.selected = SelectedRelease(prod.EXPECTED_REPOSITORY, "v1.2.3", "release/1.2", SHA,
                                        5, "main-full.yml", 8, 1)
        self.serial = 0
        self.starts = 0
        self.bad_image = False
        for item in self.found.values():
            item["Image"] = OLD_IMAGE
            item["State"]["StartedAt"] = "2026-11-04T00:00:00Z"
        prod._atomic_text(self.state_dir / "compatibility-policy.sha", "c" * 40)
        prod._atomic_text(self.state_dir / "github-read-token", "synthetic-test-token")

    def image(self, reference):
        """Возвращает проверяемые OCI revision обоих локальных образов."""
        old = reference in (OLD_IMAGE, prep.pinned_tag(OLD_SHA))
        return {"Id": OLD_IMAGE if old else IMAGE, "Config": {"Labels": {
            prep.OCI_REVISION: "f" * 40 if self.bad_image else OLD_SHA if old else self.selected.sha}}}

    def select(self, version):
        """Возвращает synthetic immutable Release для проверки точной записи."""
        return self.selected

    def restore_checkout(self, sha):
        """Фиксирует возврат checkout без действий над репозиторием тестов."""
        self.commands.append(("restore", sha))

    def prepare_runtime_schema(self, state, record):
        """Подменяет проверку Git-SQL; доставка допуска проверяется отдельным тестом."""
        record("recovery_selected", runtime_schema=None)

    def timer_suspend(self):
        """Фиксирует запрет synthetic backup timer."""
        self.commands.append(("timer_suspend",))

    def verify_both(self, expected_id, expected_sha, **kwargs):
        """Проверяет оба fake runtime после действительного вызова start."""
        if any(item["Image"] != expected_id or not item["State"]["Running"] for item in self.found.values()):
            raise AssertionError("wrong runtime")
        if self.image(expected_id)["Config"]["Labels"][prep.OCI_REVISION] != expected_sha:
            raise AssertionError("wrong revision")

    def run(self, *args, env=None):
        """Эмулирует транспорт guard/create/start с уникальными ID каждого create."""
        self.commands.append(args)
        if args[:2] == ("systemctl", "stop"):
            return ""
        if args[:3] == ("docker", "ps", "--all"):
            channel = args[-1].split("=", 2)[-1]
            return self.found.get(channel, {}).get("Id", "")
        if args[:2] == ("docker", "inspect"):
            return json.dumps(next(item for item in self.found.values() if item["Id"] == args[2]))
        if args[:2] in (("docker", "stop"), ("docker", "update")):
            for item in self.found.values():
                if item["Id"] == args[-1]:
                    if args[1] == "update":
                        item["HostConfig"]["RestartPolicy"]["Name"] = "no"
                    else:
                        item["State"]["Running"] = False
                        if item["State"]["Status"] != "created":
                            item["State"]["Status"] = "exited"
            return ""
        if args[:3] == ("docker", "compose", "create"):
            if any(item["State"]["Running"] for item in self.found.values()):
                raise AssertionError("create with live writer")
            self.serial += 1
            self.found = self._containers(env["ASSISTANT_DATABASE_PATH"], "created")
            for name, item in self.found.items():
                item["Id"] = f"{name}-{self.serial}"
                item["Image"] = self.image(env["ASSISTANT_IMAGE"])["Id"]
                item["State"]["StartedAt"] = ZERO_TIME
            return ""
        if args[:2] == ("docker", "start"):
            state = prep.read_state(self.state_dir, self.checkout)
            if state.get("stage") != "recovery_prepared" and state.get("admission_may_have_started") is not True:
                raise AssertionError("start without durable permission")
            if any(item["State"]["Running"] for item in self.found.values()):
                raise AssertionError("double polling")
            if set(args[2:]) != {item["Id"] for item in self.found.values()}:
                raise AssertionError("wrong IDs")
            self.starts += 1
            for item in self.found.values():
                item["State"].update(Running=True, Status="running", StartedAt="2026-11-04T01:00:00Z")
            return ""
        return super().run(*args, env=env)


class DatabaseRecoveryTests(unittest.TestCase):
    """Проверяет границу, повтор, corruption и независимую проверку допуска."""

    def setUp(self):
        """Подменяет POSIX owner/mode check, сохраняя чтение настоящих файлов."""
        mocked = patch.object(prod.compatibility, "private_text", side_effect=lambda path: path.read_text().strip())
        mocked.start()
        self.addCleanup(mocked.stop)

    def _setup(self, root, stage="database_selected"):
        host = RecoveryHost(root)
        old = switch.identity(host.checkout / "data/app.db", "legacy", MANIFEST)
        evidence = {"artifact_id": 10, "artifact_digest": "sha256:" + "d" * 64,
                    "policy_sha": "c" * 40, "schema_fingerprint": old["fingerprint"]}
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST,
                 "controller_sha": "c" * 40, "old_sha": OLD_SHA,
                 "old_image_id": OLD_IMAGE, "old_tag": prep.pinned_tag(OLD_SHA),
                 "candidate": asdict(host.selected) | {"image_id": IMAGE, "tag": prep.pinned_tag(SHA)},
                 "compatibility_evidence": evidence}

        def record(name, **changes):
            state.update(changes, stage=name)
            prep.write_state(host.state_dir, host.checkout, state)

        switch.register_initial(host, state, record, prod._atomic_text)
        if stage != "writers_stopped":
            def stop(name, **changes):
                record(name, **changes)
                if name == stage:
                    raise Crash()
            with self.assertRaises(Crash):
                switch.transition(host, state, stop, prod._atomic_text)
        return host, state, record

    def _recover(self, host, state):
        with patch.object(prod.compatibility, "verify_evidence", return_value=state["compatibility_evidence"]) as verify:
            prod._recover(host, state)
        return verify

    def test_before_admission_recovers_old_at_every_prestart_stage(self):
        """Все pre-admission стадии возвращают old, сохраняя файлы и false."""
        for stage in ("writers_stopped", "snapshot_verified", "candidate_copy_verified", "switch_intent",
                      "database_selected", "runtime_prepared"):
            with self.subTest(stage=stage), TemporaryDirectory() as temporary:
                host, state, _ = self._setup(Path(temporary), stage)
                original = Path(state["old"]["host_path"]).read_bytes()
                verify = self._recover(host, state)
                self.assertEqual(switch.read_selected(host, state), "old")
                self.assertIs(state["admission_may_have_started"], False)
                self.assertEqual(Path(state["old"]["host_path"]).read_bytes(), original)
                self.assertEqual(state["stage"], "recovery_verified")
                self.assertTrue((host.state_dir / "deploy-in-progress").exists())
                verify.assert_not_called()
                if "new" in state:
                    self.assertTrue(Path(state["new"]["host_path"]).exists())
                self._recover(host, state)
                self.assertEqual(host.starts, 2)
                self.assertEqual(len(host.found), 2)

    def test_after_admission_preserves_wal_rows_and_rechecks_exact_evidence(self):
        """Записи нового WAL переживают recovery/repeat; artifact проверяется дважды."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary), "candidate_started")
            path = Path(state["new"]["host_path"])
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("INSERT INTO messages VALUES(2, 'accepted-after-admission')")
                db.commit()
                self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
                wal = Path(str(path) + "-wal").read_bytes()
                for _ in range(2):
                    verify = self._recover(host, state)
                    self.assertEqual(verify.call_count, 2)
                    for call in verify.call_args_list:
                        self.assertEqual(call.args[1:], (host.selected, "c" * 40, OLD_SHA))
                    self.assertEqual(switch.read_selected(host, state), "new")
                    self.assertTrue(state["admission_may_have_started"])
                    self.assertEqual(Path(str(path) + "-wal").read_bytes(), wal)
                    self.assertEqual(db.execute("SELECT text FROM messages ORDER BY id").fetchall(),
                                     [("original",), ("accepted-after-admission",)])
                self.assertEqual(host.starts, 3)

    def test_annotated_deploy_and_automatic_recovery_boundaries(self):
        """Проверяет выбор поколения при успехе и четырёх отказах через deploy-prod."""
        for fault in (None, "before_migration", "before_admission", "after_admission", "evidence_mismatch"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                self._assert_annotated_deploy(Path(temporary), fault)

    def _assert_annotated_deploy(self, root, fault):
        host = RecoveryHost(root)
        host.marker(False)
        old_database = host.selected_database()
        old_bytes = old_database.read_bytes()
        migration = "0002_recovery_fixture.sql"
        expected = [*MANIFEST, migration]
        for item in host.found.values():
            item["State"].update(Running=True, Status="running")
            item["Config"]["Labels"] = {prep.OCI_REVISION: OLD_SHA}
        old_tag = prep.pinned_tag(OLD_SHA)
        host.set_runtime(old_tag, OLD_IMAGE)
        prep.write_state(host.state_dir, host.checkout, {"stage": "success", "last_good": {
            "version": "v1.2.2", "sha": OLD_SHA, "image_id": OLD_IMAGE, "tag": old_tag}})

        def migrate(db):
            db.executescript("CREATE INDEX messages_text ON messages(text);"
                             f"INSERT INTO schema_migrations VALUES(2, '{migration}');")

        with closing(sqlite3.connect(old_database)) as source, closing(sqlite3.connect(":memory:")) as copy_db:
            source.backup(copy_db)
            migrate(copy_db)
            fingerprint = prod.compatibility.schema_fingerprint(copy_db)
        api = annotated_release(host, fingerprint)
        original_run = host.run
        original_prepare = host.prepare_database_containers
        original_verify = host.verify_both
        migrated = []
        committed_wal = None

        def run(*args, env=None):
            if args == ("git", "rev-parse", "HEAD"):
                return OLD_SHA
            if args[:3] == ("docker", "compose", "stop"):
                host.commands.append(args)
                for item in host.found.values():
                    item["State"].update(Running=False, Status="exited")
                return ""
            return original_run(*args, env=env)

        def migrate_copy(image, directory, *, basename="app.db"):
            self.assertEqual(image, IMAGE)
            path = directory / basename
            self.assertNotEqual(path, old_database)
            with closing(sqlite3.connect(path)) as db:
                migrate(db)
            migrated.append(path)

        def prepare_runtime(*args, recovery=False):
            ids = original_prepare(*args, recovery=recovery)
            if not recovery and fault == "before_admission":
                raise prod.DeployError("synthetic create failure")
            return ids

        def verify_runtime(image, sha, **kwargs):
            nonlocal committed_wal
            original_verify(image, sha, **kwargs)
            if image == IMAGE and fault in ("after_admission", "evidence_mismatch"):
                db = stack.enter_context(closing(sqlite3.connect(host.selected_database())))
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("INSERT INTO messages VALUES(2, 'accepted-after-admission')")
                db.commit()
                wal = Path(str(host.selected_database()) + "-wal")
                committed_wal = wal.read_bytes()
                self.assertTrue(committed_wal)
                raise prod.DeployError("synthetic health failure")

        original_get = api.get

        def get(path, **kwargs):
            if fault == "evidence_mismatch":
                durable = prep.read_state(host.state_dir, host.checkout)
                if durable["stage"] == "recovery_prepared":
                    api.workflow_bytes = b"different reviewed workflow"
            elif fault == "before_migration":
                api.workflow_bytes = b"different reviewed workflow"
            return original_get(path, **kwargs)

        with ExitStack() as stack:
            for name in ("require_switch_rollout", "installed", "fetch", "require_compose_pin",
                         "activate_unit", "timer_restore"):
                stack.enter_context(patch.object(host, name))
            for name in ("check_candidate_image", "require_clean_checkout", "require_disk_space"):
                stack.enter_context(patch.object(prep, name))
            stack.enter_context(patch.object(host, "uses_selector_runtime", return_value=True))
            stack.enter_context(patch.object(host, "timer_snapshot", return_value={"enabled": False, "active": False}))
            stack.enter_context(patch.object(host, "run", side_effect=run))
            stack.enter_context(patch.object(host, "migrate_copy", side_effect=migrate_copy))
            stack.enter_context(patch.object(host, "prepare_database_containers", side_effect=prepare_runtime))
            stack.enter_context(patch.object(host, "verify_both", side_effect=verify_runtime))
            stack.enter_context(patch.object(prep, "deploy_lock", return_value=nullcontext()))
            stack.enter_context(patch.object(prep, "build_pinned_image",
                                             return_value=(prep.pinned_tag(host.selected.sha), IMAGE)))
            stack.enter_context(patch.object(prod, "_schema_names", return_value=expected))
            stack.enter_context(patch.object(prod, "GitHub", return_value=api))
            stack.enter_context(patch.object(prod.compatibility, "verify_evidence", side_effect=AssertionError("legacy")))
            requests = stack.enter_context(patch.object(api, "get", side_effect=get))
            if fault is None:
                self.assertEqual(prod.deploy(host, host.selected.version), host.selected.sha)
            else:
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, host.selected.version)

            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(old_database.read_bytes(), old_bytes)
            if fault == "before_migration":
                self.assertEqual(durable["stage"], "failed_preflight")
                self.assertEqual(durable["error"], "SAFE_WORKFLOW_BYTES")
                self.assertEqual(host.selected_database(), old_database)
                self.assertEqual(durable["last_good"]["sha"], OLD_SHA)
                self.assertEqual(prod._current_image(host), OLD_IMAGE)
                self.assertEqual(migrated, [])
                self.assertFalse((host.state_dir / "active-db.json").exists())
                self.assertFalse(list(host.state_dir.glob("final-*.db")))
                self.assertFalse((host.state_dir / "deploy-in-progress").exists())
                return

            self.assertEqual(durable["candidate"]["sha"], host.selected.sha)
            self.assertEqual(durable["compatibility_evidence"]["tag_object_sha"], host.selected.tag_object_sha)
            self.assertEqual(len(migrated), 2)
            self.assertNotEqual(migrated[0], migrated[1])
            snapshot = Path(durable["snapshot"]["path"])
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), durable["snapshot"]["sha256"])
            prep.validate_schema(snapshot, MANIFEST)
            with closing(sqlite3.connect(snapshot)) as db:
                self.assertEqual(db.execute("SELECT text FROM messages").fetchall(), [("original",)])
            admitted = fault != "before_admission"
            self.assertIs(durable["admission_may_have_started"], admitted)
            self.assertEqual(switch.read_selected(host, durable), "new" if admitted else "old")
            self.assertEqual(durable["new"]["fingerprint"], fingerprint)
            selected_database = host.selected_database()
            prep.validate_schema(selected_database, expected if admitted else MANIFEST)
            with closing(sqlite3.connect(selected_database)) as db:
                rows = [("original",)]
                if fault in ("after_admission", "evidence_mismatch"):
                    rows.append(("accepted-after-admission",))
                self.assertEqual(db.execute("SELECT text FROM messages ORDER BY id").fetchall(), rows)
            if committed_wal is not None:
                self.assertEqual(Path(str(selected_database) + "-wal").read_bytes(), committed_wal)

            self.assertEqual(durable["last_good"]["sha"], host.selected.sha if fault is None else OLD_SHA)
            if fault is None:
                self.assertEqual(durable["last_good"]["backup_id"], str(snapshot))
            if fault == "evidence_mismatch":
                self.assertEqual(durable["stage"], "degraded")
                self.assertEqual(host.starts, 1)
                self.assertTrue((host.state_dir / "deploy-in-progress").exists())
                self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                self.assertEqual(requests.call_count, 10)
            else:
                self.assertEqual(durable["stage"], "success" if fault is None else "rolled_back")
                self.assertEqual(durable["boot"]["sha"], durable["last_good"]["sha"])
                self.assertEqual(prod._current_image(host), IMAGE if fault is None else OLD_IMAGE)
                self.assertEqual(host.starts, 2 if fault == "after_admission" else 1)
                self.assertFalse((host.state_dir / "deploy-in-progress").exists())
                self.assertEqual(requests.call_count, 12 if fault == "after_admission" else 4)

    def test_recovery_crash_boundaries_converge(self):
        """Повтор после durable recovery stage и start не создаёт второй polling."""
        for admitted in (False, True):
            for boundary in ("recovery_intent", "selector_before", "selector_after", "recovery_selected",
                             "create_after", "recovery_prepared", "start_after", "recovery_verified"):
                if admitted and boundary.startswith("selector"):
                    continue
                with self.subTest(admitted=admitted, boundary=boundary), TemporaryDirectory() as temporary:
                    host, state, record = self._setup(Path(temporary),
                        "admission_recorded" if admitted else "database_selected")
                    host.guard_known_writers()

                    def interrupted(name, **changes):
                        record(name, **changes)
                        if name == boundary:
                            raise Crash()

                    def publish(path, value):
                        if boundary == "selector_before":
                            raise Crash()
                        prod._atomic_text(path, value)
                        if boundary == "selector_after":
                            raise Crash()

                    original = host.run

                    def run(*args, env=None):
                        value = original(*args, env=env)
                        if ((boundary == "create_after" and args[:3] == ("docker", "compose", "create"))
                                or (boundary == "start_after" and args[:2] == ("docker", "start"))):
                            raise Crash()
                        return value

                    with patch.object(prod.compatibility, "verify_evidence", return_value=state["compatibility_evidence"]), \
                            patch.object(host, "run", side_effect=run), self.assertRaises(Crash):
                        switch.recover(host, state, interrupted, publish)
                    durable = prep.read_state(host.state_dir, host.checkout)
                    self._recover(host, durable)
                    self.assertEqual(switch.read_selected(host, durable), "new" if admitted else "old")
                    self.assertEqual(durable["stage"], "recovery_verified")

    def test_bad_evidence_pin_selector_database_or_boundary_stop_every_writer(self):
        """Неизвестный state и API outage оставляют обе БД и закрытый marker."""
        for damage in ("api", "artifact", "sha", "release", "oci", "selector", "missing_selector",
                       "missing_db", "inode", "false_started", "intent", "candidate_id", "snapshot"):
            with self.subTest(damage=damage), TemporaryDirectory() as temporary:
                host, state, record = self._setup(Path(temporary), "candidate_started")
                evidence = copy.deepcopy(state["compatibility_evidence"])
                if damage == "artifact":
                    evidence["artifact_id"] += 1
                elif damage == "sha":
                    state["old_sha"] = "d" * 40
                elif damage == "release":
                    state["candidate"]["run_attempt"] += 1
                elif damage == "oci":
                    host.bad_image = True
                elif damage == "selector":
                    prod._atomic_text(host.state_dir / "active-db.json", json.dumps(switch.selector(state, "old")))
                elif damage == "missing_selector":
                    (host.state_dir / "active-db.json").unlink()
                elif damage == "missing_db":
                    Path(state["new"]["host_path"]).unlink()
                elif damage == "inode":
                    state["new"]["inode"] += 1
                elif damage == "false_started":
                    state["admission_may_have_started"] = False
                elif damage == "intent":
                    state["recovery_intent"] = {"to": "legacy"}
                elif damage == "candidate_id":
                    state["prepared_containers"][prep.CHANNELS[0]] = "unknown-id"
                elif damage == "snapshot":
                    state["snapshot"]["sha256"] = "0" * 64
                record(state["stage"])
                with patch.object(prod.compatibility, "verify_evidence", return_value=evidence,
                                  side_effect=OSError("API unavailable") if damage == "api" else None):
                    with self.assertRaises(prod.DeployError):
                        prod._recover(host, state)
                self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                self.assertEqual(host.starts, 1)
                self.assertEqual(state["stage"], "degraded")
                self.assertTrue(Path(state["old"]["host_path"]).exists())
                self.assertTrue((host.state_dir / "deploy-in-progress").exists())

    def test_old_runtime_on_old_database_after_admission_fails_closed(self):
        """Даже один old/old writer после true запрещает start и повтор recovery."""
        for channel in prep.CHANNELS:
            with self.subTest(channel=channel), TemporaryDirectory() as temporary:
                host, state, _ = self._setup(Path(temporary), "candidate_started")
                item = host.found[channel]
                item["Image"] = OLD_IMAGE
                item["Config"]["Env"] = ["DATABASE_PATH=" + state["old"]["container_path"]]
                selector = (host.state_dir / "active-db.json").read_bytes()
                databases = {side: Path(state[side]["host_path"]).read_bytes() for side in ("old", "new")}
                for _ in range(2):
                    with self.assertRaises(prod.DeployError):
                        self._recover(host, state)
                    self.assertEqual(state["stage"], "degraded")
                    self.assertIs(state["admission_may_have_started"], True)
                    self.assertEqual(host.starts, 1)
                    self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                    self.assertEqual((host.state_dir / "active-db.json").read_bytes(), selector)
                    self.assertTrue((host.state_dir / "deploy-in-progress").exists())
                    for side, content in databases.items():
                        self.assertEqual(Path(state[side]["host_path"]).read_bytes(), content)

    def test_unknown_stage_does_not_authorize_recovery(self):
        """Неизвестная стадия не получает права запуска даже при целых файлах."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._setup(Path(temporary))
            record("unknown_stage")
            with self.assertRaises(prod.DeployError):
                self._recover(host, state)
            self.assertEqual(state["stage"], "degraded")
            self.assertEqual(host.starts, 0)
            self.assertEqual(switch.read_selected(host, state), "new")

    def test_partial_registration_and_transition_rebind(self):
        """Только durable registration/rebind intent позволяет завершить публикацию."""
        for rebind in (False, True):
            with self.subTest(rebind=rebind), TemporaryDirectory() as temporary:
                host, state, record = self._setup(Path(temporary), "writers_stopped")
                if rebind:
                    before = switch.selector(state, "old")
                    state["transition_id"] = "e" * 32
                    state["selector_rebind"] = {"from": before, "to": switch.selector(state, "old")}
                else:
                    (host.state_dir / "active-db.json").unlink()
                record("writers_stopped")
                self._recover(host, state)
                self.assertEqual(switch.read_selected(host, state), "old")

    def test_malformed_or_missing_state_preserved_and_both_entry_points_guard(self):
        """Ни JSON, ни его секреты не публикуются; повреждённый файл сохраняется."""
        for payload in (b'{"secret":"do-not-print",', b'[]', b'{"stage":"success","stage":"ready"}', None):
            for command in (prod.verify_pinned, lambda host: prod.deploy(host, "v1.2.3")):
                with self.subTest(payload=payload, command=command), TemporaryDirectory() as temporary:
                    host, _, _ = self._setup(Path(temporary), "candidate_started")
                    path = host.state_dir / "state.json"
                    if payload is None:
                        path.unlink()
                    else:
                        path.write_bytes(payload)
                    with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                        for _ in range(2):
                            with self.assertRaises(prod.DeployError) as error:
                                command(host)
                            self.assertNotIn("do-not-print", str(error.exception))
                    self.assertEqual(path.read_bytes() if path.exists() else None, payload)
                    self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                    self.assertTrue((host.state_dir / "recovery-blocked.json").exists())
                    self.assertEqual(host.starts, 1)

    def test_guard_attempts_other_writer_after_first_operation_fails(self):
        """Ошибка update первого канала не пропускает stop и защиту второго."""
        with TemporaryDirectory() as temporary:
            host, _, _ = self._setup(Path(temporary), "candidate_started")
            original = host.run

            def fail(*args, env=None):
                if args[:2] == ("docker", "update") and args[-1] == host.found[prep.CHANNELS[0]]["Id"]:
                    raise OSError("Docker failure")
                return original(*args, env=env)

            with patch.object(host, "run", side_effect=fail), self.assertRaises(prod.DeployError):
                host.guard_known_writers()
            self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))

    def test_evidence_recheck_before_start_can_revoke_recovery(self):
        """Отзыв artifact между подготовкой и start закрывает оба канала."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary), "candidate_started")
            with patch.object(prod.compatibility, "verify_evidence", side_effect=[
                    state["compatibility_evidence"], OSError("revoked")]), self.assertRaises(prod.DeployError):
                prod._recover(host, state)
            self.assertEqual(host.starts, 1)
            self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
            self.assertEqual(switch.read_selected(host, state), "new")

    def test_repeated_command_recovery_retains_gate_and_single_pair(self):
        """Обе команды выполняют recovery под lock, но не открывают следующий deploy."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary))
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                for command in (lambda: prod.deploy(host, "v1.2.3"), lambda: prod.verify_pinned(host)):
                    with self.assertRaisesRegex(prod.DeployError, "rollout закрыт"):
                        command()
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(durable["stage"], "recovery_verified")
            self.assertEqual(switch.read_selected(host, durable), "old")
            self.assertEqual(len(host.found), 2)
            self.assertEqual(host.starts, 2)

    def test_partial_create_missing_channel_recovers_only_before_possible_start(self):
        """Partial create восстанавливается при false; после start отсутствие неоднозначно."""
        for stage in ("runtime_prepared", "admission_recorded", "candidate_started"):
            with self.subTest(stage=stage), TemporaryDirectory() as temporary:
                host, state, _ = self._setup(Path(temporary), stage)
                host.found.pop(prep.CHANNELS[0])
                if stage == "runtime_prepared":
                    self._recover(host, state)
                    self.assertEqual(state["stage"], "recovery_verified")
                    self.assertEqual(host.starts, 1)
                    self.assertEqual(switch.read_selected(host, state), "old")
                else:
                    starts = host.starts
                    with self.assertRaises(prod.DeployError):
                        self._recover(host, state)
                    self.assertEqual(state["stage"], "degraded")
                    self.assertEqual(host.starts, starts)
                    self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                self.assertTrue(Path(state["old"]["host_path"]).exists())
                self.assertTrue(Path(state["new"]["host_path"]).exists())

    def test_partial_create_failure_after_rollback_record_uses_saved_origin(self):
        """Штатный отказ create сохраняет pre-start origin перед rollback_started."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._setup(Path(temporary), "runtime_prepared")
            host.found.pop(prep.CHANNELS[0])
            record("rollback_started", rollback_origin_stage="database_selected")
            self._recover(host, state)
            self.assertEqual(switch.read_selected(host, state), "old")

    def test_partial_create_with_started_survivor_is_not_recovered(self):
        """False не разрешает возврат, если оставшийся candidate уже запускался."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary), "runtime_prepared")
            host.found.pop(prep.CHANNELS[0])
            host.found[prep.CHANNELS[1]]["State"]["StartedAt"] = "2026-11-04T01:00:00Z"
            with self.assertRaises(prod.DeployError):
                self._recover(host, state)
            self.assertEqual(host.starts, 0)
            self.assertEqual(state["stage"], "degraded")


if __name__ == "__main__":
    unittest.main()
