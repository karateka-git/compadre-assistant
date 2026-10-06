from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from obs_chat_bot.capabilities.obsidian.data.sqlite.analysis_result_dtos import ArticleAnalysisResultDto
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult


def analysis_result_dto_from_result(
    result: ArticleAnalysisResult,
) -> ArticleAnalysisResultDto:
    """Преобразует доменную модель результата анализа в SQLite DTO.

    Args:
        result: Доменная модель результата LLM-анализа.

    Returns:
        DTO с простыми значениями, подходящими для SQLite.
    """
    return ArticleAnalysisResultDto(
        id=result.id,
        article_id=result.article_id,
        app_user_id=result.app_user_id,
        llm_model=result.llm_model,
        prompt_version=result.prompt_version,
        result_text=result.result_text,
        created_at=result.created_at.isoformat() if result.created_at else None,
    )


def analysis_result_dto_from_row(row: sqlite3.Row) -> ArticleAnalysisResultDto:
    """Преобразует строку SQLite в DTO результата анализа.

    Args:
        row: Строка SQLite с колонками таблицы `analysis_results`.

    Returns:
        DTO, отражающий сохранённую строку.
    """
    return ArticleAnalysisResultDto(
        id=row["id"],
        article_id=row["article_id"],
        app_user_id=row["app_user_id"],
        llm_model=row["llm_model"],
        prompt_version=row["prompt_version"],
        result_text=row["result_text"],
        created_at=row["created_at"],
    )


def analysis_result_from_dto(
    dto: ArticleAnalysisResultDto,
) -> ArticleAnalysisResult:
    """Преобразует SQLite DTO в доменную модель результата анализа.

    Args:
        dto: Data-модель результата анализа.

    Returns:
        Доменная модель результата анализа.

    Raises:
        ValueError: Если DTO не содержит обязательные поля сохранённой записи.
    """
    if dto.id is None:
        raise ValueError("Saved analysis result DTO must contain id")
    if dto.created_at is None:
        raise ValueError("Saved analysis result DTO must contain created_at")

    return ArticleAnalysisResult(
        id=dto.id,
        article_id=dto.article_id,
        app_user_id=dto.app_user_id,
        llm_model=dto.llm_model,
        prompt_version=dto.prompt_version,
        result_text=dto.result_text,
        created_at=_parse_utc_timestamp(dto.created_at),
    )


def analysis_result_from_row(row: sqlite3.Row) -> ArticleAnalysisResult:
    """Преобразует строку SQLite напрямую в результат анализа."""
    return analysis_result_from_dto(analysis_result_dto_from_row(row))


def _parse_utc_timestamp(value: str) -> datetime:
    """Преобразует SQLite timestamp в timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
