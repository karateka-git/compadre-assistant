"""SQLite-хранилище явных настроек встроенных возможностей."""

import sqlite3

from compadre_assistant.core.data.sqlite.capability_preference_dtos import (
    UserCapabilityPreferenceDto,
)
from compadre_assistant.core.data.sqlite.capability_preference_mappers import (
    capability_preference_from_dto,
    capability_preference_to_dto,
)
from compadre_assistant.core.domain.capability_preferences import (
    UserCapabilityPreference,
    UserCapabilityPreferenceRepository,
)


class SQLiteUserCapabilityPreferenceRepository(UserCapabilityPreferenceRepository):
    """Читает AppUser-настройки и атомарно меняет только один ID."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Принимает соединение с включёнными SQLite foreign keys."""
        self._connection = connection

    def get_for_user(self, app_user_id: int) -> tuple[UserCapabilityPreference, ...]:
        """Возвращает все явные строки AppUser без интерпретации каталога."""
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        rows = self._connection.execute(
            "SELECT app_user_id, capability_id, enabled "
            "FROM user_capability_preferences WHERE app_user_id = ? ORDER BY capability_id",
            (app_user_id,),
        ).fetchall()
        return tuple(
            capability_preference_from_dto(UserCapabilityPreferenceDto(*row))
            for row in rows
        )

    def upsert(self, preference: UserCapabilityPreference) -> bool | None:
        """Сериализует чтение прежнего значения и запись одного ключа.

        Args:
            preference: Явный выбор конкретного AppUser и возможности.

        Returns:
            Прежний явный boolean или None при отсутствии строки.

        Raises:
            sqlite3.Error: SQLite не смогла зафиксировать изменение.
        """
        dto = capability_preference_to_dto(preference)
        with self._connection:
            # Lock до чтения сохраняет смысл ответа «уже» при параллельных командах.
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT enabled FROM user_capability_preferences "
                "WHERE app_user_id = ? AND capability_id = ?",
                (dto.app_user_id, dto.capability_id),
            ).fetchone()
            self._connection.execute(
                "INSERT INTO user_capability_preferences (app_user_id, capability_id, enabled) "
                "VALUES (?, ?, ?) ON CONFLICT(app_user_id, capability_id) "
                "DO UPDATE SET enabled = excluded.enabled",
                (dto.app_user_id, dto.capability_id, dto.enabled),
            )
            return bool(row[0]) if row is not None else None
