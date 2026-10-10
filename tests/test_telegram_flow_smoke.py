"""Smoke-тест Telegram-only MVP без реального Telegram и LLM."""

from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleUseCase
from compadre_assistant.capabilities.obsidian.domain.articles.extracted import ExtractedArticle
from compadre_assistant.capabilities.obsidian.domain.articles.html import ArticleHtml
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlUseCase
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from test_incoming_boundary import FakeIdentity
from test_process_incoming_message import FakeVaultSelectionManager, _test_vault
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import (
    SQLiteArticleAnalysisResultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.capabilities.obsidian.data.sqlite.incoming_message_repository import (
    SQLiteIncomingMessageRepository,
)
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.capabilities.obsidian.data.sqlite.processing_error_repository import (
    SQLiteProcessingErrorRecorder,
)
from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
from tests.sqlite_helpers import ensure_app_user


class SilentLogger:
    """Logger-заглушка для smoke-теста Telegram-flow."""

    def error(self, *_args: object, **_kwargs: object) -> None:
        """Игнорирует error-сообщение."""


class TelegramFlowSmokeTest(unittest.TestCase):
    """Проверяет ежедневный Telegram-сценарий на временной SQLite-базе."""

    def test_telegram_flow_saves_message_article_and_analysis(self) -> None:
        """Ссылка проходит весь MVP-flow и повторно использует сохранённые данные."""
        with TemporaryDirectory(prefix="compadre-assistant-telegram-flow-") as directory:
            database_path = Path(directory) / "telegram-flow.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
                ensure_app_user(connection)
                fetcher = FakeArticleHtmlFetcher()
                analyzer = FakeArticleAnalyzer()
                article_repository = SQLiteArticleRepository(connection)
                process_use_case = ProcessArticleUrlUseCase(
                    article_repository=article_repository,
                    html_fetcher=fetcher,
                    text_extractor=FakeArticleTextExtractor(),
                    error_recorder=SQLiteProcessingErrorRecorder(connection),
                )
                analysis_use_case = AnalyzeArticleUseCase(
                    article_repository=article_repository,
                    analyzer=analyzer,
                    analysis_result_repository=SQLiteArticleAnalysisResultRepository(
                        connection
                    ),
                    error_recorder=SQLiteProcessingErrorRecorder(connection),
                )
                message_repository = SQLiteIncomingMessageRepository(connection)

                identity = FakeIdentity()
                identity.user = AppUser(id=1, display_name="Тест")
                incoming_use_case = IncomingMessageDispatcher(
                    registry=CapabilityRegistry((ObsidianMessageHandler(
                        article_url_use_case=process_use_case,
                        article_analysis_use_case=analysis_use_case,
                        incoming_message_repository=message_repository,
                        vault_selection_manager=FakeVaultSelectionManager(selected=_test_vault(1)),
                    ),)),
                    user_identity_service=identity,
                    chat_mode_repository=SQLiteChatModeRepository(connection),
                )
                incoming_use_case.execute(IncomingEvent(channel="telegram", chat_id="100", message_id="9", text="/obsidian"))
                first_reply = incoming_use_case.execute(_message("10")).text
                second_reply = incoming_use_case.execute(_message("11")).text

                counts = _read_counts(connection)

        self.assertIn("Анализ готов.", first_reply)
        self.assertIn("## Кратко", first_reply)
        self.assertIn("Использую сохраненный анализ.", second_reply)
        self.assertEqual(fetcher.calls, ["https://example.com/article?utm_source=tg"])
        self.assertEqual(analyzer.article_ids, [1])
        self.assertEqual(counts["articles"], 1)
        self.assertEqual(counts["incoming_messages"], 2)
        self.assertEqual(counts["analysis_results"], 1)
        self.assertEqual(counts["processing_errors"], 0)


class FakeArticleHtmlFetcher:
    """Fake HTML-загрузчик для Telegram-flow smoke."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str) -> ArticleHtml:
        """Возвращает стабильный HTML без сетевого запроса."""
        self.calls.append(url)
        return ArticleHtml(
            source_url=url,
            final_url="https://example.com/article",
            content="<html><body><article>Telegram smoke</article></body></html>",
            content_type="text/html",
        )


class FakeArticleTextExtractor:
    """Fake extractor для Telegram-flow smoke."""

    def extract(self, html: ArticleHtml) -> ExtractedArticle:
        """Возвращает очищенный текст без вызова `trafilatura`."""
        return ExtractedArticle(
            source_url=html.source_url,
            final_url=html.final_url,
            title="Telegram smoke article",
            cleaned_text="Telegram smoke article text",
        )


class FakeArticleAnalyzer:
    """Fake LLM-анализатор для Telegram-flow smoke."""

    def __init__(self) -> None:
        self.article_ids: list[int] = []

    def analyze(self, article: Article) -> ArticleAnalysisResult:
        """Возвращает стабильный Markdown-анализ без LLM-запроса."""
        if article.id is None:
            raise ValueError("article must contain id")
        self.article_ids.append(article.id)
        return ArticleAnalysisResult(
            article_id=article.id,
            llm_model="fake-llm",
            prompt_version="article-summary-v1",
            result_text="## Кратко\nTelegram smoke analysis.",
        )


def _message(message_id: str) -> IncomingEvent:
    """Создаёт входящее Telegram-сообщение со ссылкой."""
    return IncomingEvent(
        channel="telegram",
        chat_id="100",
        message_id=message_id,
        text="https://example.com/article?utm_source=tg",
    )


def _read_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Считает записи MVP-таблиц после smoke-сценария."""
    tables = [
        "articles",
        "incoming_messages",
        "analysis_results",
        "processing_errors",
    ]
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tables
    }


if __name__ == "__main__":
    unittest.main()
