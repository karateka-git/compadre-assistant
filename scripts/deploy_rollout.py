"""Проверяет инфраструктурный допуск SQLite v2 без ручного флага обхода."""

import json
from pathlib import Path
import stat


# Backup использует отдельный unit и отдельный D-Bus object path.
BACKUP_DROPIN = Path("/etc/systemd/system/obs-chat-bot-backup.service.d/zz-deploy-selector.conf")
BACKUP_DBUS_PATH = "/org/freedesktop/systemd1/unit/obs_2dchat_2dbot_2dbackup_2eservice"


def require_infrastructure(host) -> None:
    """Сверяет snapshot, файлы, effective boot/backup и отсутствие чужих writers.

    Проверка только читает инфраструктуру. Она не заменяет exact-pair CI,
    миграцию предварительной копии и согласованное окно без ручных writers.

    Args:
        host: Production Host либо явная модель изолированного испытания.

    Raises:
        ValueError: Файл, загруженный unit или backup допускает обход selector.
    """
    from scripts import deploy_prod as prod

    host.controller_sha()
    files = ((prod.PINNED_DROPIN, "obs-chat-bot-selector.conf"),
             (BACKUP_DROPIN, "obs-chat-bot-backup-selector.conf"),
             (prod.TIMER_GUARD, "obs-chat-bot-backup-guard.conf"),
             (prod.BACKUP_GUARD, "obs-chat-bot-backup-service-guard.conf"))
    for path, template in files:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022
                or path.read_bytes() != (prod.SOURCE_ROOT / "deploy" / template).read_bytes()):
            raise ValueError("Rollout unit file не подтверждён")
    host.run("systemd-analyze", "verify", prod.SERVICE, prod.TIMER, prod.BACKUP_SERVICE)
    for unit in (prod.SERVICE, prod.TIMER, prod.BACKUP_SERVICE):
        if host.run("systemctl", "show", "--property=NeedDaemonReload", "--value", unit) != "no":
            raise ValueError("Rollout unit не загружен")
    host.require_systemd_boot(host.state_dir / "runtime.env")
    paths = host.run("systemctl", "show", "--property=DropInPaths", "--value", prod.BACKUP_SERVICE).split()
    if paths != [str(prod.BACKUP_GUARD), str(BACKUP_DROPIN)]:
        raise ValueError("Порядок backup drop-ins не подтверждён")
    raw = host.run("busctl", "--json=short", "get-property", "org.freedesktop.systemd1",
                   BACKUP_DBUS_PATH, "org.freedesktop.systemd1.Service", "ExecStartEx")
    value = json.loads(raw)
    expected = prod.SELECTOR_EXEC[:-2] + ["scripts.deploy_backup"]
    commands = value.get("data")
    if (value.get("type") != "a(sasasttttuii)" or not isinstance(commands, list)
            or len(commands) != 1 or not isinstance(commands[0], list) or len(commands[0]) != 10
            or commands[0][:3] != [expected[0], expected, prod.PINNED_EXEC_FLAGS]):
        raise ValueError("Effective backup не использует selector helper")
    # Guards проверяются и на диске, и в загруженном unit: поздний drop-in
    # может сбросить Condition, не меняя проверенного файла.
    for unit in (prod.TIMER, prod.BACKUP_SERVICE):
        object_path = BACKUP_DBUS_PATH if unit == prod.BACKUP_SERVICE else (
            "/org/freedesktop/systemd1/unit/obs_2dchat_2dbot_2dbackup_2etimer")
        value = json.loads(host.run("busctl", "--json=short", "get-property", "org.freedesktop.systemd1",
                                    object_path, "org.freedesktop.systemd1.Unit", "Conditions"))
        conditions = value.get("data")
        expected = ["ConditionPathExists", False, True, str(host.state_dir / "deploy-in-progress")]
        if (value.get("type") != "a(sbbsi)" or not isinstance(conditions, list)
                or not 1 <= len(conditions) <= 2
                or any(not isinstance(item, list) or len(item) != 5 or item[:4] != expected
                       for item in conditions)):
            raise ValueError("Effective backup marker guard не подтверждён")
    host.require_no_foreign_database_mounts({item["Id"] for item in host.recovery_inventory().values()})
