"""Общее SQLite-хранилище режима пользователя в конкретном чате."""

import sqlite3

from obs_chat_bot.core.data.sqlite.chat_mode_dtos import ChatModeDto
from obs_chat_bot.core.data.sqlite.chat_mode_mappers import chat_mode_from_dto, chat_mode_to_dto
from obs_chat_bot.core.domain.chat_modes import ChatMode, ChatModeKey, ChatModeRepository


class SQLiteChatModeRepository(ChatModeRepository):
    """Разделяет режимы связанных каналов и восстанавливает их между процессами."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, key: ChatModeKey) -> ChatMode | None:
        """Читает режим по полному адресу пользователя и чата."""
        row = self._connection.execute(
            "SELECT app_user_id, channel, chat_id, capability_id, updated_at "
            "FROM active_chat_modes WHERE app_user_id = ? AND channel = ? AND chat_id = ?",
            (key.app_user_id, key.channel, key.chat_id),
        ).fetchone()
        return chat_mode_from_dto(ChatModeDto(**dict(row))) if row is not None else None

    def save(self, mode: ChatMode) -> None:
        """Атомарно заменяет выбор конкретного чата."""
        dto = chat_mode_to_dto(mode)
        with self._connection:
            self._connection.execute(
                "INSERT INTO active_chat_modes (app_user_id, channel, chat_id, capability_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(app_user_id, channel, chat_id) DO UPDATE SET "
                "capability_id = excluded.capability_id, updated_at = CURRENT_TIMESTAMP",
                (dto.app_user_id, dto.channel, dto.chat_id, dto.capability_id),
            )

    def activate_if_absent(self, mode: ChatMode) -> ChatMode:
        """Создаёт первый режим, сохраняя выбор другого процесса при гонке.

        Args:
            mode: Начальный выбор для указанного чата.

        Returns:
            Сохранённый выбор, включая уже существовавший режим.

        Raises:
            RuntimeError: Если записанный режим не удалось прочитать.
        """
        dto = chat_mode_to_dto(mode)
        with self._connection:
            self._connection.execute(
                "INSERT INTO active_chat_modes (app_user_id, channel, chat_id, capability_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(app_user_id, channel, chat_id) DO NOTHING",
                (dto.app_user_id, dto.channel, dto.chat_id, dto.capability_id),
            )
            saved = self.get(mode.key)
        if saved is None:
            raise RuntimeError("Saved chat mode could not be read")
        return saved

    def clear(self, key: ChatModeKey) -> None:
        """Удаляет маршрут конкретного чата без удаления данных возможности."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM active_chat_modes WHERE app_user_id = ? AND channel = ? AND chat_id = ?",
                (key.app_user_id, key.channel, key.chat_id),
            )
