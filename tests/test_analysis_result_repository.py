"""Тесты SQLite-хранения результатов LLM-анализа."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from obs_chat_bot.capabilities.obsidian.data.sqlite.analysis_result_repository import (
    SQLiteArticleAnalysisResultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from obs_chat_bot.data.sqlite.connection import connect_database
from obs_chat_bot.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from tests.sqlite_helpers import ensure_app_user


class SQLiteArticleAnalysisResultRepositoryTest(unittest.TestCase):
    """Проверяет repository результатов анализа на временной SQLite-базе."""

    def test_save_creates_analysis_result(self) -> None:
        """Новый результат анализа сохраняется и возвращается с ID."""
        with TemporaryDirectory(prefix="obs-chat-bot-analysis-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                article_id = _create_article(connection)
                repository = SQLiteArticleAnalysisResultRepository(connection)

                saved = repository.save(_analysis_result(article_id))

                row = connection.execute(
                    """
                    SELECT article_id, llm_model, prompt_version, result_text
                    FROM analysis_results
                    WHERE id = ?
                    """,
                    (saved.id,),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(saved.article_id, article_id)
        self.assertEqual(row["llm_model"], "fake-llm")
        self.assertIn("Кратко", row["result_text"])
        self.assertIsNotNone(saved.created_at)

    def test_get_by_id_returns_saved_result(self) -> None:
        """Сохранённый результат можно прочитать по ID."""
        with TemporaryDirectory(prefix="obs-chat-bot-analysis-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                article_id = _create_article(connection)
                repository = SQLiteArticleAnalysisResultRepository(connection)
                saved = repository.save(_analysis_result(article_id))

                found = repository.get_by_id(saved.id)

        self.assertEqual(found, saved)

    def test_get_latest_for_article_returns_newest_result(self) -> None:
        """Для статьи возвращается последний сохранённый результат анализа."""
        with TemporaryDirectory(prefix="obs-chat-bot-analysis-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                article_id = _create_article(connection)
                repository = SQLiteArticleAnalysisResultRepository(connection)
                first = repository.save(_analysis_result(article_id, text="Первая версия"))
                second = repository.save(_analysis_result(article_id, text="Вторая версия"))

                latest = repository.get_latest_for_article(article_id)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(latest, second)

    def test_get_latest_for_article_returns_none_without_results(self) -> None:
        """Если результатов анализа нет, repository возвращает `None`."""
        with TemporaryDirectory(prefix="obs-chat-bot-analysis-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                article_id = _create_article(connection)
                repository = SQLiteArticleAnalysisResultRepository(connection)

                latest = repository.get_latest_for_article(article_id)

        self.assertIsNone(latest)

    def test_save_rejects_already_saved_result(self) -> None:
        """Repository не вставляет модель, которая уже содержит ID."""
        with TemporaryDirectory(prefix="obs-chat-bot-analysis-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                repository = SQLiteArticleAnalysisResultRepository(connection)

                with self.assertRaises(ValueError):
                    repository.save(
                        ArticleAnalysisResult(
                            id=1,
                            article_id=1,
                            llm_model="fake-llm",
                            prompt_version="article-summary-v1",
                            result_text="## Кратко\nТекст.",
                        )
                    )


def _create_article(connection: sqlite3.Connection) -> int:
    """Создаёт статью для проверки внешнего ключа результатов анализа."""
    ensure_app_user(connection)
    article = SQLiteArticleRepository(connection).create(
        Article(
            source_url="https://example.com/article",
            normalized_url="https://example.com/article",
        )
    )
    if article.id is None:
        raise AssertionError("Created article must contain id")
    return article.id


def _analysis_result(
    article_id: int,
    text: str = "Статья разобрана.",
) -> ArticleAnalysisResult:
    """Создаёт несохранённый результат анализа для repository-тестов."""
    return ArticleAnalysisResult(
        article_id=article_id,
        llm_model="fake-llm",
        prompt_version="article-summary-v1",
        result_text=f"## Кратко\n{text}",
    )


if __name__ == "__main__":
    unittest.main()
