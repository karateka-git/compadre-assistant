"""Тесты пользовательской изоляции статей."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from obs_chat_bot.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from obs_chat_bot.data.sqlite.connection import connect_database
from obs_chat_bot.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from tests.sqlite_helpers import ensure_app_user


class ArticleUserIsolationTest(unittest.TestCase):
    """Проверяет, что одинаковая ссылка может принадлежать разным пользователям."""

    def test_same_normalized_url_can_be_saved_for_different_users(self) -> None:
        """Уникальность статьи ограничена пользователем приложения."""
        with TemporaryDirectory(prefix="obs-chat-bot-article-scope-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                ensure_app_user(connection)
                connection.execute(
                    "INSERT INTO app_users (id, display_name) VALUES (2, 'Second user')"
                )
                repository = SQLiteArticleRepository(connection)

                first = repository.create(_article(app_user_id=1))
                second = repository.create(_article(app_user_id=2))

                found_first = repository.find_by_normalized_url(
                    "https://example.com/article",
                    app_user_id=1,
                )
                found_second = repository.find_by_normalized_url(
                    "https://example.com/article",
                    app_user_id=2,
                )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(found_first, first)
        self.assertEqual(found_second, second)


def _article(app_user_id: int) -> Article:
    """Создает статью для конкретного пользователя."""
    return Article(
        app_user_id=app_user_id,
        source_url="https://example.com/article",
        normalized_url="https://example.com/article",
    )


if __name__ == "__main__":
    unittest.main()
