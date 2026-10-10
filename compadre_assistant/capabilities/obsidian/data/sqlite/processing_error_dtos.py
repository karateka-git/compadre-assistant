from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessingErrorDto:
    """Представляет ошибку обработки статьи в форме таблицы SQLite."""

    stage: str
    error_type: str
    error_message: str
    article_id: int | None = None
    app_user_id: int | None = None
    incoming_message_id: int | None = None
    id: int | None = None
    created_at: str | None = None
