"""Общее SQLite-хранилище режима пользователя в конкретном чате."""

import sqlite3

from compadre_assistant.core.data.sqlite.chat_mode_dtos import ChatModeDto
from compadre_assistant.core.data.sqlite.chat_mode_mappers import (
    chat_mode_from_dto, chat_mode_state_from_dto, chat_mode_to_dto,
)
from compadre_assistant.core.domain.chat_modes import (
    ChatMode, ChatModeKey, ChatModeRepository, ChatModeState,
)


class SQLiteChatModeRepository(ChatModeRepository):
    """Разделяет режимы связанных каналов и восстанавливает их между процессами."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, key: ChatModeKey) -> ChatMode | None:
        """Читает режим по полному адресу пользователя и чата."""
        row = self._connection.execute(
            "SELECT app_user_id, channel, chat_id, capability_id, revision, updated_at "
            "FROM active_chat_modes WHERE app_user_id = ? AND channel = ? AND chat_id = ? "
            "AND capability_id IS NOT NULL",
            (key.app_user_id, key.channel, key.chat_id),
        ).fetchone()
        return chat_mode_from_dto(ChatModeDto(**dict(row))) if row is not None else None

    def get_state(self, key: ChatModeKey) -> ChatModeState:
        """Читает ревизию даже после очистки маршрута."""
        row = self._connection.execute(
            "SELECT app_user_id, channel, chat_id, capability_id, revision, updated_at "
            "FROM active_chat_modes WHERE app_user_id = ? AND channel = ? AND chat_id = ?",
            (key.app_user_id, key.channel, key.chat_id),
        ).fetchone()
        return (
            chat_mode_state_from_dto(ChatModeDto(**dict(row)))
            if row is not None else ChatModeState(key, None, 0)
        )

    def replace_if_revision(
        self, key: ChatModeKey, expected_revision: int, capability_id: str | None
    ) -> bool:
        """Сравнивает ревизию и меняет маршрут в короткой write-транзакции.

        BEGIN IMMEDIATE сериализует read/modify/write между отдельными
        SQLite-соединениями TG и VK без удержания блокировки на обработчике.
        """
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        if capability_id is not None and not capability_id.strip():
            raise ValueError("capability_id must not be empty")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            current = self.get_state(key)
            if current.revision != expected_revision:
                return False
            if expected_revision == 0:
                self._connection.execute(
                    "INSERT INTO active_chat_modes "
                    "(app_user_id, channel, chat_id, capability_id, revision) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (key.app_user_id, key.channel, key.chat_id, capability_id),
                )
            else:
                self._connection.execute(
                    "UPDATE active_chat_modes SET capability_id = ?, "
                    "revision = revision + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE app_user_id = ? AND channel = ? AND chat_id = ?",
                    (capability_id, key.app_user_id, key.channel, key.chat_id),
                )
            return True

    def save(self, mode: ChatMode) -> None:
        """Атомарно заменяет выбор конкретного чата."""
        dto = chat_mode_to_dto(mode)
        with self._connection:
            self._connection.execute(
                "INSERT INTO active_chat_modes (app_user_id, channel, chat_id, capability_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(app_user_id, channel, chat_id) DO UPDATE SET "
                "capability_id = excluded.capability_id, revision = revision + 1, "
                "updated_at = CURRENT_TIMESTAMP",
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
                "VALUES (?, ?, ?, ?) ON CONFLICT(app_user_id, channel, chat_id) DO UPDATE SET "
                "capability_id = excluded.capability_id, revision = revision + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE capability_id IS NULL",
                (dto.app_user_id, dto.channel, dto.chat_id, dto.capability_id),
            )
            saved = self.get(mode.key)
        if saved is None:
            raise RuntimeError("Saved chat mode could not be read")
        return saved

    def clear(self, key: ChatModeKey) -> None:
        """Очищает маршрут, оставляя ревизию для защиты от позднего результата."""
        with self._connection:
            self._connection.execute(
                "INSERT INTO active_chat_modes (app_user_id, channel, chat_id, capability_id) "
                "VALUES (?, ?, ?, NULL) ON CONFLICT(app_user_id, channel, chat_id) "
                "DO UPDATE SET capability_id = NULL, revision = revision + 1, "
                "updated_at = CURRENT_TIMESTAMP",
                (key.app_user_id, key.channel, key.chat_id),
            )
