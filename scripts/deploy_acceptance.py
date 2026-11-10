"""Сверяет техническую готовность уже установленного выпуска без изменений."""

from contextlib import contextmanager
import hashlib
import json
import os
import re
import stat
from typing import Iterator

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_runtime as runtime
from scripts.deploy_prod import DeployError, Host, IMAGE_PATTERN, SERVICE


@contextmanager
def _read_lock(host: Host) -> Iterator[None]:
    """Разделяет существующий lock с читателями без создания файлов."""
    import fcntl

    fd = os.open(host.state_dir / "deploy.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
            raise DeployError("Lock выпуска не является приватным обычным файлом")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeployError("Техническая приёмка недоступна во время другого выпуска") from error
        yield
    finally:
        os.close(fd)


def _require_unblocked(host: Host) -> None:
    """Отклоняет состояние, в котором выкладка или восстановление не завершены."""
    if any(os.path.lexists(host.state_dir / name)
           for name in ("deploy-in-progress", "recovery-blocked.json")):
        raise DeployError("Техническая приёмка закрыта маркером")


def _container_identity(host: Host, image: str) -> dict[str, tuple[str, str]]:
    """Возвращает идентичность двух здоровых процессов без раскрытия inspect."""
    found = host.containers()
    if set(found) != set(prep.CHANNELS):
        raise DeployError("Техническая приёмка требует оба канала")
    identity = {}
    for name, item in found.items():
        state = item["State"]
        identifier, started = item.get("Id"), state.get("StartedAt")
        if (item.get("Image") != image or item.get("RestartCount") != 0
                or state.get("Running") is not True or state.get("Health", {}).get("Status") != "healthy"
                or not isinstance(identifier, str) or not identifier
                or not isinstance(started, str) or not started or started.startswith("0001-")):
            raise DeployError("Техническая приёмка не подтвердила работающий канал")
        identity[name] = (identifier, started)
    if len({identifier for identifier, _ in identity.values()}) != len(prep.CHANNELS):
        raise DeployError("Техническая приёмка обнаружила повтор контейнера")
    return identity


def _require_legacy_database(host: Host, manifest: list[str]) -> str:
    """Сверяет обычный bind и SQLite без selector у legacy выпуска."""
    data = str(host.checkout / "data")
    found = host.containers()
    for item in found.values():
        config = item["Config"]
        paths = [value.removeprefix("DATABASE_PATH=") for value in config.get("Env", [])
                 if value.startswith("DATABASE_PATH=")]
        mounts = item.get("Mounts", [])
        bind = [mount for mount in mounts if mount.get("Destination") == "/app/data"]
        if (not (paths == ["/app/data/app.db"]
                 or (paths == ["data/app.db"] and config.get("WorkingDir") == "/app"))
                or len(bind) != 1 or bind[0].get("Type") != "bind"
                or bind[0].get("Source") != data or bind[0].get("RW") is not True
                or any(mount.get("Destination", "").startswith("/app/data/") for mount in mounts)):
            raise DeployError("Техническая приёмка не подтвердила legacy mount или путь SQLite")
    host.require_no_foreign_database_mounts({item["Id"] for item in found.values()})
    return switch.identity(host.checkout / "data/app.db", "legacy", manifest)["fingerprint"]


def _verify_locked(host: Host, version: str, sha: str) -> dict:
    """Подтверждает установку и формирует безопасный снимок под lock."""
    _require_unblocked(host)
    state = prep.read_state(host.state_dir, host.checkout) or {}
    if state.get("stage") != "success":
        raise DeployError("Техническая приёмка требует завершённый успешный выпуск")
    good, candidate = state["last_good"], state["candidate"]
    kind = good.get("kind", "ordinary")
    image, tag = good["image_id"], prep.pinned_tag(sha)
    if (good.get("version") != version or good.get("sha") != sha
            or kind not in ("ordinary", "emergency") or candidate.get("kind", "ordinary") != kind
            or not isinstance(image, str) or not IMAGE_PATTERN.fullmatch(image)
            or good.get("tag") != tag or candidate.get("tag") != tag or candidate.get("image_id") != image
            or candidate.get("version") != version or candidate.get("sha") != sha
            or good.get("ci_run_id") != candidate.get("run_id")
            or good.get("ci_run_attempt") != candidate.get("run_attempt")
            or any(type(candidate.get(key)) is not int or candidate[key] <= 0
                   for key in ("release_id", "run_id", "run_attempt"))):
        raise DeployError("Техническая приёмка не подтвердила ожидаемый Release, SHA и CI")
    if (host.run("git", "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all")
            or host.run("git", "rev-parse", "HEAD") != sha
            or host.run("git", "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"):
        raise DeployError("Техническая приёмка не подтвердила чистый checkout точного SHA")
    prep.require_exact_tag(host.checkout, version, sha, tag_object_sha=candidate.get("tag_object_sha"))
    manifest = state["schema_after"]
    if not isinstance(manifest, list) or not manifest or any(
            not isinstance(name, str) or not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", name)
            for name in manifest):
        raise DeployError("Техническая приёмка не подтвердила журнал миграций")
    initial = _container_identity(host, image)
    # Durable success уже подтверждает старт polling; старые строки логов
    # могут исчезнуть к отдельному осмотру после живой проверки владельцем.
    if state.get("protocol_version") == switch.PROTOCOL_VERSION:
        boot = runtime.verify(host, state, require_running=True)
        if (boot["outcome"] != "success" or boot["sha"] != sha or boot["image_id"] != image
                or boot["generation_id"] != state["new"]["generation_id"]
                or manifest != state["new"]["manifest"]):
            raise DeployError("Техническая приёмка не подтвердила выбранное поколение")
        generation, transition = boot["generation_id"], boot["transition_id"]
        fingerprint = state["new"]["fingerprint"]
    elif "protocol_version" not in state and not os.path.lexists(host.state_dir / "active-db.json"):
        host.verify_installed(good)
        if host.image(image).get("Config", {}).get("Labels", {}).get(prep.OCI_REVISION) != sha:
            raise DeployError("Техническая приёмка не подтвердила legacy OCI revision")
        host.require_no_duplicate_polling()
        fingerprint = _require_legacy_database(host, manifest)
        generation, transition = "legacy", None
    else:
        raise DeployError("Техническая приёмка не подтвердила протокол SQLite")
    timer = state.get("timer_before")
    if (not isinstance(timer, dict) or timer.keys() != {"enabled", "active"}
            or any(type(value) is not bool for value in timer.values())
            or not host.unit_active(SERVICE) or host.timer_snapshot() != timer
            or _container_identity(host, image) != initial
            or prep.read_state(host.state_dir, host.checkout) != state):
        raise DeployError("Техническая приёмка не подтвердила unit, таймер или устойчивость выпуска")
    _require_unblocked(host)
    receipt = {
        "schema_version": 1, "version": version, "sha": sha, "kind": kind,
        "release_id": candidate["release_id"], "run_id": candidate["run_id"],
        "run_attempt": candidate["run_attempt"], "generation_id": generation, "migration": manifest[-1],
    }
    identity = {"receipt": receipt, "transition": transition, "image": image, "containers": initial,
                "fingerprint": fingerprint, "tag_object_sha": candidate.get("tag_object_sha")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return receipt | {"runtime_id": digest}


def verify_installed(host: Host, version: str, sha: str) -> dict:
    """Проверяет техническую готовность точного установленного Release.

    Args:
        host: Адаптер чтения Docker, systemd и внешнего состояния VPS.
        version: Явно ожидаемая опубликованная версия.
        sha: Явно ожидаемый полный commit SHA.

    Returns:
        Снимок с идентичностью выпуска, схемы и пары каналов.

    Raises:
        DeployError: Выпуск или устойчивое состояние не подтверждены.
        OSError: Каталог состояния, блокировка или другой источник недоступен.
        ValueError: Сохранённые данные перехода не соответствуют фактам.
    """
    if not prep.VERSION_PATTERN.fullmatch(version) or not prep.SHA_PATTERN.fullmatch(sha):
        raise DeployError("Техническая приёмка требует версию и полный SHA")
    with _read_lock(host):
        return _verify_locked(host, version, sha)
