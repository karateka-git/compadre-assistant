"""Тесты application-моделей входящих сообщений."""

import unittest

from obs_chat_bot.capabilities.obsidian.domain.articles.incoming_messages import (
    ArticleIncomingMessage,
    SavedIncomingMessage,
)


class IncomingMessageTest(unittest.TestCase):
    """Проверяет базовую валидацию входящих сообщений."""

    def test_incoming_message_accepts_text_message(self) -> None:
        """Корректное сообщение создаётся без ошибки."""
        message = ArticleIncomingMessage(
            channel="telegram",
            chat_id="100",
            message_id="200",
            text="https://example.com/article",
        )

        self.assertEqual(message.channel, "telegram")

    def test_incoming_message_rejects_empty_text(self) -> None:
        """Пустой текст не подходит для текущего сценария обработки ссылок."""
        with self.assertRaises(ValueError):
            ArticleIncomingMessage(
                channel="telegram",
                chat_id="100",
                message_id="200",
                text=" ",
            )


class SavedIncomingMessageTest(unittest.TestCase):
    """Проверяет модель сохранённого входящего сообщения."""

    def test_saved_incoming_message_accepts_storage_identity(self) -> None:
        """Сохранённое сообщение содержит ID записи и опциональную связь со статьёй."""
        message = SavedIncomingMessage(
            id=1,
            channel="telegram",
            chat_id="100",
            message_id="200",
            text="https://example.com/article",
            article_id=10,
        )

        self.assertEqual(message.id, 1)
        self.assertEqual(message.article_id, 10)

    def test_saved_incoming_message_rejects_invalid_ids(self) -> None:
        """Неположительные ID не считаются валидной ссылкой на сохранённые данные."""
        with self.assertRaises(ValueError):
            SavedIncomingMessage(
                id=0,
                channel="telegram",
                chat_id="100",
                message_id="200",
                text="https://example.com/article",
            )

        with self.assertRaises(ValueError):
            SavedIncomingMessage(
                id=1,
                channel="telegram",
                chat_id="100",
                message_id="200",
                text="https://example.com/article",
                article_id=0,
            )


if __name__ == "__main__":
    unittest.main()
