from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.articles.incoming_messages import (
    ArticleIncomingMessage,
    SavedIncomingMessage,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.incoming_message_dtos import IncomingMessageDto


def incoming_message_dto_from_message(
    message: ArticleIncomingMessage,
) -> IncomingMessageDto:
    """Преобразует application-модель сообщения в SQLite DTO.

    Args:
        message: Нормализованное входящее сообщение.

    Returns:
        DTO с полями, подходящими для вставки в таблицу `incoming_messages`.
    """
    return IncomingMessageDto(
        channel=message.channel,
        app_user_id=message.app_user_id,
        chat_id=message.chat_id,
        message_id=message.message_id,
        message_text=message.text,
    )


def incoming_message_dto_from_row(row: sqlite3.Row) -> IncomingMessageDto:
    """Преобразует строку SQLite в DTO входящего сообщения.

    Args:
        row: Строка SQLite с колонками таблицы `incoming_messages`.

    Returns:
        DTO, отражающий сохранённую строку.
    """
    return IncomingMessageDto(
        id=row["id"],
        article_id=row["article_id"],
        channel=row["channel"],
        app_user_id=row["app_user_id"],
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        message_text=row["message_text"],
        received_at=row["received_at"],
    )


def saved_incoming_message_from_dto(
    dto: IncomingMessageDto,
) -> SavedIncomingMessage:
    """Преобразует SQLite DTO в application-модель сохранённого сообщения.

    Args:
        dto: Data-модель входящего сообщения.

    Returns:
        Application-модель с ID сохранённой записи.

    Raises:
        ValueError: Если DTO не содержит ID сохранённой записи.
    """
    if dto.id is None:
        raise ValueError("Saved incoming message DTO must contain id")

    return SavedIncomingMessage(
        id=dto.id,
        article_id=dto.article_id,
        channel=dto.channel,
        app_user_id=dto.app_user_id,
        chat_id=dto.chat_id,
        message_id=dto.message_id,
        text=dto.message_text,
    )


def saved_incoming_message_from_row(row: sqlite3.Row) -> SavedIncomingMessage:
    """Преобразует строку SQLite напрямую в сохранённое входящее сообщение."""
    return saved_incoming_message_from_dto(incoming_message_dto_from_row(row))
