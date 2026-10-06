from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleIncomingMessage:
    """Представляет сообщение, принятое article pipeline для журнала.

    Общий вход передаёт нейтральный ``IncomingEvent``; модель журнала создаётся
    внутри Obsidian только перед обработкой URL статьи.
    """

    channel: str
    chat_id: str
    message_id: str
    text: str
    app_user_id: int = 1
    external_user_id: str | None = None
    username: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not self.channel.strip():
            raise ValueError("channel must not be empty")
        if not self.chat_id.strip():
            raise ValueError("chat_id must not be empty")
        if not self.message_id.strip():
            raise ValueError("message_id must not be empty")
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.external_user_id is not None and not self.external_user_id.strip():
            raise ValueError("external_user_id must not be empty")


@dataclass(frozen=True, slots=True)
class SavedIncomingMessage:
    """Представляет сообщение article pipeline, уже сохранённое в хранилище.

    Application-слой использует эту модель, чтобы работать с ID записи без
    знания о конкретной таблице SQLite или другом data-источнике.
    """

    id: int
    channel: str
    chat_id: str
    message_id: str
    text: str
    app_user_id: int = 1
    article_id: int | None = None

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("id must be positive")
        if not self.channel.strip():
            raise ValueError("channel must not be empty")
        if not self.chat_id.strip():
            raise ValueError("chat_id must not be empty")
        if not self.message_id.strip():
            raise ValueError("message_id must not be empty")
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.article_id is not None and self.article_id <= 0:
            raise ValueError("article_id must be positive")
