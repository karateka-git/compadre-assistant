from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .statuses import ArticleStatus


@dataclass(frozen=True, slots=True)
class Article:
    """Представляет статью независимо от способа её хранения."""

    source_url: str
    normalized_url: str
    app_user_id: int = 1
    id: int | None = None
    title: str | None = None
    cleaned_text: str | None = None
    text_hash: str | None = None
    status: ArticleStatus = ArticleStatus.NEW
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source_url.strip():
            raise ValueError("source_url must not be empty")
        if not self.normalized_url.strip():
            raise ValueError("normalized_url must not be empty")
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if not isinstance(self.status, ArticleStatus):
            raise TypeError("status must be an ArticleStatus")
