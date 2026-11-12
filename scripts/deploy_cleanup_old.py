"""Удаляет прежнее поколение SQLite после независимой приёмки выпуска."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from scripts import deploy_acceptance as acceptance
from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_runtime as runtime
from scripts import predeploy_compatibility as compatibility
from scripts.deploy_prod import DeployError, Host, SERVICE


_SIDECARS = ("-wal", "-shm", "-journal")
_RUNTIME_ID = re.compile(r"[0-9a-f]{64}")


class CleanupError(DeployError):
    """Означает, что прежнее поколение нельзя доказанно удалить."""


def _old_path(host: Host, state: dict) -> Path:
    """Проверяет канонический путь старой БД внутри каталога данных."""
    old = state.get("old")
    if not isinstance(old, dict):
        raise CleanupError("Не зарегистрировано прежнее поколение")
    generation = old.get("generation_id")
    expected = "app.db" if generation == "legacy" else f"app-{generation}.db"
    path = Path(old.get("host_path", ""))
    data = host.checkout / "data"
    if (not path.is_absolute() or path.parent != data or path.name != expected
            or path.resolve() != path or not isinstance(generation, str)
            or (generation != "legacy" and not re.fullmatch(r"[0-9a-f]{32}", generation))
            or path == Path(state.get("new", {}).get("host_path", ""))):
        raise CleanupError("Путь прежней SQLite не подтверждён")
    return path


def _file_identity(path: Path) -> list[int]:
    """Получает device/inode только для обычного файла без hardlink."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CleanupError("Файл очистки имеет alias или необычный тип")
    return [info.st_dev, info.st_ino]


def _snapshot(host: Host, state: dict) -> None:
    """Подтверждает необходимый окончательный backup без изменения данных."""
    snapshot = state.get("snapshot")
    good = state.get("last_good")
    if not isinstance(snapshot, dict) or not isinstance(good, dict):
        raise CleanupError("Не подтверждён окончательный backup")
    path = Path(snapshot.get("path", ""))
    generation = state.get("new", {}).get("generation_id")
    if (not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation)
            or path.parent != host.state_dir or path.name != f"final-{generation}.db"
            or not path.is_absolute() or path.resolve() != path
            or good.get("backup_id") != str(path)
            or snapshot.get("integrity") is not True or snapshot.get("foreign_keys") is not True
            or not isinstance(snapshot.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot["sha256"])):
        raise CleanupError("Окончательный backup не совпадает с принятой установкой")
    _file_identity(path)
    if any(os.path.lexists(str(path) + suffix) for suffix in _SIDECARS):
        raise CleanupError("Окончательный backup имеет SQLite sidecar")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != snapshot["sha256"]:
            raise CleanupError("Окончательный backup изменился")
    prep.validate_schema(path, state["schema_before"])
    with closing(compatibility.open_copy_readonly(path)) as connection:
        compatibility.require_copy_foreign_keys(connection)


def _require_runtime(host: Host, state: dict, confirmed_runtime: str) -> dict:
    """Подтверждает завершённый runtime до первого удаления old."""
    if (state.get("stage") != "success" or state.get("protocol_version") != switch.PROTOCOL_VERSION
            or state.get("admission_may_have_started") is not True
            or any(os.path.lexists(host.state_dir / name)
                   for name in ("deploy-in-progress", "recovery-blocked.json"))):
        raise CleanupError("Переход или recovery не завершён")
    boot = runtime.verify(host, state, require_running=True)
    if (not isinstance(boot, dict) or boot.get("outcome") != "success"
            or switch.read_selected(host, state) != "new"):
        raise CleanupError("Не подтверждена выбранная новая SQLite")
    candidate, good = state.get("candidate"), state.get("last_good")
    if (not isinstance(candidate, dict) or not isinstance(good, dict)
            or good.get("version") != candidate.get("version")
            or good.get("sha") != candidate.get("sha")
            or good.get("ci_run_id") != candidate.get("run_id")
            or good.get("ci_run_attempt") != candidate.get("run_attempt")):
        raise CleanupError("Last good не совпадает с установленным выпуском")
    receipt = acceptance._verify_locked(host, good["version"], good["sha"])
    if receipt.get("runtime_id") != confirmed_runtime:
        raise CleanupError("Runtime изменился после живой приёмки")
    return boot


def _partial_runtime(host: Host, state: dict, confirmed_runtime: str) -> None:
    """Сверяет живой new runtime при частично удалённом old по durable intent.

    Обычная приёмка в этот момент намеренно не читает неполный old. Эта
    проверка не меняет общий контракт: после completion приёмка повторяется.
    """
    acceptance._require_unblocked(host)
    if state.get("stage") != "success":
        raise CleanupError("Переход перестал быть успешным")
    switch.require_boundary(state)
    boot = state.get("boot")
    if (not isinstance(boot, dict) or boot.get("outcome") != "success"
            or boot != runtime.boot_record(state, "success")):
        raise CleanupError("Boot изменился после начала очистки")
    selector_path = host.state_dir / "active-db.json"
    selector_info = selector_path.lstat()
    if (not stat.S_ISREG(selector_info.st_mode) or selector_info.st_nlink != 1
            or (os.name == "posix" and (selector_info.st_uid != 0 or selector_info.st_gid != 0
                                       or stat.S_IMODE(selector_info.st_mode) != 0o600))
            or prep.parse_state_object(selector_path.read_text(encoding="utf-8"))
            != switch.selector(state, "new")
            or state.get("switch_intent") != {"from": state["old"]["generation_id"],
                                               "to": state["new"]["generation_id"]}):
        raise CleanupError("Selector изменился после начала очистки")
    switch.require_identity(state["new"], host.checkout / "data")
    tag = boot["tag"]
    expected_env = host.database_env(tag, state["new"]["container_path"])
    if (host.state_dir / "runtime.env").read_text(encoding="utf-8").splitlines() != [
            f"{key}={value}" for key, value in expected_env.items()]:
        raise CleanupError("Runtime pin изменился после начала очистки")
    image = host.image(tag)
    if (image["Id"] != boot["image_id"]
            or image.get("Config", {}).get("Labels", {}).get(prep.OCI_REVISION) != boot["sha"]):
        raise CleanupError("Image изменился после начала очистки")
    found = host.containers()
    if set(found) != set(prep.CHANNELS):
        raise CleanupError("Пара каналов изменилась после начала очистки")
    for name, item in found.items():
        paths = [entry for entry in item["Config"].get("Env", [])
                 if entry.startswith("DATABASE_PATH=")]
        mounts = [mount for mount in item.get("Mounts", [])
                  if mount.get("Destination") == "/app/data"]
        if (item["Id"] != boot["containers"][name] or item.get("Image") != boot["image_id"]
                or paths != ["DATABASE_PATH=" + boot["database_path"]]
                or item["HostConfig"]["RestartPolicy"]["Name"] != "no"
                or item.get("RestartCount") != 0 or item["State"].get("Running") is not True
                or len(mounts) != 1 or mounts[0].get("Type") != "bind"
                or mounts[0].get("Source") != str(host.checkout / "data")
                or mounts[0].get("RW") is not True
                or any(mount.get("Destination", "").startswith("/app/data/")
                       or (mount.get("Source") == str(host.checkout / "data")
                           and mount.get("Destination") != "/app/data")
                       for mount in item.get("Mounts", []))):
            raise CleanupError("Контейнер сменил поколение или путь SQLite")
        host.require_runtime_schema(item, state)
    host.require_no_foreign_database_mounts(set(boot["containers"].values()))
    host.require_no_duplicate_polling()
    # Exact ID и started_at входят в runtime_id; остальное повторно проверяет
    # полная приёмка сразу после durable completion.
    current = acceptance._container_identity(host, boot["image_id"])
    good, candidate = state["last_good"], state["candidate"]
    if (good.get("version") != candidate.get("version")
            or good.get("sha") != candidate.get("sha")
            or good.get("ci_run_id") != candidate.get("run_id")
            or good.get("ci_run_attempt") != candidate.get("run_attempt")
            or good.get("image_id") != boot["image_id"]
            or good.get("tag") != tag or candidate.get("tag") != tag
            or host.run("git", "--no-optional-locks", "status", "--porcelain=v1",
                        "--untracked-files=all")
            or host.run("git", "rev-parse", "HEAD") != good["sha"]
            or host.run("git", "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
            or not host.unit_active(SERVICE)
            or host.timer_snapshot() != state.get("timer_before")):
        raise CleanupError("Установка изменилась после начала очистки")
    prep.require_exact_tag(host.checkout, good["version"], good["sha"],
                           tag_object_sha=candidate.get("tag_object_sha"))
    receipt = {"schema_version": 1, "version": good["version"], "sha": good["sha"],
               "kind": good.get("kind", "ordinary"), "release_id": candidate["release_id"],
               "run_id": candidate["run_id"], "run_attempt": candidate["run_attempt"],
               "generation_id": state["new"]["generation_id"],
               "migration": state["schema_after"][-1]}
    identity = {"receipt": receipt, "transition": state["transition_id"],
                "image": boot["image_id"], "containers": current,
                "fingerprint": state["new"]["fingerprint"],
                "tag_object_sha": candidate.get("tag_object_sha")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    if digest != confirmed_runtime:
        raise CleanupError("Runtime изменился после живой приёмки")


def _intent(host: Host, state: dict, confirmed_runtime: str) -> dict:
    """Строит точный durable intent с inode каждого существующего sidecar."""
    path = _old_path(host, state)
    switch.require_identity(state["old"], host.checkout / "data")
    old_identity = _file_identity(path)
    if old_identity != [state["old"]["device"], state["old"]["inode"]]:
        raise CleanupError("Прежняя БД сменилась после регистрации")
    sidecars = {suffix: _file_identity(Path(str(path) + suffix))
                for suffix in _SIDECARS if os.path.lexists(str(path) + suffix)}
    return switch.old_cleanup_record(state) | {"runtime_id": confirmed_runtime, "sidecars": sidecars}


def _verify_intent(host: Host, state: dict, confirmed_runtime: str) -> tuple[Path, dict]:
    """Сверяет intent и каждый ещё существующий inode перед unlink."""
    intent = state.get("old_generation_cleanup_intent")
    expected = switch.old_cleanup_record(state)
    if (not isinstance(intent, dict) or intent.keys() != expected.keys() | {"runtime_id", "sidecars"}
            or any(intent.get(key) != value for key, value in expected.items())
            or intent.get("runtime_id") != confirmed_runtime
            or not isinstance(intent.get("sidecars"), dict)
            or not set(intent["sidecars"]).issubset(_SIDECARS)):
        raise CleanupError("Intent очистки не совпадает с принятым переходом")
    path = _old_path(host, state)
    for suffix, identity in intent["sidecars"].items():
        if (not isinstance(identity, list) or len(identity) != 2
                or any(type(value) is not int for value in identity)):
            raise CleanupError("Некорректная идентичность SQLite sidecar")
    for suffix in ("", *_SIDECARS):
        item = Path(str(path) + suffix)
        if suffix and suffix not in intent["sidecars"] and os.path.lexists(item):
            raise CleanupError("После intent появился новый SQLite sidecar")
        if os.path.lexists(item):
            expected_id = ([expected["old_device"], expected["old_inode"]] if not suffix
                           else intent["sidecars"][suffix])
            if _file_identity(item) != expected_id:
                raise CleanupError("Файл прежнего поколения изменился до удаления")
    return path, intent


def _fsync_dir(path: Path) -> None:
    """Сохраняет удаление записи каталога на Linux перед следующим шагом."""
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _require_no_open_old(state: dict) -> None:
    """Отказывает, если Linux-процесс держит old или его sidecar открытым."""
    if os.name != "posix":
        return
    intent = state["old_generation_cleanup_intent"]
    identities = {(intent["old_device"], intent["old_inode"])}
    identities.update(tuple(value) for value in intent["sidecars"].values())
    try:
        with os.scandir("/proc") as processes:
            for process in processes:
                if not process.name.isdigit():
                    continue
                try:
                    with os.scandir(Path(process.path) / "fd") as descriptors:
                        for descriptor in descriptors:
                            try:
                                info = os.stat(descriptor.path)
                            except FileNotFoundError:
                                continue
                            if (info.st_dev, info.st_ino) in identities:
                                raise CleanupError("Прежняя SQLite ещё открыта процессом")
                except FileNotFoundError:
                    continue
    except OSError as error:
        raise CleanupError("Не удалось исключить открытые дескрипторы old") from error


def cleanup_old(host: Host, version: str, sha: str, confirmed_runtime: str) -> dict:
    """Повторяемо удаляет только прежнюю SQLite после живой приёмки.

    Args:
        host: Доверенный адаптер установленного VPS или изолированного стенда.
        version: Точная установленная версия Release.
        sha: Полный SHA текущего last_good.
        confirmed_runtime: Runtime, для которого владелец проверил оба канала.

    Returns:
        Состояние завершённой очистки, привязанное к исходному inode.

    Raises:
        CleanupError: Приёмка, backup, intent или удаляемый файл расходятся.
        DeployError: Runtime, selector или контейнеры не подтверждены.
    """
    if not isinstance(confirmed_runtime, str) or not _RUNTIME_ID.fullmatch(confirmed_runtime):
        raise CleanupError("Нужно подтверждение живых ответов для точного runtime_id")
    with prep.deploy_lock(host.state_dir, host.checkout):
        state = prep.read_state(host.state_dir, host.checkout)
        if not isinstance(state, dict) or state.get("stage") != "success":
            raise CleanupError("Очистка требует завершённый успешный выпуск")
        good = state.get("last_good")
        if (not isinstance(good, dict) or good.get("version") != version or good.get("sha") != sha):
            raise CleanupError("Установленный выпуск не совпадает с командой очистки")
        if "old_generation_cleanup" in state:
            receipt = acceptance._verify_locked(host, version, sha)
            if receipt["runtime_id"] != confirmed_runtime:
                raise CleanupError("Runtime изменился после живой приёмки")
            _snapshot(host, state)
            return state["old_generation_cleanup"]
        _snapshot(host, state)
        if "old_generation_cleanup_intent" not in state:
            _require_runtime(host, state, confirmed_runtime)
            state["old_generation_cleanup_intent"] = _intent(host, state, confirmed_runtime)
            _require_no_open_old(state)
            prep.write_state(host.state_dir, host.checkout, state)
            state = prep.read_state(host.state_dir, host.checkout)
        path, intent = _verify_intent(host, state, confirmed_runtime)
        _require_no_open_old(state)
        remaining = {suffix for suffix in ("", *_SIDECARS)
                     if os.path.lexists(str(path) + suffix)}
        if "" in remaining and set(intent["sidecars"]).issubset(remaining):
            _require_runtime(host, state, confirmed_runtime)
        else:
            _partial_runtime(host, state, confirmed_runtime)
        # Удаляем каждый sidecar раньше БД. После каждого unlink синхронизируем
        # каталог: повтор опирается на durable intent и оставшиеся inode.
        for suffix in _SIDECARS:
            if suffix in intent["sidecars"]:
                item = Path(str(path) + suffix)
                if os.path.lexists(item):
                    _verify_intent(host, state, confirmed_runtime)
                    _require_no_open_old(state)
                    item.unlink()
                    _fsync_dir(item.parent)
        if os.path.lexists(path):
            _verify_intent(host, state, confirmed_runtime)
            _require_no_open_old(state)
            path.unlink()
            _fsync_dir(path.parent)
        _verify_intent(host, state, confirmed_runtime)
        state["old_generation_cleanup"] = switch.old_cleanup_record(state)
        del state["old_generation_cleanup_intent"]
        prep.write_state(host.state_dir, host.checkout, state)
        receipt = acceptance._verify_locked(host, version, sha)
        if receipt["runtime_id"] != confirmed_runtime:
            raise CleanupError("Runtime изменился после очистки")
        return state["old_generation_cleanup"]


def check_old(host: Host, version: str, sha: str) -> dict:
    """Показывает техническую приёмку и состояние очистки без записи."""
    with prep.deploy_lock(host.state_dir, host.checkout):
        receipt = acceptance._verify_locked(host, version, sha)
        state = prep.read_state(host.state_dir, host.checkout)
        _snapshot(host, state)
        return {"receipt": receipt, "cleanup": "completed" if "old_generation_cleanup" in state else "pending"}


def main() -> None:
    """Запускает root-only проверку или очистку выбранного выпуска."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="точная установленная версия vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный SHA установленного коммита")
    parser.add_argument("--apply", action="store_true", help="удалить прежнее поколение")
    parser.add_argument("--confirm-live", metavar="RUNTIME_ID", help="подтверждённая владельцем установка")
    args = parser.parse_args()
    if args.apply != (args.confirm_live is not None):
        parser.error("--confirm-live требуется только вместе с --apply")
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise CleanupError("Очистка предназначена для root на Linux")
        host = Host()
        result = (cleanup_old(host, args.version, args.sha, args.confirm_live) if args.apply
                  else check_old(host, args.version, args.sha))
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    except Exception as error:
        print(f"deploy-cleanup-old: отказ {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
