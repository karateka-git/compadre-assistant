from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.articles.ports import ArticleAnalysisResultRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.analysis_result_mappers import (
    analysis_result_dto_from_result,
    analysis_result_from_row,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult


ANALYSIS_RESULT_COLUMNS = """
    id,
    article_id,
    app_user_id,
    llm_model,
    prompt_version,
    result_text,
    created_at
"""


class ArticleAnalysisResultRepositoryError(RuntimeError):
    """Базовая ошибка операций `ArticleAnalysisResultRepository`."""


class SQLiteArticleAnalysisResultRepository(ArticleAnalysisResultRepository):
    """Изолирует SQL-операции с таблицей `analysis_results`.

    Args:
        connection: Соединение SQLite, созданное через `connect_database`.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, result: ArticleAnalysisResult) -> ArticleAnalysisResult:
        """Сохраняет результат анализа и возвращает запись с ID.

        Args:
            result: Несохранённый результат анализа статьи.

        Returns:
            Результат, прочитанный из базы после вставки.

        Raises:
            ValueError: Если переданный результат уже содержит ID.
            ArticleAnalysisResultRepositoryError: Если запись не удалось прочитать.
        """
        if result.id is not None:
            raise ValueError("A new analysis result must not have an id")

        dto = analysis_result_dto_from_result(result)

        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO analysis_results (
                    article_id,
                    app_user_id,
                    llm_model,
                    prompt_version,
                    result_text
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    dto.article_id,
                    dto.app_user_id,
                    dto.llm_model,
                    dto.prompt_version,
                    dto.result_text,
                ),
            )

        result_id = cursor.lastrowid
        if result_id is None:
            raise ArticleAnalysisResultRepositoryError(
                "SQLite did not return a new analysis result id"
            )

        created = self.get_by_id(result_id)
        if created is None:
            raise ArticleAnalysisResultRepositoryError(
                f"Created analysis result could not be read: {result_id}"
            )
        return created

    def get_by_id(self, result_id: int) -> ArticleAnalysisResult | None:
        """Возвращает результат анализа по ID.

        Args:
            result_id: Идентификатор результата анализа в SQLite.

        Returns:
            Найденный результат или `None`.
        """
        row = self._connection.execute(
            f"SELECT {ANALYSIS_RESULT_COLUMNS} FROM analysis_results WHERE id = ?",
            (result_id,),
        ).fetchone()
        return analysis_result_from_row(row) if row is not None else None

    def get_latest_for_article(self, article_id: int) -> ArticleAnalysisResult | None:
        """Возвращает последний результат анализа статьи.

        Args:
            article_id: ID статьи, для которой нужен последний результат.

        Returns:
            Самый поздний результат анализа или `None`, если результатов нет.
        """
        row = self._connection.execute(
            f"""
            SELECT {ANALYSIS_RESULT_COLUMNS}
            FROM analysis_results
            WHERE article_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (article_id,),
        ).fetchone()
        return analysis_result_from_row(row) if row is not None else None
