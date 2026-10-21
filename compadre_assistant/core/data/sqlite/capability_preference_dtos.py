"""Формат явных личных настроек в SQLite."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserCapabilityPreferenceDto:
    """Передаёт строку настроек с SQLite-представлением boolean."""

    app_user_id: int
    capability_id: str
    enabled: int
