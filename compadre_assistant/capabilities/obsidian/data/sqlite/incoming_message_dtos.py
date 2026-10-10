from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IncomingMessageDto:
    """Представляет входящее сообщение в форме data-слоя SQLite.

    DTO отражает таблицу `incoming_messages`: хранит channel-specific
    идентификаторы, исходный текст и optional связь со статьёй.
    """

    channel: str
    app_user_id: int
    chat_id: str
    message_id: str
    message_text: str
    article_id: int | None = None
    id: int | None = None
    received_at: str | None = None
