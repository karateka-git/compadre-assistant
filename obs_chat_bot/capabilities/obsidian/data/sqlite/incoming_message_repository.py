from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.application.articles.incoming_messages import (
    ArticleIncomingMessage,
    SavedIncomingMessage,
)
from obs_chat_bot.capabilities.obsidian.application.articles.ports import IncomingMessageRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.incoming_message_mappers import (
    incoming_message_dto_from_message,
    saved_incoming_message_from_row,
)


INCOMING_MESSAGE_COLUMNS = """
    id,
    article_id,
    app_user_id,
    channel,
    chat_id,
    message_id,
    message_text,
    received_at
"""


class SQLiteIncomingMessageRepository(IncomingMessageRepository):
    """Изолирует SQL-операции с таблицей `incoming_messages`.

    Args:
        connection: Соединение SQLite, созданное через `connect_database`.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, message: ArticleIncomingMessage) -> SavedIncomingMessage:
        """Сохраняет входящее сообщение или возвращает уже существующее.

        Args:
            message: Нормализованное сообщение из presentation-слоя.

        Returns:
            Сохранённое сообщение с ID записи.

        Raises:
            RuntimeError: Если запись не удалось прочитать после сохранения.
        """
        dto = incoming_message_dto_from_message(message)

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO incoming_messages (
                    channel,
                    app_user_id,
                    chat_id,
                    message_id,
                    message_text
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel, chat_id, message_id) DO NOTHING
                """,
                (
                    dto.channel,
                    dto.app_user_id,
                    dto.chat_id,
                    dto.message_id,
                    dto.message_text,
                ),
            )

        saved = self.find_by_external_id(
            channel=message.channel,
            chat_id=message.chat_id,
            message_id=message.message_id,
        )
        if saved is None:
            raise RuntimeError("Saved incoming message could not be read")
        return saved

    def link_to_article(
        self,
        *,
        incoming_message_id: int,
        article_id: int,
    ) -> SavedIncomingMessage | None:
        """Привязывает сохранённое входящее сообщение к статье.

        Args:
            incoming_message_id: ID сохранённого входящего сообщения.
            article_id: ID статьи, созданной или найденной после обработки URL.

        Returns:
            Обновлённое сообщение или `None`, если запись не найдена.
        """
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE incoming_messages
                SET article_id = ?
                WHERE id = ?
                """,
                (article_id, incoming_message_id),
            )

        return self.get_by_id(incoming_message_id) if cursor.rowcount else None

    def get_by_id(self, incoming_message_id: int) -> SavedIncomingMessage | None:
        """Возвращает входящее сообщение по ID.

        Args:
            incoming_message_id: Идентификатор сообщения в SQLite.

        Returns:
            Найденное сообщение или `None`.
        """
        row = self._connection.execute(
            f"SELECT {INCOMING_MESSAGE_COLUMNS} FROM incoming_messages WHERE id = ?",
            (incoming_message_id,),
        ).fetchone()
        return saved_incoming_message_from_row(row) if row is not None else None

    def find_by_external_id(
        self,
        *,
        channel: str,
        chat_id: str,
        message_id: str,
    ) -> SavedIncomingMessage | None:
        """Ищет сообщение по идентификаторам внешнего канала.

        Args:
            channel: Имя канала, например `telegram`.
            chat_id: ID чата во внешнем канале.
            message_id: ID сообщения во внешнем канале.

        Returns:
            Найденное сообщение или `None`.
        """
        row = self._connection.execute(
            f"""
            SELECT {INCOMING_MESSAGE_COLUMNS}
            FROM incoming_messages
            WHERE channel = ? AND chat_id = ? AND message_id = ?
            """,
            (channel, chat_id, message_id),
        ).fetchone()
        return saved_incoming_message_from_row(row) if row is not None else None
