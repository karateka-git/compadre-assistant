"""Smoke-сценарии для быстрой проверки основных контуров приложения."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from obs_chat_bot.capabilities.obsidian.domain.articles.analyzing import (
    AnalyzeArticleCommand,
    AnalyzeArticleUseCase,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.extracted import ExtractedArticle
from obs_chat_bot.capabilities.obsidian.domain.articles.html import ArticleHtml
from obs_chat_bot.capabilities.obsidian.domain.articles.processing import (
    ProcessArticleUrlCommand,
    ProcessArticleUrlUseCase,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.analysis_result_repository import (
    ArticleAnalysisResultRepositoryError,
    SQLiteArticleAnalysisResultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.article_repository import (
    ArticleRepositoryError,
    SQLiteArticleRepository,
)
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import MigrationError, apply_migrations
from obs_chat_bot.capabilities.obsidian.data.sqlite.processing_error_repository import (
    SQLiteProcessingErrorRecorder,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus


class SQLiteSmokeError(RuntimeError):
    """Ошибка прохождения smoke-сценария SQLite."""


class PipelineSmokeError(RuntimeError):
    """Ошибка прохождения smoke-сценария article pipeline."""


class AnalysisSmokeError(RuntimeError):
    """Ошибка прохождения smoke-сценария LLM-анализа статьи."""


def run_sqlite_smoke() -> None:
    """Проверяет миграции, FTS5 и `ArticleRepository` на временной базе.

    Raises:
        SQLiteSmokeError: Если миграции или чтение сохранённой статьи не работают.
    """
    try:
        with TemporaryDirectory(prefix="obs-chat-bot-smoke-") as temporary_directory:
            database_path = Path(temporary_directory) / "smoke.db"
            _run_sqlite_scenario(database_path)
    except (MigrationError, ArticleRepositoryError, OSError, sqlite3.Error) as error:
        raise SQLiteSmokeError(f"SQLite smoke scenario failed: {error}") from error


def run_pipeline_smoke() -> None:
    """Проверяет article pipeline на fake HTML без сети и `trafilatura`.

    Raises:
        PipelineSmokeError: Если pipeline не сохранил статью, не извлёк текст
            или повторно обработал уже сохранённый URL.
    """
    try:
        with TemporaryDirectory(prefix="obs-chat-bot-pipeline-smoke-") as temporary_directory:
            database_path = Path(temporary_directory) / "pipeline-smoke.db"
            _run_pipeline_scenario(database_path)
    except (
        MigrationError,
        ArticleRepositoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as error:
        raise PipelineSmokeError(f"Pipeline smoke scenario failed: {error}") from error


def run_analysis_smoke() -> None:
    """Проверяет analysis pipeline на fake LLM без сетевых запросов.

    Raises:
        AnalysisSmokeError: Если анализ не сохранил результат или не обновил
            статус статьи.
    """
    try:
        with TemporaryDirectory(prefix="obs-chat-bot-analysis-smoke-") as directory:
            database_path = Path(directory) / "analysis-smoke.db"
            _run_analysis_scenario(database_path)
    except (
        MigrationError,
        ArticleRepositoryError,
        ArticleAnalysisResultRepositoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as error:
        raise AnalysisSmokeError(f"Analysis smoke scenario failed: {error}") from error


def _run_sqlite_scenario(database_path: Path) -> None:
    """Выполняет проверки SQLite на указанном временном файле."""
    with connect_database(database_path) as connection:
        first_run = apply_migrations(connection)
        if not first_run:
            raise SQLiteSmokeError("Fresh database did not receive migrations")

        second_run = apply_migrations(connection)
        if second_run:
            raise SQLiteSmokeError("Migrations were applied more than once")

        connection.execute(
            "INSERT INTO obsidian_note_chunks_fts(obsidian_note_chunks_fts) "
            "VALUES ('integrity-check')"
        )

        _create_smoke_user(connection)
        repository = SQLiteArticleRepository(connection)
        expected = Article(
            source_url="https://example.com/article?utm_source=smoke",
            normalized_url="https://example.com/article",
        )
        created = repository.create(expected)

        if created.id is None:
            raise SQLiteSmokeError("Created article does not have an id")

        loaded = repository.get_by_id(created.id)
        if loaded != created:
            raise SQLiteSmokeError("Loaded article differs from the created article")

        found = repository.find_by_normalized_url(expected.normalized_url)
        if found != created:
            raise SQLiteSmokeError("Article was not found by normalized URL")


def _run_pipeline_scenario(database_path: Path) -> None:
    """Выполняет article pipeline на временной базе и fake зависимостях."""
    with connect_database(database_path) as connection:
        apply_migrations(connection)
        _create_smoke_user(connection)

        repository = SQLiteArticleRepository(connection)
        fetcher = _FakeArticleHtmlFetcher()
        extractor = _FakeArticleTextExtractor()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=fetcher,
            text_extractor=extractor,
            error_recorder=SQLiteProcessingErrorRecorder(connection),
        )

        first_result = use_case.execute(
            ProcessArticleUrlCommand(
                source_url="https://example.com/article?utm_source=smoke"
            )
        )

        if not first_result.created:
            raise PipelineSmokeError("Pipeline did not create a new article")
        if not first_result.extracted:
            raise PipelineSmokeError("Pipeline did not extract article text")
        if first_result.article.status != ArticleStatus.EXTRACTED:
            raise PipelineSmokeError("Pipeline did not mark article as extracted")
        if first_result.article.normalized_url != "https://example.com/article":
            raise PipelineSmokeError("Pipeline did not normalize article URL")
        if first_result.article.cleaned_text != _FakeArticleTextExtractor.CLEANED_TEXT:
            raise PipelineSmokeError("Pipeline saved unexpected cleaned text")

        second_result = use_case.execute(
            ProcessArticleUrlCommand(source_url="https://example.com/article")
        )

        if second_result.created:
            raise PipelineSmokeError("Pipeline created duplicate article")
        if second_result.extracted:
            raise PipelineSmokeError("Pipeline re-extracted already processed article")
        if fetcher.calls != ["https://example.com/article?utm_source=smoke"]:
            raise PipelineSmokeError("Pipeline fetched HTML unexpected number of times")


def _run_analysis_scenario(database_path: Path) -> None:
    """Выполняет analysis pipeline на временной базе и fake LLM."""
    with connect_database(database_path) as connection:
        apply_migrations(connection)
        _create_smoke_user(connection)

        article_repository = SQLiteArticleRepository(connection)
        analysis_repository = SQLiteArticleAnalysisResultRepository(connection)
        article = article_repository.create(
            Article(
                source_url="https://example.com/article",
                normalized_url="https://example.com/article",
            )
        )
        if article.id is None:
            raise AnalysisSmokeError("Created article does not have an id")

        updated = article_repository.update_content(
            article.id,
            title="Smoke article",
            cleaned_text="Smoke article text",
            text_hash="hash",
        )
        if updated is None:
            raise AnalysisSmokeError("Article content was not saved")

        use_case = AnalyzeArticleUseCase(
            article_repository=article_repository,
            analyzer=_FakeArticleAnalyzer(),
            analysis_result_repository=analysis_repository,
            error_recorder=SQLiteProcessingErrorRecorder(connection),
        )

        result = use_case.execute(AnalyzeArticleCommand(article_id=article.id))

        if not result.created:
            raise AnalysisSmokeError("Analysis smoke did not create a result")
        if result.article.status != ArticleStatus.ANALYZED:
            raise AnalysisSmokeError("Article was not marked as analyzed")
        if "Smoke summary" not in result.analysis.result_text:
            raise AnalysisSmokeError("Analysis result contains unexpected text")

        repeated = use_case.execute(AnalyzeArticleCommand(article_id=article.id))
        if repeated.created:
            raise AnalysisSmokeError("Analysis smoke created duplicate result")


def _create_smoke_user(connection: sqlite3.Connection) -> None:
    """Создаёт явного пользователя для smoke-данных на пустой development-схеме."""
    connection.execute(
        "INSERT INTO app_users (display_name) VALUES (?)",
        ("Smoke user",),
    )


class _FakeArticleHtmlFetcher:
    """Fake HTML-загрузчик для smoke-проверки pipeline."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str) -> ArticleHtml:
        """Возвращает стабильный HTML без сетевого запроса."""
        self.calls.append(url)
        return ArticleHtml(
            source_url=url,
            final_url="https://example.com/article",
            content="<html><body><article>Smoke article</article></body></html>",
            content_type="text/html",
        )


class _FakeArticleTextExtractor:
    """Fake extractor для smoke-проверки pipeline."""

    CLEANED_TEXT = "Smoke article text"

    def extract(self, html: ArticleHtml) -> ExtractedArticle:
        """Возвращает стабильный очищенный текст без `trafilatura`."""
        return ExtractedArticle(
            source_url=html.source_url,
            final_url=html.final_url,
            title="Smoke article",
            cleaned_text=self.CLEANED_TEXT,
        )


class _FakeArticleAnalyzer:
    """Fake LLM-анализатор для smoke-проверки analysis pipeline."""

    def analyze(self, article: Article) -> ArticleAnalysisResult:
        """Возвращает стабильный Markdown-результат без LLM-запроса."""
        if article.id is None:
            raise ValueError("article must contain id")
        return ArticleAnalysisResult(
            article_id=article.id,
            llm_model="fake-llm",
            prompt_version="article-summary-v1",
            result_text="## Кратко\nSmoke summary.",
        )
