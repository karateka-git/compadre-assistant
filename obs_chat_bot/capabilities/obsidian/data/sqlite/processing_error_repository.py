from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.application.articles.ports import ProcessingErrorRecorder
from obs_chat_bot.capabilities.obsidian.application.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.data.sqlite.processing_error_mappers import (
    processing_error_dto_from_record,
)


class SQLiteProcessingErrorRecorder(ProcessingErrorRecorder):
    """Сохраняет диагностические ошибки article pipeline в SQLite.

    Args:
        connection: Соединение SQLite, созданное через `connect_database`.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(
        self,
        *,
        article_id: int | None,
        app_user_id: int | None = None,
        incoming_message_id: int | None = None,
        stage: ProcessingStage,
        error_type: str,
        error_message: str,
    ) -> None:
        """Сохраняет ошибку обработки статьи.

        Args:
            article_id: ID статьи, если она уже была создана.
            app_user_id: ID пользователя приложения, если он уже известен.
            incoming_message_id: ID входящего сообщения, если ошибка связана
                с конкретным сообщением пользователя.
            stage: Этап обработки, на котором произошла ошибка.
            error_type: Имя класса ошибки.
            error_message: Текст ошибки.
        """
        dto = processing_error_dto_from_record(
            article_id=article_id,
            app_user_id=app_user_id,
            incoming_message_id=incoming_message_id,
            stage=stage,
            error_type=error_type,
            error_message=error_message,
        )

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO processing_errors (
                    article_id,
                    app_user_id,
                    incoming_message_id,
                    stage,
                    error_type,
                    error_message
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    dto.article_id,
                    dto.app_user_id,
                    dto.incoming_message_id,
                    dto.stage,
                    dto.error_type,
                    dto.error_message,
                ),
            )
