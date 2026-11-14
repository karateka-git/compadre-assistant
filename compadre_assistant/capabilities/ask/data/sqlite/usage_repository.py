"""Сохраняет метаданные вызовов Ask в SQLite."""

from pathlib import Path

from compadre_assistant.capabilities.ask.data.sqlite.usage_mappers import usage_observation_to_dto
from compadre_assistant.capabilities.ask.domain.usage import UsageObservation, UsageObservationWriter
from compadre_assistant.core.data.sqlite.connection import connect_database


class SQLiteUsageRepository(UsageObservationWriter):
    """Хранит метаданные вызовов на коротких независимых соединениях.

    Args:
        database_path: Путь к общей базе с применёнными миграциями.
    """

    def __init__(self, database_path: Path) -> None:
        """Сохраняет путь для открытия соединений в потоке записи."""
        self._database_path = database_path

    def store(self, observation: UsageObservation) -> bool:
        """Атомарно сохраняет первое наблюдение по ключу вызова.

        Args:
            observation: Метаданные вызова с исходным ключом.

        Returns:
            True для новой записи или False, если ключ уже использован.

        Raises:
            sqlite3.Error: Не удалось выполнить или зафиксировать запись.
        """
        dto = usage_observation_to_dto(observation)
        with connect_database(self._database_path) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO ask_usage (call_id, started_at_us, provider, actual_model, tokens_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(call_id) DO NOTHING",
                (dto.call_id, dto.started_at_us, dto.provider, dto.actual_model, dto.tokens_json),
            )
            return cursor.rowcount == 1
