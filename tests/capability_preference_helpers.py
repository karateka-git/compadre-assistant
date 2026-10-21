"""Подменное хранилище личного выбора для тестов маршрутизации."""

from compadre_assistant.core.domain.capability_preferences import (
    UserCapabilityPreference,
    UserCapabilityPreferenceRepository,
)


class MemoryCapabilityPreferences(UserCapabilityPreferenceRepository):
    """Хранит явные настройки и считает чтения допуска события."""

    def __init__(self) -> None:
        """Создаёт независимое хранилище настроек."""
        self.rows: dict[tuple[int, str], bool] = {}
        self.reads = 0

    def get_for_user(self, app_user_id: int) -> tuple[UserCapabilityPreference, ...]:
        """Возвращает явные настройки пользователя и фиксирует чтение."""
        self.reads += 1
        return tuple(
            UserCapabilityPreference(owner, capability_id, enabled)
            for (owner, capability_id), enabled in self.rows.items()
            if owner == app_user_id
        )

    def upsert(self, preference: UserCapabilityPreference) -> bool | None:
        """Заменяет только ключ конкретного пользователя и возможности."""
        key = (preference.app_user_id, preference.capability_id)
        previous = self.rows.get(key)
        self.rows[key] = preference.enabled
        return previous
