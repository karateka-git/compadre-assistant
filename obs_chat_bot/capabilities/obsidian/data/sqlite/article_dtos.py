from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleDto:
    """Представляет статью в форме data-слоя SQLite.

    DTO дублирует доменную модель `Article` в форме, удобной для хранения:
    статус и timestamps представлены простыми значениями SQLite.
    """

    source_url: str
    normalized_url: str
    app_user_id: int
    title: str | None
    cleaned_text: str | None
    text_hash: str | None
    status: str
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
