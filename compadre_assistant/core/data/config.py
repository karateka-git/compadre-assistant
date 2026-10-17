"""Чтение общих настроек приложения, каналов и включённых возможностей."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


REQUIRED_ENV_VARS = (
    "APP_ENV",
    "DATABASE_PATH",
)


class ConfigError(ValueError):
    """Сообщает об отсутствующей или некорректной настройке приложения."""


@dataclass(frozen=True)
class AppConfig:
    """Содержит настройки приложения, подготовленные для composition root."""

    app_env: str
    database_path: Path
    telegram_bot_token: str
    telegram_proxy_url: str | None = None
    app_debug: bool = False
    vk_bot_token: str = ""
    vk_group_id: int | None = None
    enabled_capabilities: tuple[str, ...] = ("obsidian",)
    selected_channels: tuple[str, ...] = ("telegram",)

    @property
    def data_dir(self) -> Path:
        """Возвращает каталог SQLite и локальных служебных файлов."""
        return self.database_path.parent

    def safe_summary(self) -> dict[str, str]:
        """Возвращает диагностические настройки без значений credentials."""
        return {
            "app_env": self.app_env,
            "enabled_capabilities": ",".join(self.enabled_capabilities),
            "selected_channels": ",".join(self.selected_channels),
            "database_path": str(self.database_path),
            "telegram_bot_token": _presence(self.telegram_bot_token),
            "telegram_proxy_url": _presence(self.telegram_proxy_url or ""),
            "app_debug": str(self.app_debug).lower(),
            "vk_bot_token": _presence(self.vk_bot_token),
            "vk_group_id": (
                str(self.vk_group_id)
                if self.vk_group_id is not None
                else "missing"
            ),
        }


def load_database_path() -> Path:
    """Читает только путь SQLite из окружения и локального `.env`.

    Returns:
        Путь БД для отдельного запуска миграций без настроек каналов.

    Raises:
        ConfigError: DATABASE_PATH отсутствует или содержит только пробелы.
    """
    if load_dotenv is not None:
        load_dotenv()
    value = os.getenv("DATABASE_PATH", "").strip()
    if not value:
        raise ConfigError("Missing required environment variable: DATABASE_PATH")
    return Path(value)


def load_config(*, channels: tuple[str, ...] | None = None) -> AppConfig:
    """Читает настройки окружения и локальный `.env`, если доступен dotenv.

    Args:
        channels: Каналы текущей команды; `None` читает ENABLED_CHANNELS.

    Returns:
        Настройки выбранных каналов и включённых возможностей.

    Raises:
        ConfigError: Обязательные настройки отсутствуют или некорректны.
    """
    if load_dotenv is not None:
        load_dotenv()

    selected_channels = (
        parse_channels(os.getenv("ENABLED_CHANNELS", "telegram,vk"))
        if channels is None else channels
    )
    if len(set(selected_channels)) != len(selected_channels) or any(
        channel not in {"telegram", "vk"} for channel in selected_channels
    ):
        raise ConfigError("Некорректный список выбранных каналов")
    required = list(REQUIRED_ENV_VARS)
    if "telegram" in selected_channels:
        required.append("TELEGRAM_BOT_TOKEN")
    if "vk" in selected_channels:
        required.extend(("VK_BOT_TOKEN", "VK_GROUP_ID"))
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        joined = ", ".join(missing)
        raise ConfigError(f"Missing required environment variables: {joined}")

    enabled_capabilities = _get_enabled_capabilities()
    return AppConfig(
        app_env=_get_required("APP_ENV"),
        database_path=Path(_get_required("DATABASE_PATH")),
        telegram_bot_token=(
            os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if "telegram" in selected_channels else ""
        ),
        telegram_proxy_url=(
            _get_optional_telegram_proxy_url()
            if "telegram" in selected_channels else None
        ),
        app_debug=_get_bool("APP_DEBUG", default=False),
        vk_bot_token=(
            os.getenv("VK_BOT_TOKEN", "").strip()
            if "vk" in selected_channels else ""
        ),
        vk_group_id=_get_optional_int("VK_GROUP_ID") if "vk" in selected_channels else None,
        enabled_capabilities=enabled_capabilities,
        selected_channels=selected_channels,
    )


def parse_channels(value: str) -> tuple[str, ...]:
    """Разбирает непустой список явно выбранных адаптеров.

    Args:
        value: Имена каналов, разделённые запятыми.

    Returns:
        Выбранные каналы в порядке запуска.

    Raises:
        ConfigError: Список пуст, содержит повтор или неизвестный канал.
    """
    channels = tuple(part.strip() for part in value.split(",") if part.strip())
    if not channels or len(set(channels)) != len(channels) or any(
        channel not in {"telegram", "vk"} for channel in channels
    ):
        raise ConfigError(
            "ENABLED_CHANNELS/--channels: ожидаются telegram и/или vk без повторов"
        )
    return channels


def _get_required(name: str) -> str:
    value = os.getenv(name)
    if value is None:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _presence(value: str) -> str:
    return "set" if value else "missing"


def _get_optional_telegram_proxy_url() -> str | None:
    """Читает адрес Telegram-прокси без включения credentials в сообщения ошибок.

    Returns:
        Адрес прокси либо `None` для прямого подключения.

    Raises:
        ConfigError: Если адрес или протокол прокси некорректен.
    """
    value = os.getenv("TELEGRAM_PROXY_URL", "").strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ConfigError("TELEGRAM_PROXY_URL has invalid format") from error
    if (
        parsed.scheme not in {"socks5", "socks4", "http"}
        or not parsed.hostname
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("TELEGRAM_PROXY_URL has invalid format")
    return value


def _get_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Environment variable {name} must be boolean")


def _get_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigError(f"Environment variable {name} must be integer") from error
    if parsed <= 0:
        raise ConfigError(f"Environment variable {name} must be positive")
    return parsed


def _get_enabled_capabilities() -> tuple[str, ...]:
    """Читает явный список ID; пустая строка отключает все возможности."""
    identifiers = tuple(
        part.strip()
        for part in os.getenv("ENABLED_CAPABILITIES", "obsidian").split(",")
        if part.strip()
    )
    if len(set(identifiers)) != len(identifiers):
        raise ConfigError("Конфликт ID в ENABLED_CAPABILITIES")
    if any(re.fullmatch(r"[a-z][a-z0-9_-]*", identifier) is None for identifier in identifiers):
        raise ConfigError("Некорректный ID в ENABLED_CAPABILITIES")
    return identifiers
