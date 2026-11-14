"""Читает настройки доверенных профилей и источника отчёта Admin."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
import re
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from compadre_assistant.capabilities.admin.domain.cost_report import DEFAULT_REPORT_TIMEZONE


_TRUSTED_IDS_ENV = "ADMIN_TRUSTED_APP_USER_IDS"
_TIMEWEB_TOKEN_ENV = "ADMIN_TIMEWEB_API_TOKEN"
_TIMEWEB_KEY_ENV = "ADMIN_TIMEWEB_KEY_ID"
_TIMEWEB_CURRENCY_ENV = "ADMIN_TIMEWEB_CURRENCY"
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")
_MAX_APP_USER_ID = 2**63 - 1  # Внутренний ключ SQLite хранится как знаковое целое.


class AdminConfigError(ValueError):
    """Сообщает о неверной настройке режима администрирования."""


@dataclass(frozen=True, slots=True)
class AdminTimewebConfig:
    """Содержит секрет API аккаунта, ID ключа AI Gateway и валюту отчёта."""

    api_token: str = field(repr=False)
    key_id: UUID
    currency: str


def load_admin_timeweb_config(environ: Mapping[str, str] | None = None) -> AdminTimewebConfig | None:
    """Проверяет настройки источника статистики без обращения к Timeweb.

    Args:
        environ: Подменное окружение либо окружение текущего процесса.

    Returns:
        Настройки источника или None, если токен и ID ключа отсутствуют.

    Raises:
        AdminConfigError: Настройка неполная или содержит недопустимое значение.
    """
    source = os.environ if environ is None else environ
    token = source.get(_TIMEWEB_TOKEN_ENV, "").strip()
    raw_key = source.get(_TIMEWEB_KEY_ENV, "").strip()
    if not token and not raw_key:
        return None
    if not token or not raw_key:
        raise AdminConfigError(f"Для отчёта вместе задаются {_TIMEWEB_TOKEN_ENV} и {_TIMEWEB_KEY_ENV}.")
    if not token.isascii() or not token.isprintable() or " " in token:
        raise AdminConfigError(f"{_TIMEWEB_TOKEN_ENV} должен содержать токен без пробелов.")
    try:
        key_id = UUID(raw_key)
    except ValueError:
        raise AdminConfigError(f"{_TIMEWEB_KEY_ENV} должен содержать UUID ключа шлюза.") from None
    if str(key_id) != raw_key.lower():
        raise AdminConfigError(f"{_TIMEWEB_KEY_ENV} должен содержать UUID ключа шлюза.")
    currency = source.get(_TIMEWEB_CURRENCY_ENV, "").strip().upper() or "RUB"
    if _CURRENCY_PATTERN.fullmatch(currency) is None:
        raise AdminConfigError(f"{_TIMEWEB_CURRENCY_ENV} должен содержать трёхбуквенный код валюты аккаунта.")
    return AdminTimewebConfig(token, key_id, currency)


def load_admin_trusted_app_user_ids(environ: Mapping[str, str] | None = None) -> frozenset[int]:
    """Читает допущенные AppUser ID из списка через запятую.

    Args:
        environ: Подменное окружение либо окружение текущего процесса.

    Returns:
        Множество доверенных внутренних ID. Пустая настройка закрывает доступ.

    Raises:
        AdminConfigError: Список содержит пустой, повторный или некорректный ID.
    """
    source = os.environ if environ is None else environ
    raw_ids = source.get(_TRUSTED_IDS_ENV, "").strip()
    if not raw_ids:
        return frozenset()
    trusted_ids = set()
    for part in raw_ids.split(","):
        value = part.strip()
        if len(value) > 19 or not value.isascii() or not value.isdecimal() or value.startswith("0"):
            raise AdminConfigError(f"{_TRUSTED_IDS_ENV} должен содержать положительные ID через запятую.")
        app_user_id = int(value)
        if app_user_id > _MAX_APP_USER_ID or app_user_id in trusted_ids:
            raise AdminConfigError(f"{_TRUSTED_IDS_ENV} содержит повторный ID или ID вне диапазона SQLite.")
        trusted_ids.add(app_user_id)
    return frozenset(trusted_ids)


def load_admin_profile() -> frozenset[int]:
    """Загружает доверенные профили и проверяет доступность пояса по умолчанию.

    Returns:
        Набор AppUser ID, доступный на время работы текущего процесса.

    Raises:
        AdminConfigError: Список ID некорректен или недоступна база часовых поясов.
    """
    trusted_ids = load_admin_trusted_app_user_ids()
    try:
        ZoneInfo(DEFAULT_REPORT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        raise AdminConfigError(f"Данные часового пояса {DEFAULT_REPORT_TIMEZONE} недоступны.") from error
    return trusted_ids
