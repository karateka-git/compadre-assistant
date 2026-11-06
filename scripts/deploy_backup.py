"""Создаёт ночную SQLite-копию выбранного поколения под общим deploy lock."""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
import uuid

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import predeploy_compatibility as compatibility
from scripts.deploy_prod import DeployError, Host


def selected_database(host: Host) -> tuple[Path, list[str]]:
    """Выбирает проверенное terminal-поколение без fallback к legacy при v2.

    Вызывающий удерживает deploy lock до завершения backup. Marker запрещает
    действие и при прямом запуске CLI в обход systemd ConditionPathExists.

    Raises:
        DeployError: Переход не завершён или selector/runtime неоднозначны.
    """
    if any(os.path.lexists(host.state_dir / name) for name in (
            "deploy-in-progress", "recovery-blocked.json")):
        raise DeployError("Backup запрещён при незавершённом переходе")
    state = prep.read_state(host.state_dir, host.checkout)
    if not state or state.get("stage") not in ("success", "rolled_back", "pinned_verified", "failed_preflight"):
        raise DeployError("Backup требует завершённое состояние контроллера")
    v2_files = any(os.path.lexists(host.state_dir / name) for name in ("active-db.json", "database-compose.yml"))
    if "protocol_version" in state or v2_files:
        if state.get("stage") not in ("success", "rolled_back"):
            raise DeployError("Backup v2 требует завершённый переход")
        side = switch.read_selected(host, state)
        path = Path(state[side]["host_path"])
        manifest = state[side]["manifest"]
    else:
        path = host.checkout / "data/app.db"
        with closing(compatibility.open_copy_readonly(path)) as connection:
            rows = connection.execute("SELECT name FROM schema_migrations ORDER BY version").fetchall()
        manifest = [row[0] for row in rows]
    # Эта проверка также отвергает runtime.env, оставшийся от другого selector.
    tag = host.runtime_tag()
    good = state.get("last_good")
    if (not isinstance(good, dict) or good.get("tag") != tag
            or host.image(tag)["Id"] != good.get("image_id")):
        raise DeployError("Backup runtime pin не совпадает с last_good")
    if not manifest or any(not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", name) for name in manifest):
        raise DeployError("Backup не подтвердил журнал миграций")
    prep.validate_schema(path, manifest)
    return path, manifest


def backup(host: Host, destination_dir: Path) -> Path:
    """Копирует выбранную SQLite API, проверяет integrity/FK и не удаляет копии.

    Args:
        host: Адаптер production или локального стенда.
        destination_dir: Приватный каталог ночных копий вне checkout.

    Returns:
        Новый уникальный файл backup; старые поколения не меняются.

    Raises:
        DeployError: Состояние не разрешает backup.
        PreparationError: Lock занят, места недостаточно либо копия повреждена.
    """
    target = prep.require_external_state_dir(destination_dir, host.checkout)
    if destination_dir.absolute() != target:
        raise DeployError("Backup directory не должен быть symlink")
    with prep.deploy_lock(host.state_dir, host.checkout):
        source, manifest = selected_database(host)
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        prep.require_disk_space(target, source)
        name = datetime.now(timezone.utc).strftime("nightly-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex + ".db"
        destination = prep.backup_sqlite(source, target / name)
        prep.validate_schema(destination, manifest)
        with closing(compatibility.open_copy_readonly(destination)) as connection:
            compatibility.require_copy_foreign_keys(connection)
        return destination


def main() -> None:
    """Запускает root-only backup для отдельного systemd service."""
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise DeployError("Backup предназначен для root на Linux")
        backup(Host(), Path("/var/backups/obs-chat-bot"))
        print("deploy-backup: success")
    except Exception as error:
        print(f"deploy-backup: отказ {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
