"""Преобразования ошибок обработки статьи для хранения в SQLite."""

from __future__ import annotations

from compadre_assistant.capabilities.obsidian.domain.articles.stages import ProcessingStage
from compadre_assistant.capabilities.obsidian.data.sqlite.processing_error_dtos import (
    ProcessingErrorDto,
)


def processing_error_dto_from_record(
    *,
    article_id: int | None,
    app_user_id: int | None,
    incoming_message_id: int | None,
    stage: ProcessingStage,
    error_type: str,
    error_message: str,
) -> ProcessingErrorDto:
    """Преобразует параметры ошибки article pipeline в SQLite DTO.

    Args:
        article_id: ID статьи, если она уже создана.
        app_user_id: ID пользователя, если он уже известен.
        incoming_message_id: ID связанного входящего сообщения.
        stage: Этап обработки статьи.
        error_type: Имя класса ошибки.
        error_message: Текст ошибки.

    Returns:
        DTO с формой записи таблицы `processing_errors`.
    """
    return ProcessingErrorDto(
        article_id=article_id,
        app_user_id=app_user_id,
        incoming_message_id=incoming_message_id,
        stage=stage.value,
        error_type=error_type,
        error_message=error_message,
    )
