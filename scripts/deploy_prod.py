"""Контроллер ручного production-выпуска для двух каналов Assistant.

Запускать только после установки внешнего pin и systemd drop-in по runbook.
Процесс целиком владеет критической секцией и не выполняет автоматический restore БД.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any

from scripts import deploy_prepare as prep
from scripts import deploy_database_switch as database_switch
from scripts import deploy_metrics as metrics
from scripts import predeploy_compatibility as compatibility
from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.full_ci_controller import GitHub
from scripts.select_deploy_release import SelectedRelease, select_emergency_release, select_release


CHECKOUT = Path("/opt/obs-chat-bot")
STATE_DIR = Path("/var/lib/compadre-assistant-deploy")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
SERVICE = "obs-chat-bot.service"
TIMER = "obs-chat-bot-backup.timer"
BACKUP_SERVICE = "obs-chat-bot-backup.service"
SERVICE_DBUS_PATH = "/org/freedesktop/systemd1/unit/obs_2dchat_2dbot_2eservice"
CHANNELS_DROPIN = Path("/etc/systemd/system/obs-chat-bot.service.d/channels.conf")
LEGACY_PINNED_DROPIN = Path("/etc/systemd/system/obs-chat-bot.service.d/90-deploy-pinned.conf")
# На VPS файл 90 остался от v1.0.1 и не заменяется при установке нового zz.
LEGACY_PINNED_TEMPLATE = Path("deploy/obs-chat-bot-pinned-v101.conf")
# systemd читает drop-in по имени: этот файл обязан идти после channels.conf.
PINNED_DROPIN = Path("/etc/systemd/system/obs-chat-bot.service.d/zz-deploy-pinned.conf")
TIMER_GUARD = Path("/etc/systemd/system/obs-chat-bot-backup.timer.d/90-deploy-guard.conf")
BACKUP_GUARD = Path("/etc/systemd/system/obs-chat-bot-backup.service.d/90-deploy-guard.conf")
PINNED_EXEC = ["/usr/bin/docker", "compose", "up", "-d", "--no-build", "--pull", "never",
               "vk_assistant", "tg_assistant"]
PINNED_STOP = ["/usr/bin/docker", "compose", "stop", "vk_assistant", "tg_assistant"]
# Префикс ! у systemd сохраняет sandbox, но выполняет только эти команды без User=/Group=.
PINNED_EXEC_FLAGS = ["no-setuid"]
DEPLOY_SSH = ("ssh -i /home/user/.ssh/obs-chat-bot_deploy -o IdentitiesOnly=yes "
              "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/user/.ssh/known_hosts "
              "-o BatchMode=yes")
IMAGE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
LOG_SIGNALS = {
    "tg_assistant": "Telegram bot polling started",
    "vk_assistant": "VK bot long polling started",
}
COOLDOWN = timedelta(hours=1)


class DeployError(RuntimeError):
    """Обозначает контролируемый отказ выпуска без вывода секретов."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Сохраняет сообщение контроллера и необязательный статический SAFE-код."""
        super().__init__(message)
        self.code = code


def _command(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Выполняет команду и возвращает stdout, скрывая потенциальный stderr."""
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if result.returncode:
        raise DeployError(f"{args[0]} завершился с кодом {result.returncode}")
    return result.stdout.strip()


def _atomic_text(path: Path, value: str) -> None:
    """Атомарно пишет приватный серверный файл и синхронизирует каталог."""
    import tempfile

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _version(value: str) -> tuple[int, int, int]:
    """Преобразует каноническую версию в ключ для обычного SemVer порядка."""
    if not prep.VERSION_PATTERN.fullmatch(value):
        raise DeployError("Нужна каноническая версия vMAJOR.MINOR.PATCH")
    return tuple(int(part) for part in value[1:].split("."))  # type: ignore[return-value]


class Host:
    """Изолирует операции с VPS для проверки контроллера на fake host."""

    def __init__(self, checkout: Path = CHECKOUT, state_dir: Path = STATE_DIR) -> None:
        """Сохраняет пути checkout и внешнего состояния выпуска."""
        self.checkout = checkout
        self.state_dir = prep.require_external_state_dir(state_dir, checkout)

    def run(self, *args: str, env: dict[str, str] | None = None) -> str:
        """Запускает системную команду из production checkout."""
        controlled = os.environ.copy()
        for key in ("COMPOSE_FILE", "ASSISTANT_IMAGE", "ASSISTANT_DATABASE_PATH", "DOCKER_CONTEXT", "GIT_SSH_COMMAND"):
            controlled.pop(key, None)
        controlled["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        if env is not None:
            controlled.update(env)
        return _command(*args, cwd=self.checkout, env=controlled)

    def logs(self, container_id: str) -> str:
        """Читает короткий stdout/stderr контейнера только в память проверяющего процесса."""
        controlled = os.environ.copy()
        controlled["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        controlled.pop("DOCKER_CONTEXT", None)
        result = subprocess.run(("docker", "logs", "--since", "10m", "--tail", "500", container_id),
                                cwd=self.checkout, env=controlled, text=True,
                                capture_output=True, check=False)
        if result.returncode:
            raise DeployError("Не удалось прочитать журнал канала")
        return result.stdout + result.stderr

    def select(self, version: str) -> SelectedRelease:
        """Повторно проверяет опубликованный Release и точную попытку CI."""
        previous = Path.cwd()
        try:
            os.chdir(self.checkout)
            return select_release(GitHub(EXPECTED_REPOSITORY, "", allow_anonymous=True), version)
        finally:
            os.chdir(previous)

    def select_emergency(self, version: str) -> SelectedRelease:
        """Повторно сверяет аварийный Release и фактический полный CI."""
        previous = Path.cwd()
        try:
            os.chdir(self.checkout)
            return select_emergency_release(GitHub(EXPECTED_REPOSITORY, "", allow_anonymous=True), version)
        finally:
            os.chdir(previous)

    def initiator(self) -> str:
        """Возвращает подтверждённого sudo инициатора серверной команды."""
        import pwd

        uid = os.environ.get("SUDO_UID")
        if uid is None or not uid.isdecimal():
            raise DeployError("Аварийный режим требует запуска владельцем через sudo")
        account = pwd.getpwuid(int(uid))
        if account.pw_name != "user" or int(uid) == 0:
            raise DeployError("Аварийный режим доступен только серверному владельцу user")
        return f"{account.pw_name} (uid={uid})"

    def fetch(self, selected: SelectedRelease) -> None:
        """Получает только названный tag и SHA существующим read-only deploy key."""
        env = {"GIT_SSH_COMMAND": DEPLOY_SSH}
        self.run("git", "fetch", "--no-tags", "origin", selected.sha, env=env)
        self.run("git", "fetch", "--no-tags", "origin",
                 f"refs/tags/{selected.version}:refs/tags/{selected.version}", env=env)
        prep.require_exact_tag(self.checkout, selected.version, selected.sha)
        self.run("git", "checkout", "--detach", selected.sha)
        prep.require_clean_checkout(self.checkout, selected.sha)

    def restore_checkout(self, sha: str) -> None:
        """Возвращает чистый detached checkout прежнего commit после отказа."""
        self.run("git", "checkout", "--detach", sha)
        prep.require_clean_checkout(self.checkout, sha)

    def image(self, reference: str) -> dict[str, Any]:
        """Читает локальный image ID и OCI метку без env и данных контейнера."""
        value = json.loads(self.run("docker", "image", "inspect", reference, "--format", "{{json .}}"))
        if not isinstance(value, dict) or not IMAGE_PATTERN.fullmatch(str(value.get("Id", ""))):
            raise DeployError("Некорректный Docker image ID")
        return value

    def containers(self) -> dict[str, dict[str, Any]]:
        """Читает ограниченный набор полей обоих Compose-контейнеров."""
        found: dict[str, dict[str, Any]] = {}
        for name in prep.CHANNELS:
            container_id = self.run("docker", "compose", "ps", "--all", "-q", name)
            if not container_id:
                raise DeployError(f"Контейнер {name} отсутствует")
            value = json.loads(self.run("docker", "inspect", container_id, "--format", "{{json .}}"))
            if not isinstance(value, dict):
                raise DeployError(f"Некорректный inspect: {name}")
            found[name] = value
        return found

    def require_no_duplicate_polling(self) -> None:
        """Отклоняет другой Docker-контейнер с теми же Telegram/VK credentials.

        Значения токенов остаются только в памяти процесса и не попадают в журнал.
        """
        current = self.containers()
        own_ids = {item["Id"] for item in current.values()}

        def credentials(item: dict[str, Any]) -> dict[str, str]:
            values = dict(entry.split("=", 1) for entry in (item["Config"].get("Env") or [])
                          if "=" in entry)
            return {key: values[key] for key in ("TELEGRAM_BOT_TOKEN", "VK_BOT_TOKEN")
                    if values.get(key)}

        known = {name: credentials(item) for name, item in current.items()}
        if not known["tg_assistant"].get("TELEGRAM_BOT_TOKEN") or not known["vk_assistant"].get("VK_BOT_TOKEN"):
            raise DeployError("Не удалось подтвердить credentials действующих каналов")
        for container_id in self.run("docker", "ps", "--no-trunc", "-q").splitlines():
            if container_id in own_ids:
                continue
            item = json.loads(self.run("docker", "inspect", container_id, "--format", "{{json .}}"))
            other = credentials(item)
            if any(other.get(key) == value for channel in known.values() for key, value in channel.items()):
                raise DeployError("Другой Docker-контейнер использует credentials канала")

    def compose_env(self, tag: str, *, critical: bool = False) -> dict[str, str]:
        """Задаёт тот же pin и файлы Compose, которые закреплены в systemd."""
        files = [self.checkout / "docker-compose.yml", self.state_dir / "pinned-compose.yml"]
        if critical:
            files.append(self.state_dir / "critical-compose.yml")
        return {"ASSISTANT_IMAGE": tag, "COMPOSE_FILE": ":".join(map(str, files))}

    def require_compose_pin(self, tag: str, *, critical: bool = False) -> None:
        """Проверяет effective image, pull и restart policy обоих каналов."""
        env = self.compose_env(tag, critical=critical)
        self.run("docker", "compose", "config", "--quiet", env=env)
        config = json.loads(self.run("docker", "compose", "config", "--format", "json", env=env))
        expected_restart = "no" if critical else "unless-stopped"
        if any(config["services"][name].get("image") != tag or
               config["services"][name].get("pull_policy") != "never" or
               str(config["services"][name].get("restart")) != expected_restart
               for name in prep.CHANNELS):
            raise DeployError("Effective Compose не закрепляет оба канала и restart policy")

    def systemd_property(self, interface: str, name: str, signature: str) -> list[Any]:
        """Читает структурированное effective свойство загруженного systemd unit.

        Args:
            interface: D-Bus интерфейс unit или service.
            name: Имя проверяемого свойства.
            signature: Ожидаемая D-Bus сигнатура.

        Returns:
            Список значений свойства.

        Raises:
            DeployError: Свойство отсутствует или имеет неожиданную форму.
        """
        try:
            value = json.loads(self.run("busctl", "--json=short", "get-property",
                                        "org.freedesktop.systemd1", SERVICE_DBUS_PATH,
                                        interface, name))
        except ValueError as exc:
            raise DeployError("Не удалось разобрать effective systemd unit") from exc
        if (not isinstance(value, dict) or value.get("type") != signature or
                not isinstance(value.get("data"), list)):
            raise DeployError("Не удалось проверить effective systemd unit")
        return value["data"]

    def systemd_text_property(self, interface: str, name: str) -> str:
        """Читает строковое effective свойство unit из D-Bus без fallback.

        Args:
            interface: D-Bus интерфейс service.
            name: Имя строкового свойства.

        Returns:
            Значение загруженного свойства.

        Raises:
            DeployError: Свойство отсутствует или имеет неожиданную форму.
        """
        try:
            value = json.loads(self.run("busctl", "--json=short", "get-property",
                                        "org.freedesktop.systemd1", SERVICE_DBUS_PATH,
                                        interface, name))
        except ValueError as exc:
            raise DeployError("Не удалось разобрать effective systemd unit") from exc
        if (not isinstance(value, dict) or value.get("type") != "s" or
                not isinstance(value.get("data"), str)):
            raise DeployError("Не удалось проверить effective systemd unit")
        return value["data"]

    def require_systemd_boot(self, runtime: Path) -> None:
        """Проверяет порядок drop-in и фактические команду, env и marker guard.

        Args:
            runtime: Внешний файл с закреплённым image tag и Compose путями.

        Raises:
            DeployError: Загруженный unit может запустить канал без безопасного pin.
        """
        dropins = self.run("systemctl", "show", "--property=DropInPaths", "--value", SERVICE).split()
        expected = [str(CHANNELS_DROPIN), str(PINNED_DROPIN)]
        if LEGACY_PINNED_DROPIN.is_file():
            expected.insert(0, str(LEGACY_PINNED_DROPIN))
        if dropins != expected:
            raise DeployError("Порядок systemd drop-in не закрепляет безопасный запуск")
        service_interface = "org.freedesktop.systemd1.Service"
        for property_name, expected in (("ExecStartEx", PINNED_EXEC), ("ExecStopEx", PINNED_STOP)):
            command = self.systemd_property(service_interface, property_name,
                                            "a(sasasttttuii)")
            if (len(command) != 1 or not isinstance(command[0], list) or len(command[0]) != 10 or
                    command[0][:3] != [expected[0], expected, PINNED_EXEC_FLAGS]):
                raise DeployError(f"Effective {property_name} не закрепляет root-private Compose")
        for property_name, expected in (("User", "user"), ("Group", "user"),
                                        ("WorkingDirectory", str(self.checkout))):
            if self.systemd_text_property(service_interface, property_name) != expected:
                raise DeployError(f"Effective {property_name} отличается от проверенного unit")
        environment = self.systemd_property("org.freedesktop.systemd1.Service", "EnvironmentFiles",
                                            "a(sb)")
        if (not 1 <= len(environment) <= 2 or
                any(entry != [str(runtime), False] for entry in environment)):
            raise DeployError("Effective EnvironmentFile не закрепляет runtime pin")
        conditions = self.systemd_property("org.freedesktop.systemd1.Unit", "Conditions",
                                           "a(sbbsi)")
        expected_guard = ["ConditionPathExists", False, True,
                          str(self.state_dir / "deploy-in-progress")]
        if (not 1 <= len(conditions) <= 2 or
                any(not isinstance(entry, list) or len(entry) != 5 or entry[:4] != expected_guard
                    for entry in conditions)):
            raise DeployError("Effective Condition не блокирует запуск при marker")

    def require_private_pin(self) -> None:
        """Требует root-owned каталог 0700 и файлы pin 0600 без symlink файлов.

        Raises:
            DeployError: Внешний pin доступен постороннему пользователю либо подменён.
        """
        expected = ((self.state_dir, stat.S_ISDIR, 0o700),
                    (self.state_dir / "runtime.env", stat.S_ISREG, 0o600),
                    (self.state_dir / "pinned-compose.yml", stat.S_ISREG, 0o600),
                    (self.state_dir / "critical-compose.yml", stat.S_ISREG, 0o600))
        for path, file_type, mode in expected:
            info = path.lstat()
            if (not file_type(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or
                    stat.S_IMODE(info.st_mode) != mode):
                raise DeployError("Внешний pin должен принадлежать root и оставаться приватным")

    def installed(self, old_id: str, old_sha: str) -> None:
        """Требует заранее установленный внешний pin и безопасный boot path."""
        marker = self.state_dir / "deploy-in-progress"
        runtime = self.state_dir / "runtime.env"
        override = self.state_dir / "pinned-compose.yml"
        critical = self.state_dir / "critical-compose.yml"
        dropin = PINNED_DROPIN
        timer_guard = TIMER_GUARD
        backup_guard = BACKUP_GUARD
        if marker.exists() or not all(path.is_file() for path in
                                           (runtime, override, critical, dropin, timer_guard, backup_guard)):
            raise DeployError("Внешний pin/systemd drop-in не установлен или остался marker")
        self.require_private_pin()
        if override.read_bytes() != (SOURCE_ROOT / "deploy/pinned-compose.yml").read_bytes():
            raise DeployError("Внешний Compose override отличается от проверенного шаблона")
        if critical.read_bytes() != (SOURCE_ROOT / "deploy/critical-compose.yml").read_bytes():
            raise DeployError("Critical Compose override отличается от проверенного шаблона")
        if dropin.read_bytes() != (SOURCE_ROOT / "deploy/obs-chat-bot-pinned.conf").read_bytes():
            raise DeployError("Systemd drop-in отличается от проверенного шаблона")
        if (LEGACY_PINNED_DROPIN.exists() and
                LEGACY_PINNED_DROPIN.read_bytes() !=
                (SOURCE_ROOT / LEGACY_PINNED_TEMPLATE).read_bytes()):
            raise DeployError("Прежний systemd drop-in отличается от проверенного шаблона")
        if timer_guard.read_bytes() != (SOURCE_ROOT / "deploy/obs-chat-bot-backup-guard.conf").read_bytes():
            raise DeployError("Backup timer guard отличается от проверенного шаблона")
        if backup_guard.read_bytes() != (SOURCE_ROOT / "deploy/obs-chat-bot-backup-service-guard.conf").read_bytes():
            raise DeployError("Backup service guard отличается от проверенного шаблона")
        tag = prep.pinned_tag(old_sha)
        if self.image(tag)["Id"] != old_id or self.runtime_tag() != tag:
            raise DeployError("Текущий внешний pin не совпадает с работающим image ID")
        self.run("systemd-analyze", "verify", SERVICE, TIMER, BACKUP_SERVICE)
        if any(self.run("systemctl", "show", "--property=NeedDaemonReload", "--value", unit) != "no"
               for unit in (SERVICE, TIMER, BACKUP_SERVICE)):
            raise DeployError("systemd не загрузил установленный drop-in")
        self.require_systemd_boot(runtime)
        self.require_compose_pin(tag)
        self.require_compose_pin(tag, critical=True)
        if self.run("systemctl", "is-active", SERVICE) != "active":
            raise DeployError("Основной systemd service не active")

    def selected_database(self) -> Path:
        """Разрешает рабочий путь из selector; отсутствие v2 сохраняет legacy путь."""
        state = prep.read_state(self.state_dir, self.checkout) or {}
        if "protocol_version" in state or (self.state_dir / "active-db.json").exists():
            side = database_switch.read_selected(self, state)
            return Path(state[side]["host_path"])
        return self.checkout / "data/app.db"

    def runtime_tag(self) -> str:
        """Читает legacy pin либо строго сверяет производный pin выбранной SQLite."""
        value = (self.state_dir / "runtime.env").read_text(encoding="utf-8")
        lines = value.splitlines()
        expected_file = (
            f"COMPOSE_FILE={self.checkout / 'docker-compose.yml'}:"
            f"{self.state_dir / 'pinned-compose.yml'}"
        )
        if (self.state_dir / "active-db.json").exists():
            state = prep.read_state(self.state_dir, self.checkout) or {}
            side = database_switch.read_selected(self, state)
            tag = lines[0].removeprefix("ASSISTANT_IMAGE=") if lines else ""
            env = self.database_env(tag, state[side]["container_path"])
            if lines != [f"{key}={value}" for key, value in env.items()]:
                raise DeployError("runtime.env не соответствует selector")
            return tag
        if len(lines) != 2 or lines[1] != expected_file or not lines[0].startswith("ASSISTANT_IMAGE="):
            raise DeployError("runtime.env имеет неверный формат")
        return lines[0].split("=", 1)[1]

    def set_runtime(self, tag: str, expected_id: str) -> None:
        """Атомарно переключает внешний pin только на проверенный image ID."""
        if self.image(tag)["Id"] != expected_id:
            raise DeployError("Image tag больше не указывает на ожидаемый ID")
        value = (f"ASSISTANT_IMAGE={tag}\n"
                 f"COMPOSE_FILE={self.checkout / 'docker-compose.yml'}:"
                 f"{self.state_dir / 'pinned-compose.yml'}\n")
        _atomic_text(self.state_dir / "runtime.env", value)

    def require_switch_rollout(self) -> None:
        """Закрывает эксплуатационный v2 до реализации recovery и установки snapshot."""
        raise DeployError("SQLite protocol v2 закрыт до шагов 6.4 и 6.5")

    def database_env(self, tag: str, database_path: str) -> dict[str, str]:
        """Формирует отдельный Compose override для обоих выбранных поколений."""
        if re.fullmatch(r"/app/data/app(?:-[0-9a-f]{32})?\.db", database_path) is None:
            raise DeployError("Некорректный runtime путь SQLite")
        env = self.compose_env(tag, critical=True)
        env["COMPOSE_FILE"] += ":" + str(self.state_dir / "database-compose.yml")
        env["ASSISTANT_DATABASE_PATH"] = database_path
        return env

    def set_database_runtime(self, tag: str, expected_id: str, database_path: str) -> None:
        """Закрепляет образ и DATABASE_PATH в производном runtime.env под marker."""
        self.require_snapshot_quiescence()
        if self.image(tag)["Id"] != expected_id:
            raise DeployError("Candidate pin изменился")
        override = (SOURCE_ROOT / "deploy/database-compose.yml").read_text(encoding="utf-8")
        _atomic_text(self.state_dir / "database-compose.yml", override)
        env = self.database_env(tag, database_path)
        _atomic_text(self.state_dir / "runtime.env", "".join(f"{key}={value}\n" for key, value in env.items()))
        if (self.state_dir / "runtime.env").read_text(encoding="utf-8") != "".join(
                f"{key}={value}\n" for key, value in env.items()):
            raise DeployError("Runtime pin не сохранён")

    def require_database_containers(self, ids: dict[str, str], expected_id: str,
                                    database_path: str) -> None:
        """Проверяет оба ещё не запущенных контейнера и единственный data bind."""
        self.require_snapshot_quiescence()
        found = self.containers()
        if (set(found) != set(prep.CHANNELS) or set(ids) != set(prep.CHANNELS)
                or len(set(ids.values())) != len(prep.CHANNELS)):
            raise DeployError("Неполный набор созданных контейнеров")
        for name, item in found.items():
            paths = [entry for entry in item["Config"].get("Env", []) if entry.startswith("DATABASE_PATH=")]
            mounts = item.get("Mounts", [])
            data_mounts = [mount for mount in mounts if mount.get("Destination") == "/app/data"]
            if (item["Id"] != ids[name] or item.get("Image") != expected_id
                    or item["State"].get("Status") != "created"
                    or item.get("RestartCount") != 0
                    or paths != ["DATABASE_PATH=" + database_path]
                    or len(data_mounts) != 1
                    or data_mounts[0].get("Type") != "bind"
                    or data_mounts[0].get("Source") != str(self.checkout / "data")
                    or data_mounts[0].get("RW") is not True
                    or any(mount.get("Destination", "").startswith("/app/data/") for mount in mounts)
                    or any(mount.get("Source") == str(self.checkout / "data")
                           and mount.get("Destination") != "/app/data" for mount in mounts)):
                raise DeployError("Созданный runtime не соответствует выбранной SQLite")
        self.require_no_duplicate_polling()

    def prepare_database_containers(self, tag: str, expected_id: str,
                                    database_path: str) -> dict[str, str]:
        """Создаёт оба канала без start и проверяет фактические mount/env/image."""
        self.require_snapshot_quiescence()
        if self.image(tag)["Id"] != expected_id:
            raise DeployError("Candidate pin изменился перед create")
        env = self.database_env(tag, database_path)
        config = json.loads(self.run("docker", "compose", "config", "--format", "json", env=env))
        for name in prep.CHANNELS:
            service = config["services"][name]
            if (service.get("image") != tag or service.get("pull_policy") != "never"
                    or str(service.get("restart")) != "no"
                    or service.get("environment", {}).get("DATABASE_PATH") != database_path):
                raise DeployError("Compose не закрепляет выбранную SQLite")
        self.run("docker", "compose", "create", "--no-build", "--pull", "never",
                 "--force-recreate", *prep.CHANNELS, env=env)
        ids = {name: item["Id"] for name, item in self.containers().items()}
        self.require_database_containers(ids, expected_id, database_path)
        return ids

    def start_database_containers(self, ids: dict[str, str], expected_id: str,
                                  database_path: str) -> None:
        """Запускает только проверенные ID после повторного чтения durable admission."""
        durable = prep.read_state(self.state_dir, self.checkout)
        if (not durable or durable.get("admission_may_have_started") is not True
                or durable.get("prepared_containers") != ids
                or database_switch.read_selected(self, durable) != "new"
                or durable["new"]["container_path"] != database_path
                or durable["candidate"]["image_id"] != expected_id
                or durable.get("admission") != {"transition_id": durable["transition_id"],
                    "generation_id": durable["new"]["generation_id"],
                    "image_id": expected_id, "sha": durable["candidate"]["sha"]}):
            raise DeployError("Запуск без durable admission запрещён")
        self.require_database_containers(ids, expected_id, database_path)
        self.run("docker", "start", *(ids[name] for name in prep.CHANNELS))

    def marker(self, enabled: bool) -> None:
        """Сохраняет reboot marker до завершения проверенного перехода."""
        path = self.state_dir / "deploy-in-progress"
        if enabled:
            _atomic_text(path, "critical\n")
        else:
            path.unlink(missing_ok=True)

    def unit_state(self, unit: str) -> str:
        """Читает точный ActiveState без вывода содержимого unit."""
        status = self.run("systemctl", "show", "--property=ActiveState", "--value", unit)
        if status not in ("active", "inactive", "activating", "deactivating", "failed"):
            raise DeployError(f"Не удалось проверить systemd unit {unit}")
        return status

    def unit_active(self, unit: str) -> bool:
        """Требует устойчивое active/inactive без трактовки failed как остановки."""
        status = self.unit_state(unit)
        if status not in ("active", "inactive"):
            raise DeployError(f"Systemd unit {unit} не в устойчивом состоянии")
        return status == "active"

    def unit_enabled(self, unit: str) -> bool:
        """Читает enabled/disabled независимо от exit code штатного disabled."""
        result = subprocess.run(("systemctl", "is-enabled", unit), cwd=self.checkout,
                                text=True, capture_output=True, check=False)
        status = result.stdout.strip()
        if (status, result.returncode) not in (("enabled", 0), ("disabled", 1)):
            raise DeployError(f"Неожиданное состояние включения {unit}")
        return status == "enabled"

    def timer_snapshot(self) -> dict[str, bool]:
        """Фиксирует исходные enabled и active для точного восстановления."""
        return {"enabled": self.unit_enabled(TIMER), "active": self.unit_active(TIMER)}

    def timer_suspend(self) -> None:
        """Отключает timer до reboot и ждёт завершения уже запущенного backup."""
        self.run("systemctl", "disable", "--now", TIMER)
        if self.unit_enabled(TIMER):
            raise DeployError("Ночной backup timer не отключён")
        for _ in range(60):
            state = self.unit_state(BACKUP_SERVICE)
            if state == "inactive":
                break
            if state == "failed":
                raise DeployError("Ночной backup service завершился ошибкой")
            time.sleep(1)
        else:
            raise DeployError("Ночной backup service не завершился")

    def timer_restore(self, snapshot: dict[str, bool]) -> None:
        """Возвращает исходные enabled и active после выпуска или recovery."""
        if set(snapshot) != {"enabled", "active"} or any(type(x) is not bool for x in snapshot.values()):
            raise DeployError("Нет достоверного исходного состояния backup timer")
        self.run("systemctl", "enable" if snapshot["enabled"] else "disable", TIMER)
        self.run("systemctl", "start" if snapshot["active"] else "stop", TIMER)
        if self.timer_snapshot() != snapshot:
            raise DeployError("Исходное состояние backup timer не восстановлено")

    def restart_policy(self, value: str) -> None:
        """Меняет Docker restart policy у обоих каналов до reboot-safe stop."""
        if value not in ("no", "unless-stopped"):
            raise DeployError("Некорректная restart policy")
        for container in self.containers().values():
            self.run("docker", "update", f"--restart={value}", container["Id"])
        if any(item["HostConfig"]["RestartPolicy"]["Name"] != value
               for item in self.containers().values()):
            raise DeployError("Docker restart policy не подтверждена")

    def stop_both(self) -> None:
        """Останавливает unit и оба Compose writers, затем проверяет отсутствие работающих."""
        self.run("systemctl", "stop", SERVICE)
        if (self.state_dir / "active-db.json").exists():
            # При повреждённом selector остановка не должна зависеть от его чтения.
            self.run("docker", "stop", *(item["Id"] for item in self.containers().values()))
        else:
            self.run("docker", "compose", "stop", *prep.CHANNELS,
                     env=self.compose_env(self.runtime_tag()))
        if any(item["State"]["Running"] for item in self.containers().values()):
            raise DeployError("Один из writers продолжает работать")

    def start_both(self, tag: str, expected_id: str) -> None:
        """Запускает оба сервиса с единым pin без build и pull."""
        if self.image(tag)["Id"] != expected_id:
            raise DeployError("Image tag изменился перед запуском")
        self.require_compose_pin(tag, critical=True)
        self.run("docker", "compose", "up", "-d", "--no-build", "--pull", "never",
                 "--force-recreate", *prep.CHANNELS, env=self.compose_env(tag, critical=True))
        if any(item["HostConfig"]["RestartPolicy"]["Name"] != "no"
               for item in self.containers().values()):
            raise DeployError("Candidate получил автоматическую Docker restart policy")

    def activate_unit(self) -> None:
        """Возвращает штатный systemd запуск после проверки временных контейнеров."""
        self.run("systemctl", "start", SERVICE)
        if not self.unit_active(SERVICE):
            raise DeployError("Основной systemd service не active после переключения")

    def verify_both(self, expected_id: str, expected_sha: str | None, *, timeout: int = 120) -> None:
        """Ждёт health, OCI revision, нулевых рестартов и признаков двух polling."""
        deadline = time.monotonic() + timeout
        stable_since: float | None = None
        while time.monotonic() < deadline:
            found = self.containers()
            correct = all(
                item.get("Image") == expected_id and item["State"].get("Running") is True and
                item["State"].get("Health", {}).get("Status") == "healthy" and
                item.get("RestartCount") == 0
                for item in found.values()
            )
            revision = (self.image(expected_id).get("Config", {}).get("Labels") or {}).get(
                prep.OCI_REVISION
            )
            if correct and (expected_sha is None or revision == expected_sha):
                logs = {name: self.logs(item["Id"]) for name, item in found.items()}
                if (all(LOG_SIGNALS[name] in value for name, value in logs.items()) and
                        not any("Traceback (most recent call last)" in value or
                                "TelegramConflictError" in value for value in logs.values())):
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= 10:
                        return
                else:
                    stable_since = None
            else:
                stable_since = None
            time.sleep(3)
        raise DeployError("Оба канала не подтвердили health/image/revision/restarts/polling")

    def verify_installed(self, good: dict[str, Any]) -> None:
        """Проверяет устойчивое состояние повторного вызова без пересоздания."""
        tag = good.get("tag")
        image_id = good.get("image_id")
        if not isinstance(tag, str) or not isinstance(image_id, str):
            raise DeployError("Сохранённый last_good неполон")
        if self.runtime_tag() != tag or self.image(tag)["Id"] != image_id or not self.unit_active(SERVICE):
            raise DeployError("Повтор версии обнаружил неполный pin или unit")
        if (self.state_dir / "deploy-in-progress").exists():
            raise DeployError("Повтор версии обнаружил незавершённый marker")
        if any(item.get("Image") != image_id or
               item["State"].get("Health", {}).get("Status") != "healthy" or
               item["HostConfig"]["RestartPolicy"]["Name"] != "unless-stopped"
               for item in self.containers().values()):
            raise DeployError("Повтор версии обнаружил нездоровый канал")

    def migrate(self, image_id: str) -> None:
        """Вызывает отдельный CLI миграции без production env и сети."""
        self.run("docker", "run", "--rm", "--network", "none", "--read-only",
                 "--tmpfs", "/tmp:rw,nosuid,nodev", "-v",
                 f"{self.checkout / 'data'}:/app/data:rw", "--entrypoint", "python",
                 image_id, "-m", "compadre_assistant", "--migrate", "--database-path",
                 "/app/data/app.db")

    def require_baseline(self, good: dict[str, Any], old_sha: str, old_id: str) -> None:
        """Сверяет last_good, внешний pin и OCI revision обоих работающих каналов."""
        if (not isinstance(good, dict) or good.get("sha") != old_sha
                or good.get("image_id") != old_id or good.get("tag") != prep.pinned_tag(old_sha)):
            raise compatibility.PredeployError("SAFE_INSTALLED_LAST_GOOD")
        with compatibility.check("SAFE_INSTALLED_RUNTIME"):
            self.installed(old_id, old_sha)
        if (_current_image(self) != old_id or self.runtime_tag() != good["tag"]
                or self.image(good["tag"])["Id"] != old_id):
            raise compatibility.PredeployError("SAFE_INSTALLED_IMAGE")
        for item in (self.image(old_id), *self.containers().values()):
            if (item.get("Config", {}).get("Labels") or {}).get(prep.OCI_REVISION) != old_sha:
                raise compatibility.PredeployError("SAFE_INSTALLED_REVISION")

    def predeploy(self, selected: SelectedRelease, candidate_id: str, before: list[str],
                  expected: list[str], good: dict[str, Any], old_sha: str, old_id: str) -> None:
        """Проверяет доверенное evidence и приватную online-копию под deploy lock.

        Args:
            selected: Проверенный Release и его точный CI run/attempt.
            candidate_id: Локально собранный неизменяемый ID кандидата.
            before: Исходный журнал SQLite.
            expected: Журнал candidate migrations.
            good: Сохранённый last_good до подготовки.
            old_sha: Фактический исходный SHA checkout.
            old_id: Фактический исходный ID обоих каналов.

        Raises:
            compatibility.PredeployError: База, evidence или копия не подтверждены;
                исключение содержит только безопасный код проверки.
        """
        from tempfile import TemporaryDirectory

        self.require_baseline(good, old_sha, old_id)
        pin_path = self.state_dir / "compatibility-policy.sha"
        policy_sha = compatibility.private_text(pin_path)
        token = compatibility.private_text(self.state_dir / "github-read-token")
        api = compatibility.EvidenceAPI(EXPECTED_REPOSITORY, token)
        with compatibility.check("SAFE_EVIDENCE_READ"):
            evidence = compatibility.verify_evidence(api, selected, policy_sha, old_sha)
        with TemporaryDirectory(prefix="predeploy-", dir=self.state_dir) as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            compatibility.verify_copy(self.selected_database(), directory,
                                      before, expected, evidence["schema_fingerprint"],
                                      lambda work: self.migrate_copy(candidate_id, work))
        # Долгая миграция не сохраняет доверие к отозванному artifact или pin.
        with compatibility.check("SAFE_EVIDENCE_RECHECK"):
            if (compatibility.private_text(pin_path) != policy_sha
                    or compatibility.verify_evidence(api, selected, policy_sha, old_sha) != evidence):
                raise compatibility.PredeployError("SAFE_EVIDENCE_CHANGED")
        self.require_baseline(good, old_sha, old_id)
        self.compatibility_evidence = evidence

    def require_snapshot_quiescence(self) -> None:
        """Проверяет остановку известных production writers и backup под marker."""
        if ((self.state_dir / "deploy-in-progress").read_text(encoding="utf-8") != "critical\n"
                or any(self.unit_state(unit) != "inactive" for unit in (SERVICE, TIMER, BACKUP_SERVICE))
                or self.unit_enabled(TIMER)
                or any(item["State"].get("Running") is not False
                       or item["HostConfig"]["RestartPolicy"]["Name"] != "no"
                       for item in self.containers().values())):
            raise DeployError("Остановка writers и backup для окончательного снимка не подтверждена")

    def migrate_copy(self, image_id: str, directory: Path, *, basename: str = "app.db") -> None:
        """Запускает candidate CLI только с временной копией, без сети и secrets.

        Args:
            image_id: Проверенный неизменяемый ID локального образа.
            directory: Приватный каталог единственной writable копии.
            basename: Имя SQLite-копии в каталоге.

        Raises:
            DeployError: Некорректное имя копии либо отказ мигратора.
        """
        import uuid

        if basename != "app.db" and re.fullmatch(r"app-[0-9a-f]{32}\.db", basename) is None:
            raise DeployError("Некорректное имя SQLite-копии")
        name = "compadre-predeploy-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--name", name, "--network", "none",
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--memory", "512m", "--pids-limit", "128", "--cpus", "1",
                   "--tmpfs", "/tmp:rw,nosuid,nodev", "-v", f"{directory}:/app/data:rw",
                   "--entrypoint", "python", image_id, "-m", "compadre_assistant",
                   "--migrate", "--database-path", f"/app/data/{basename}"]
        environment = {"PATH": "/usr/bin:/bin", "DOCKER_HOST": "unix:///var/run/docker.sock"}
        try:
            result = subprocess.run(command, env=environment, capture_output=True, timeout=180,
                                    check=False)
            if result.returncode:
                raise DeployError("Предварительная миграция завершилась отказом")
        finally:
            # Timeout клиента сам по себе не останавливает Docker-контейнер.
            subprocess.run(["docker", "rm", "-f", name], env=environment,
                           capture_output=True, timeout=30, check=False)


def _state(host: Host, value: dict[str, Any], stage: str, **changes: Any) -> None:
    """Записывает стадию атомарно и печатает только безопасное событие журнала."""
    value.update(changes)
    value["stage"] = stage
    prep.write_state(host.state_dir, host.checkout, value)
    metrics.update(stage=stage)
    print(f"deploy-prod stage={stage}", flush=True)


def _schema_names(checkout: Path) -> list[str]:
    """Читает точный последовательный список SQL-файлов candidate."""
    directory = checkout / "compadre_assistant/core/data/sqlite/migrations"
    names = sorted(path.name for path in directory.glob("*.sql"))
    if not names or [int(name[:4]) for name in names] != list(range(1, len(names) + 1)):
        raise DeployError("Последовательность миграций candidate некорректна")
    if any(not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", name) for name in names):
        raise DeployError("Имя миграции candidate некорректно")
    return names


def _applied_names(database: Path) -> list[str]:
    """Читает только имена применённых миграций из SQLite."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    if [row[0] for row in rows] != list(range(1, len(rows) + 1)):
        raise DeployError("Текущие миграции БД не образуют префикс")
    return [row[1] for row in rows]


def _current_image(host: Host) -> str:
    """Требует два работающих контейнера на одном фактическом image ID."""
    found = host.containers()
    images = {item.get("Image") for item in found.values()}
    if len(images) != 1 or not all(item["State"].get("Running") for item in found.values()):
        raise DeployError("Каналы не работают вместе на одном image ID")
    image_id = images.pop()
    if not isinstance(image_id, str) or not IMAGE_PATTERN.fullmatch(image_id):
        raise DeployError("Некорректный текущий image ID")
    return image_id


def _same_selection(host: Host, selected: SelectedRelease) -> None:
    """Требует полное совпадение повторного GitHub selector с первым снимком."""
    current = (host.select_emergency(selected.version) if selected.kind == "emergency"
               else host.select(selected.version))
    if current != selected:
        raise DeployError("Release, tag или CI изменились после выбора")


def _error_reason(error: Exception) -> str:
    if isinstance(error, (DeployError, compatibility.PredeployError)) and error.code:
        return error.code
    return type(error).__name__


def _prepare_candidate(
    host: Host, version: str, old_sha: str, old_id: str, old_tag: str,
    *, emergency: bool = False, last_good: dict[str, Any] | None = None,
) -> tuple[SelectedRelease, str, str, list[str], list[str]]:
    """Готовит candidate без остановки каналов и восстанавливает checkout при ошибке."""
    try:
        selected = host.select_emergency(version) if emergency else host.select(version)
        metrics.update(sha=selected.sha, run_id=selected.run_id, run_attempt=selected.run_attempt)
        if selected.kind != ("emergency" if emergency else "ordinary"):
            raise DeployError("Тип выбранного Release не соответствует команде")
        if selected.sha == old_sha:
            raise DeployError("Текущий код уже имеет SHA выбранной версии")
        host.fetch(selected)
        _same_selection(host, selected)
        prep.require_clean_checkout(host.checkout, selected.sha)
        with metrics.stage("build"):
            tag, candidate_id = prep.build_pinned_image(host.checkout, selected.sha)
        with compatibility.check("SAFE_CANDIDATE_IMAGE"):
            prep.check_candidate_image(candidate_id, selected.sha)
        database = host.selected_database()
        prep.require_disk_space(host.state_dir, database)
        with compatibility.check("SAFE_SOURCE_SCHEMA"):
            before = _applied_names(database)
            prep.validate_schema(database, before)
        with compatibility.check("SAFE_CANDIDATE_MANIFEST"):
            expected = _schema_names(host.checkout)
        if expected[:len(before)] != before:
            raise compatibility.PredeployError("SAFE_SCHEMA_PREFIX")
        with compatibility.check("SAFE_PREDEPLOY_IO"):
            host.predeploy(selected, candidate_id, before, expected, last_good, old_sha, old_id)
        _same_selection(host, selected)
        prep.require_exact_tag(host.checkout, selected.version, selected.sha)
        prep.require_clean_checkout(host.checkout, selected.sha)
        host.require_compose_pin(old_tag)
        host.require_compose_pin(tag, critical=True)
        host.require_no_duplicate_polling()
        if _current_image(host) != old_id or host.image(old_tag)["Id"] != old_id:
            raise DeployError("Работающий старый image ID изменился перед остановкой")
        return selected, tag, candidate_id, before, expected
    except Exception as error:
        if isinstance(error, compatibility.PredeployError):
            metrics.update(predeploy_reason=error.code)
        try:
            host.restore_checkout(old_sha)
        except Exception as restore_error:
            raise DeployError(f"Подготовка не удалась и checkout не возвращён; "
                              f"нужен ручной разбор: {_error_reason(error)}") from restore_error
        if isinstance(error, compatibility.PredeployError):
            raise DeployError(f"Подготовка не удалась: {error.code}", code=error.code) from error
        raise DeployError(f"Подготовка не удалась: {type(error).__name__}") from error


def _recover(host: Host, state: dict[str, Any]) -> None:
    """Возвращает проверенный старый код, если схема не менялась; иначе закрывает запуск."""
    stage = state.get("stage")
    if "protocol_version" in state or (host.state_dir / "active-db.json").exists():
        if state.get("protocol_version") != database_switch.PROTOCOL_VERSION or stage not in ("success", "rolled_back"):
            _degrade(host, state, "SQLite v2 требует recovery шага 6.4")
            raise DeployError("Legacy recovery не может выбирать SQLite v2")
        try:
            database_switch.read_selected(host, state)
        except Exception as error:
            _degrade(host, state, "SQLite selector или state повреждены")
            raise DeployError("Выбор SQLite не доказан") from error
        return
    if stage == "degraded":
        raise DeployError("Degraded state требует ручного восстановления")
    if stage in (None, "success", "rolled_back", "failed_preflight", "pinned_verified"):
        return
    candidate = state.get("candidate")
    if isinstance(candidate, dict):
        metrics.update(sha=candidate.get("sha"), run_id=candidate.get("run_id"),
                       run_attempt=candidate.get("run_attempt"))
    if stage in ("preparing", "ready") and not (host.state_dir / "deploy-in-progress").exists():
        old_sha = state.get("old_sha")
        old_id = state.get("old_image_id")
        old_tag = state.get("old_tag")
        if not all(isinstance(value, str) for value in (old_sha, old_id, old_tag)):
            _degrade(host, state, "Нет прежнего SHA после прерванной подготовки")
            raise DeployError("Не удалось восстановить checkout после подготовки")
        try:
            if (_current_image(host) != old_id or host.runtime_tag() != old_tag or
                    host.image(old_tag)["Id"] != old_id):
                raise DeployError("Работающие каналы изменились во время подготовки")
            host.restore_checkout(old_sha)
        except Exception as error:
            _degrade(host, state, "Не удалось вернуть checkout после подготовки")
            raise DeployError("Не удалось восстановить checkout после подготовки") from error
        _state(host, state, "failed_preflight", error="Предыдущая подготовка прервана",
               retry_after=(datetime.now(timezone.utc) + COOLDOWN).isoformat())
        return
    old = state.get("old_image_id")
    old_sha = state.get("old_sha")
    old_tag = state.get("old_tag")
    before = state.get("schema_before")
    if not all(isinstance(x, str) for x in (old, old_sha, old_tag)) or not isinstance(before, list):
        _degrade(host, state, "Нет проверенного rollback manifest")
        raise DeployError("Незавершённый выпуск требует ручного восстановления")
    after = state.get("schema_after")
    if isinstance(after, list) and after != before and state.get("migration_attempted") is True:
        # Даже отсутствие новой записи в schema_migrations не доказывает,
        # что SQL не изменил схему до прерывания процесса.
        _degrade(host, state, "Новая миграция могла изменить схему")
        raise DeployError("После попытки новой миграции автоматический возврат запрещён")
    try:
        current_schema = _applied_names(host.checkout / "data/app.db")
    except Exception as error:
        _degrade(host, state, "Не удалось прочитать текущую схему")
        raise DeployError("Текущая схема неизвестна; автоматический возврат запрещён") from error
    if current_schema != before:
        _degrade(host, state, "Схема отличается от rollback manifest")
        raise DeployError("Схема изменилась; автоматический возврат запрещён")
    try:
        prep.validate_schema(host.checkout / "data/app.db", before)
    except Exception as error:
        _degrade(host, state, "SQLite integrity или схема не подтверждены")
        raise DeployError("SQLite требует ручного восстановления") from error
    try:
        host.marker(True)
        host.timer_suspend()
        host.restart_policy("no")
        with metrics.stage("recovery.stop_both"):
            host.stop_both()
        host.set_runtime(old_tag, old)
        host.start_both(old_tag, old)
        with metrics.stage("recovery.verify_both_1"):
            host.verify_both(old, None)
        host.restart_policy("unless-stopped")
        host.marker(False)
        host.activate_unit()
        with metrics.stage("recovery.verify_both_2"):
            host.verify_both(old, None)
        metrics.available()
        host.timer_restore(state["timer_before"])
        host.restore_checkout(old_sha)
        cooldown = ({"retry_after": (datetime.now(timezone.utc) + COOLDOWN).isoformat()}
                    if isinstance(state.get("failed_version"), str) else {})
        _state(host, state, "rolled_back", error="Предыдущий выпуск прерван", **cooldown)
    except Exception as error:
        _degrade(host, state, type(error).__name__)
        raise DeployError("Автоматический возврат не подтвердил оба канала") from error


def _degrade(host: Host, state: dict[str, Any], reason: str) -> None:
    """При отказе возврата пытается остановить оба канала и оставляет marker."""
    for action in (lambda: host.marker(True), host.timer_suspend,
                   lambda: host.restart_policy("no"), host.stop_both):
        try:
            if action == host.stop_both:
                with metrics.stage("degraded.stop_both"):
                    action()
            else:
                action()
        except Exception:
            # Следующую защиту пробуем и при частичном отказе предыдущей.
            continue
    _state(host, state, "degraded", error=reason)


@metrics.tracked
def deploy(host: Host, version: str, *, emergency: bool = False,
           reason: str | None = None) -> None:
    """Развёртывает названный Release или возвращает оба канала на старый образ.

    Args:
        host: Адаптер системных операций production VPS.
        version: Каноническая опубликованная версия.
        emergency: Явный выбор аварийного маршрута.
        reason: Причина решения о развёртывании аварийного выпуска.

    Raises:
        DeployError: Допуск, переключение или проверка завершились отказом.
    """
    _version(version)
    metrics.update(version=version, kind="emergency" if emergency else "ordinary")
    if emergency:
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000 or "\n" in reason or "\r" in reason:
            raise DeployError("Для аварийного выпуска нужна однострочная причина")
        initiator = host.initiator()
    elif reason is not None:
        raise DeployError("Причина допустима только с --emergency")
    else:
        initiator = None
    with prep.deploy_lock(host.state_dir, host.checkout):
        state = prep.read_state(host.state_dir, host.checkout) or {}
        previous_stage = state.get("stage")
        switch_state = state.copy() if "protocol_version" in state else None
        _recover(host, state)
        if switch_state is not None:
            host.require_switch_rollout()
        if previous_stage not in (None, "success", "rolled_back", "failed_preflight", "pinned_verified"):
            raise DeployError("Незавершённый предыдущий выпуск обработан; проверьте состояние перед новым запуском")
        if state.get("stage") == "degraded":
            raise DeployError("Degraded state требует ручного восстановления")
        last_good = state.get("last_good")
        if isinstance(last_good, dict) and last_good.get("version") == version:
            if last_good.get("kind", "ordinary") != ("emergency" if emergency else "ordinary"):
                raise DeployError("Тип установленного Release не соответствует команде")
            host.verify_installed(last_good)
            metrics.update(stage="already_installed", sha=last_good.get("sha"),
                           run_id=last_good.get("ci_run_id"), run_attempt=last_good.get("ci_run_attempt"))
            print("deploy-prod stage=already_installed", flush=True)
            return
        if isinstance(last_good, dict) and _version(version) <= _version(str(last_good.get("version"))):
            raise DeployError("Обычный выпуск должен быть выше последнего успешного")
        if state.get("failed_version") == version and state.get("retry_after"):
            if datetime.fromisoformat(str(state["retry_after"])) > datetime.now(timezone.utc):
                raise DeployError("Повтор неудачного выпуска временно закрыт; нужен ручной разбор")
        try:
            old_id = _current_image(host)
            old_sha = host.run("git", "rev-parse", "HEAD")
            prep.require_clean_checkout(host.checkout, old_sha)
            old_tag = prep.pinned_tag(old_sha)
            host.installed(old_id, old_sha)
            host.require_no_duplicate_polling()
        except Exception as error:
            _state(host, state, "failed_preflight", error=_error_reason(error))
            raise DeployError("Исходное состояние production не прошло preflight") from error
        state.update(old_sha=old_sha, old_image_id=old_id, old_tag=old_tag,
                     failed_version=version)
        state["deployment"] = ({"kind": "emergency", "initiator": initiator,
                                "reason": reason.strip(), "version": version}
                               if emergency else None)
        _state(host, state, "preparing")
        try:
            selected, tag, candidate_id, before, expected = _prepare_candidate(
                host, version, old_sha, old_id, old_tag, emergency=emergency, last_good=last_good,
            )
        except Exception as error:
            try:
                prep.require_clean_checkout(host.checkout, old_sha)
            except Exception:
                # Не теряем rollback manifest при неудачном возврате checkout.
                _state(host, state, "preparing", error=_error_reason(error))
                raise DeployError(f"Checkout не возвращён после подготовки; "
                                  f"нужен recovery: {_error_reason(error)}") from error
            _state(host, state, "failed_preflight", error=_error_reason(error),
                   failed_version=version,
                   retry_after=(datetime.now(timezone.utc) + COOLDOWN).isoformat())
            raise
        database = host.selected_database()
        if emergency:
            state["deployment"].update({
                "sha": selected.sha, "release_id": selected.release_id,
                "published_reason": selected.reason, "workflow": selected.workflow,
                "ci_state": selected.ci_state, "ci_source": selected.ci_source,
                "published_ci_state": selected.published_ci_state,
                "published_ci_source": selected.published_ci_source,
            })
            print(f"deploy-prod АВАРИЙНЫЙ РЕЖИМ version={version} sha={selected.sha} "
                  f"CI={selected.ci_state} deploy_reason={reason.strip()} "
                  f"published_reason={selected.reason}", flush=True)
        try:
            state = {
                "candidate": asdict(selected) | {"image_id": candidate_id, "tag": tag},
                "old_sha": old_sha, "old_image_id": old_id, "old_tag": old_tag,
                "schema_before": before, "schema_after": expected,
                "migration_attempted": False, "last_good": last_good,
                "timer_before": host.timer_snapshot(), "failed_version": version,
                "deployment": state.get("deployment"),
            }
            if switch_state is not None:
                import uuid

                # Selector ещё принадлежит завершённому переходу; проверка
                # предшествует публикации нового transition ID в state.
                with compatibility.check("SAFE_INSTALLED_RUNTIME"):
                    host.require_baseline(last_good, old_sha, old_id)
                side = database_switch.read_selected(host, switch_state)
                state.update(protocol_version=database_switch.PROTOCOL_VERSION,
                             transition_id=uuid.uuid4().hex, old=switch_state[side],
                             controller_sha=compatibility.private_text(host.state_dir / "controller.sha"),
                             admission_may_have_started=False,
                             compatibility_evidence=host.compatibility_evidence)
            _state(host, state, "ready")
        except Exception as error:
            host.restore_checkout(old_sha)
            _state(host, state, "failed_preflight", error=_error_reason(error))
            raise DeployError("Не удалось сохранить состояние подготовки") from error
        critical = False
        try:
            if switch_state is None:
                with compatibility.check("SAFE_INSTALLED_RUNTIME"):
                    host.require_baseline(last_good, old_sha, old_id)
            critical = True
            host.marker(True)
            _state(host, state, "critical")
            host.timer_suspend()
            host.restart_policy("no")
            with metrics.stage("deploy.stop_both"):
                host.stop_both()
            _state(host, state, "writers_stopped")
            if switch_state is not None:
                _atomic_text(host.state_dir / "active-db.json",
                             json.dumps(database_switch.selector(state, "old")) + "\n")
                database_switch.transition(
                    host, state, lambda stage, **changes: _state(host, state, stage, **changes), _atomic_text,
                )
                backup = Path(state["snapshot"]["path"])
                # Backup/systemd ещё не понимают selector. Не снимать marker и
                # не возобновлять timer до отдельной интеграции шага 6.5.
                raise DeployError("SQLite v2 завершил запуск; финализация закрыта до 6.4/6.5")
            else:
                backup = host.state_dir / "backups" / f"{version}-{selected.sha}.db"
                with metrics.stage("backup"):
                    prep.backup_sqlite(database, backup)
                    prep.validate_schema(backup, before)
                _state(host, state, "backup_verified", backup_id=str(backup))
                # Стадия фиксируется до мигратора: прерывание после неё запрещает
                # автоматический запуск старого кода при новом миграционном наборе.
                _state(host, state, "migration_started", migration_attempted=True)
                with metrics.stage("migrations"):
                    host.migrate(candidate_id)
                    prep.validate_schema(database, expected)
                _state(host, state, "schema_verified")
                host.set_runtime(tag, candidate_id)
                _state(host, state, "candidate_pinned")
                host.start_both(tag, candidate_id)
                _state(host, state, "candidate_started")
            with metrics.stage("deploy.verify_both_1"):
                host.verify_both(candidate_id, selected.sha)
            host.restart_policy("unless-stopped")
            host.marker(False)
            host.activate_unit()
            with metrics.stage("deploy.verify_both_2"):
                host.verify_both(candidate_id, selected.sha)
            metrics.available()
            host.timer_restore(state["timer_before"])
            _state(host, state, "success", last_good={
                "version": version, "sha": selected.sha, "image_id": candidate_id,
                "tag": tag, "backup_id": str(backup), "ci_run_id": selected.run_id,
                "ci_run_attempt": selected.run_attempt,
                "kind": selected.kind, "deployment": state.get("deployment"),
            }, error=None)
        except Exception as error:
            if critical:
                try:
                    _state(host, state, "rollback_started", error=_error_reason(error))
                except Exception:
                    # Потеря записи state не отменяет обязательный возврат каналов.
                    state.update(stage="rollback_started", error=_error_reason(error))
                try:
                    _recover(host, state)
                except Exception as rollback_error:
                    raise DeployError("Выпуск и автоматический возврат не прошли; degraded state") from rollback_error
            else:
                host.restore_checkout(old_sha)
                _state(host, state, "failed_preflight", error=_error_reason(error))
            state["failed_version"] = version
            state["retry_after"] = (datetime.now(timezone.utc) + COOLDOWN).isoformat()
            prep.write_state(host.state_dir, host.checkout, state)
            if isinstance(error, compatibility.PredeployError):
                metrics.update(predeploy_reason=error.code)
                raise DeployError(f"Выпуск не установлен: {error.code}", code=error.code) from error
            raise DeployError(f"Выпуск не установлен: {type(error).__name__}") from error


@metrics.tracked
def verify_pinned(host: Host) -> None:
    """Проверяет stop/start и recovery обоих каналов на текущем закреплённом образе.

    Команда предназначена для контролируемой приёмки установки до первого
    выпуска; она не меняет код, схему SQLite или выбранную версию.

    Args:
        host: Адаптер системных операций production VPS.

    Raises:
        DeployError: Pin, проверка обоих каналов или возврат не подтвердились.
    """
    with prep.deploy_lock(host.state_dir, host.checkout):
        state = prep.read_state(host.state_dir, host.checkout) or {}
        previous_stage = state.get("stage")
        _recover(host, state)
        if "protocol_version" in state:
            host.require_switch_rollout()
        if previous_stage not in (None, "success", "rolled_back", "failed_preflight", "pinned_verified"):
            raise DeployError("Предыдущий переход обработан; проверьте состояние перед повтором")
        if state.get("stage") == "degraded":
            raise DeployError("Degraded state требует ручного восстановления")
        try:
            old_id = _current_image(host)
            old_sha = host.run("git", "rev-parse", "HEAD")
            prep.require_clean_checkout(host.checkout, old_sha)
            metrics.update(sha=old_sha)
            old_tag = prep.pinned_tag(old_sha)
            host.installed(old_id, old_sha)
            host.require_no_duplicate_polling()
            schema = _applied_names(host.checkout / "data/app.db")
            prep.validate_schema(host.checkout / "data/app.db", schema)
            state.pop("failed_version", None)
            state.pop("retry_after", None)
            state.update(old_sha=old_sha, old_image_id=old_id, old_tag=old_tag,
                         schema_before=schema, timer_before=host.timer_snapshot())
            _state(host, state, "ready")
        except Exception as error:
            _state(host, state, "failed_preflight", error=type(error).__name__)
            raise DeployError("Текущий pin не прошёл проверку") from error
        try:
            host.marker(True)
            _state(host, state, "critical")
            host.timer_suspend()
            host.restart_policy("no")
            with metrics.stage("pin.stop_both"):
                host.stop_both()
            _state(host, state, "writers_stopped")
            host.set_runtime(old_tag, old_id)
            host.start_both(old_tag, old_id)
            with metrics.stage("pin.verify_both_1"):
                host.verify_both(old_id, None)
            host.restart_policy("unless-stopped")
            host.marker(False)
            host.activate_unit()
            with metrics.stage("pin.verify_both_2"):
                host.verify_both(old_id, None)
            metrics.available()
            host.timer_restore(state["timer_before"])
            _state(host, state, "pinned_verified", error=None)
        except Exception as error:
            try:
                _state(host, state, "rollback_started", error=type(error).__name__)
            except Exception:
                state.update(stage="rollback_started", error=type(error).__name__)
            try:
                _recover(host, state)
            except Exception as rollback_error:
                raise DeployError("Проверка pin и возврат не прошли; degraded state") from rollback_error
            raise DeployError("Проверка pin не прошла; оба канала возвращены") from error


def main() -> None:
    """Обрабатывает выпуск по версии или проверку текущего pin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?")
    parser.add_argument("--emergency", action="store_true", help="явный аварийный выпуск")
    parser.add_argument("--reason", help="обязательная причина аварийного развёртывания")
    parser.add_argument("--verify-pinned", action="store_true",
                        help="контролируемый stop/start обоих каналов на текущем pin")
    args = parser.parse_args()
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise DeployError("Контроллер рассчитан на запуск через sudo на Linux VPS")
        if args.verify_pinned == (args.version is not None):
            raise DeployError("Укажите версию или --verify-pinned")
        if args.verify_pinned and (args.emergency or args.reason is not None):
            raise DeployError("Проверка pin несовместима с аварийным режимом")
        if args.verify_pinned:
            verify_pinned(Host())
        else:
            deploy(Host(), args.version, emergency=args.emergency, reason=args.reason)
    except DeployError as error:
        print(f"deploy-prod: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except Exception as error:
        print(f"deploy-prod: неожиданный отказ {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
