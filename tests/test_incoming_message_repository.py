"""Тесты SQLite-хранения входящих сообщений."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from obs_chat_bot.capabilities.obsidian.domain.articles.incoming_messages import ArticleIncomingMessage
from obs_chat_bot.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.capabilities.obsidian.data.sqlite.incoming_message_repository import (
    SQLiteIncomingMessageRepository,
)
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from tests.sqlite_helpers import ensure_app_user


class SQLiteIncomingMessageRepositoryTest(unittest.TestCase):
    """Проверяет repository входящих сообщений на временной SQLite-базе."""

    def test_save_creates_incoming_message(self) -> None:
        """Новое сообщение сохраняется и возвращается с ID."""
        with TemporaryDirectory(prefix="obs-chat-bot-messages-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                ensure_app_user(connection)
                repository = SQLiteIncomingMessageRepository(connection)

                saved = repository.save(_message())

                row = connection.execute(
                    """
                    SELECT channel, chat_id, message_id, message_text
                    FROM incoming_messages
                    WHERE id = ?
                    """,
                    (saved.id,),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(saved.channel, "telegram")
        self.assertEqual(row["message_text"], "https://example.com/article")

    def test_save_returns_existing_message_for_duplicate_external_id(self) -> None:
        """Повтор того же channel/chat/message не создаёт дубль."""
        with TemporaryDirectory(prefix="obs-chat-bot-messages-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                ensure_app_user(connection)
                repository = SQLiteIncomingMessageRepository(connection)

                first = repository.save(_message())
                second = repository.save(_message(text="https://example.com/changed"))
                count = connection.execute(
                    "SELECT COUNT(*) AS total FROM incoming_messages"
                ).fetchone()["total"]

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.text, "https://example.com/article")
        self.assertEqual(count, 1)

    def test_link_to_article_updates_saved_message(self) -> None:
        """Сохранённое сообщение можно связать со статьёй."""
        with TemporaryDirectory(prefix="obs-chat-bot-messages-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                ensure_app_user(connection)
                message_repository = SQLiteIncomingMessageRepository(connection)
                article = SQLiteArticleRepository(connection).create(
                    Article(
                        source_url="https://example.com/article",
                        normalized_url="https://example.com/article",
                    )
                )
                saved = message_repository.save(_message())

                linked = message_repository.link_to_article(
                    incoming_message_id=saved.id,
                    article_id=article.id,
                )

        self.assertIsNotNone(linked)
        self.assertEqual(linked.article_id, article.id)

    def test_link_to_article_returns_none_for_missing_message(self) -> None:
        """Привязка отсутствующего сообщения возвращает `None`."""
        with TemporaryDirectory(prefix="obs-chat-bot-messages-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                repository = SQLiteIncomingMessageRepository(connection)

                linked = repository.link_to_article(
                    incoming_message_id=404,
                    article_id=1,
                )

        self.assertIsNone(linked)


def _message(text: str = "https://example.com/article") -> ArticleIncomingMessage:
    """Создаёт входящее Telegram-сообщение для repository-тестов."""
    return ArticleIncomingMessage(
        channel="telegram",
        chat_id="100",
        message_id="200",
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
