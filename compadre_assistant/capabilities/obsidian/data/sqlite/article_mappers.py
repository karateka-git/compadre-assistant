from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from compadre_assistant.capabilities.obsidian.data.sqlite.article_dtos import ArticleDto
from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
from compadre_assistant.capabilities.obsidian.domain.articles.statuses import ArticleStatus


def article_dto_from_row(row: sqlite3.Row) -> ArticleDto:
    """Преобразует строку SQLite в DTO статьи.

    Args:
        row: Строка SQLite с колонками таблицы `articles`.

    Returns:
        DTO, отражающий сохранённую строку.
    """
    return ArticleDto(
        id=row["id"],
        source_url=row["source_url"],
        normalized_url=row["normalized_url"],
        app_user_id=row["app_user_id"],
        title=row["title"],
        cleaned_text=row["cleaned_text"],
        text_hash=row["text_hash"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def article_dto_from_article(article: Article) -> ArticleDto:
    """Преобразует доменную статью в DTO для data-слоя.

    Args:
        article: Доменная модель статьи.

    Returns:
        DTO с простыми значениями, подходящими для SQLite.
    """
    return ArticleDto(
        id=article.id,
        source_url=article.source_url,
        normalized_url=article.normalized_url,
        app_user_id=article.app_user_id,
        title=article.title,
        cleaned_text=article.cleaned_text,
        text_hash=article.text_hash,
        status=article.status.value,
        created_at=article.created_at.isoformat() if article.created_at else None,
        updated_at=article.updated_at.isoformat() if article.updated_at else None,
    )


def article_from_dto(dto: ArticleDto) -> Article:
    """Преобразует DTO статьи в доменную модель.

    Args:
        dto: Data-модель статьи из SQLite.

    Returns:
        Доменная модель статьи.

    Raises:
        ValueError: Если DTO не содержит обязательные поля сохранённой статьи.
    """
    if dto.id is None:
        raise ValueError("Saved article DTO must contain id")
    if dto.created_at is None:
        raise ValueError("Saved article DTO must contain created_at")
    if dto.updated_at is None:
        raise ValueError("Saved article DTO must contain updated_at")

    return Article(
        id=dto.id,
        source_url=dto.source_url,
        normalized_url=dto.normalized_url,
        app_user_id=dto.app_user_id,
        title=dto.title,
        cleaned_text=dto.cleaned_text,
        text_hash=dto.text_hash,
        status=ArticleStatus(dto.status),
        created_at=_parse_utc_timestamp(dto.created_at),
        updated_at=_parse_utc_timestamp(dto.updated_at),
    )


def article_from_row(row: sqlite3.Row) -> Article:
    """Преобразует строку SQLite напрямую в доменную модель статьи."""
    return article_from_dto(article_dto_from_row(row))


def _parse_utc_timestamp(value: str) -> datetime:
    """Преобразует SQLite timestamp в timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
