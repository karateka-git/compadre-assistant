"""Явные преобразования личного выбора между domain и SQLite."""

from compadre_assistant.core.data.sqlite.capability_preference_dtos import (
    UserCapabilityPreferenceDto,
)
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference


def capability_preference_to_dto(
    preference: UserCapabilityPreference,
) -> UserCapabilityPreferenceDto:
    """Преобразует смысловой boolean в SQLite INTEGER."""
    return UserCapabilityPreferenceDto(
        preference.app_user_id, preference.capability_id, int(preference.enabled)
    )


def capability_preference_from_dto(
    dto: UserCapabilityPreferenceDto,
) -> UserCapabilityPreference:
    """Восстанавливает явный личный выбор из SQLite DTO."""
    return UserCapabilityPreference(dto.app_user_id, dto.capability_id, bool(dto.enabled))
