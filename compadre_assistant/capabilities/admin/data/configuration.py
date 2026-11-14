"""Читает операторскую настройку доверенных профилей администрирования."""

from collections.abc import Mapping
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from compadre_assistant.capabilities.admin.domain.cost_report import DEFAULT_REPORT_TIMEZONE


_TRUSTED_IDS_ENV = "ADMIN_TRUSTED_APP_USER_IDS"
_MAX_APP_USER_ID = 2**63 - 1  # Внутренний ключ SQLite хранится как знаковое целое.


class AdminConfigError(ValueError):
    """Сообщает о неверной настройке режима администрирования."""


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
