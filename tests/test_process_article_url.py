"""Тесты use case обработки URL статьи."""

from dataclasses import replace
from hashlib import sha256
import unittest

from obs_chat_bot.capabilities.obsidian.application.articles.errors import (
    ArticleExtractionError,
    ArticleFetchError,
)
from obs_chat_bot.capabilities.obsidian.application.articles.extracted import ExtractedArticle
from obs_chat_bot.capabilities.obsidian.application.articles.html import ArticleHtml
from obs_chat_bot.capabilities.obsidian.application.articles.processing import (
    ProcessArticleUrlCommand,
    ProcessArticleUrlError,
    ProcessArticleUrlUseCase,
)
from obs_chat_bot.capabilities.obsidian.application.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus


class FakeArticleRepository:
    """In-memory repository для проверки application use case."""

    def __init__(self, articles: list[Article] | None = None) -> None:
        self._articles_by_id: dict[int, Article] = {}
        self._next_id = 1
        self.status_updates: list[ArticleStatus] = []

        for article in articles or []:
            article_id = article.id or self._next_id
            self._articles_by_id[article_id] = replace(article, id=article_id)
            self._next_id = max(self._next_id, article_id + 1)

    def create(self, article: Article) -> Article:
        """Сохраняет статью в памяти."""
        created = replace(article, id=self._next_id)
        self._articles_by_id[self._next_id] = created
        self._next_id += 1
        return created

    def get_by_id(self, article_id: int) -> Article | None:
        """Возвращает статью по ID."""
        return self._articles_by_id.get(article_id)

    def find_by_normalized_url(
        self,
        normalized_url: str,
        app_user_id: int = 1,
    ) -> Article | None:
        """Ищет статью по нормализованному URL."""
        for article in self._articles_by_id.values():
            if article.normalized_url == normalized_url and article.app_user_id == app_user_id:
                return article
        return None

    def find_by_text_hash(self, text_hash: str, app_user_id: int = 1) -> list[Article]:
        """Возвращает статьи с указанным text hash."""
        return [
            article
            for article in self._articles_by_id.values()
            if article.text_hash == text_hash and article.app_user_id == app_user_id
        ]

    def update_status(
        self,
        article_id: int,
        status: ArticleStatus,
    ) -> Article | None:
        """Обновляет статус статьи в памяти."""
        article = self._articles_by_id.get(article_id)
        if article is None:
            return None

        updated = replace(article, status=status)
        self._articles_by_id[article_id] = updated
        self.status_updates.append(status)
        return updated

    def update_content(
        self,
        article_id: int,
        *,
        title: str | None,
        cleaned_text: str,
        text_hash: str,
        status: ArticleStatus = ArticleStatus.EXTRACTED,
    ) -> Article | None:
        """Сохраняет извлечённое содержимое статьи в памяти."""
        article = self._articles_by_id.get(article_id)
        if article is None:
            return None

        updated = replace(
            article,
            title=title,
            cleaned_text=cleaned_text,
            text_hash=text_hash,
            status=status,
        )
        self._articles_by_id[article_id] = updated
        return updated


class FakeHtmlFetcher:
    """Fake загрузчик HTML для use case тестов."""

    def __init__(self, error: ArticleFetchError | None = None) -> None:
        self.calls: list[str] = []
        self._error = error

    def fetch(self, url: str) -> ArticleHtml:
        """Возвращает HTML или выбрасывает заданную ошибку."""
        self.calls.append(url)
        if self._error is not None:
            raise self._error

        return ArticleHtml(
            source_url=url,
            final_url=url,
            content="<html><body>Raw article</body></html>",
            content_type="text/html",
        )


class FakeTextExtractor:
    """Fake extractor для use case тестов."""

    def __init__(self, error: ArticleExtractionError | None = None) -> None:
        self.calls: list[ArticleHtml] = []
        self._error = error

    def extract(self, html: ArticleHtml) -> ExtractedArticle:
        """Возвращает очищенный текст или выбрасывает заданную ошибку."""
        self.calls.append(html)
        if self._error is not None:
            raise self._error

        return ExtractedArticle(
            source_url=html.source_url,
            final_url=html.final_url,
            title="Article title",
            cleaned_text="Clean article text",
        )


class FakeProcessingErrorRecorder:
    """In-memory recorder ошибок pipeline."""

    def __init__(self) -> None:
        self.records: list[
            tuple[int | None, int | None, int | None, ProcessingStage, str, str]
        ] = []

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
        """Сохраняет ошибку в памяти."""
        self.records.append(
            (article_id, app_user_id, incoming_message_id, stage, error_type, error_message)
        )


class ProcessArticleUrlUseCaseTest(unittest.TestCase):
    """Проверяет основной application pipeline обработки URL."""

    def test_execute_creates_article_and_extracts_content(self) -> None:
        """Новая ссылка сохраняется, загружается и получает extracted-статус."""
        repository = FakeArticleRepository()
        fetcher = FakeHtmlFetcher()
        extractor = FakeTextExtractor()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=fetcher,
            text_extractor=extractor,
        )

        result = use_case.execute(
            ProcessArticleUrlCommand(
                source_url="https://example.com/post?utm_source=telegram"
            )
        )

        self.assertTrue(result.created)
        self.assertTrue(result.extracted)
        self.assertEqual(result.article.normalized_url, "https://example.com/post")
        self.assertEqual(result.article.status, ArticleStatus.EXTRACTED)
        self.assertEqual(result.article.title, "Article title")
        self.assertEqual(result.article.cleaned_text, "Clean article text")
        self.assertEqual(
            result.article.text_hash,
            sha256("Clean article text".encode("utf-8")).hexdigest(),
        )
        self.assertEqual(repository.status_updates, [ArticleStatus.FETCHING])
        self.assertEqual(fetcher.calls, ["https://example.com/post?utm_source=telegram"])
        self.assertEqual(len(extractor.calls), 1)

    def test_execute_reuses_existing_extracted_article(self) -> None:
        """Повторная ссылка с уже извлечённым текстом не загружается заново."""
        existing = Article(
            id=7,
            source_url="https://example.com/post",
            normalized_url="https://example.com/post",
            cleaned_text="Already extracted",
            status=ArticleStatus.EXTRACTED,
        )
        repository = FakeArticleRepository([existing])
        fetcher = FakeHtmlFetcher()
        extractor = FakeTextExtractor()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=fetcher,
            text_extractor=extractor,
        )

        result = use_case.execute(ProcessArticleUrlCommand("https://example.com/post"))

        self.assertFalse(result.created)
        self.assertFalse(result.extracted)
        self.assertEqual(result.article.cleaned_text, "Already extracted")
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(extractor.calls, [])

    def test_execute_returns_reviewed_article_without_reloading_source(self) -> None:
        """Повторная reviewed-ссылка не загружает страницу после очистки source text."""
        existing = Article(
            id=7,
            app_user_id=1,
            source_url="https://example.com/post",
            normalized_url="https://example.com/post",
            cleaned_text=None,
            status=ArticleStatus.REVIEWED,
        )
        repository = FakeArticleRepository([existing])
        fetcher = FakeHtmlFetcher()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=fetcher,
            text_extractor=FakeTextExtractor(),
        )

        result = use_case.execute(ProcessArticleUrlCommand(existing.source_url))

        self.assertFalse(result.created)
        self.assertFalse(result.extracted)
        self.assertEqual(result.article.status, ArticleStatus.REVIEWED)
        self.assertEqual(fetcher.calls, [])

    def test_failed_reprocess_preserves_reviewed_result(self) -> None:
        """Неудачный `/reanalyze` не уничтожает прежний reviewed-результат."""
        existing = Article(
            id=7,
            app_user_id=1,
            source_url="https://example.com/post",
            normalized_url="https://example.com/post",
            cleaned_text=None,
            status=ArticleStatus.REVIEWED,
        )
        repository = FakeArticleRepository([existing])
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=FakeHtmlFetcher(ArticleFetchError("offline")),
            text_extractor=FakeTextExtractor(),
        )

        with self.assertRaises(ProcessArticleUrlError):
            use_case.reprocess_existing(article_id=7, app_user_id=1)

        preserved = repository.get_by_id(7)
        self.assertEqual(preserved.status, ArticleStatus.REVIEWED)
        self.assertIsNone(preserved.cleaned_text)

    def test_execute_marks_failed_when_fetch_fails(self) -> None:
        """Ошибка загрузки переводит статью в failed и записывает диагностику."""
        repository = FakeArticleRepository()
        recorder = FakeProcessingErrorRecorder()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=FakeHtmlFetcher(ArticleFetchError("offline")),
            text_extractor=FakeTextExtractor(),
            error_recorder=recorder,
        )

        with self.assertRaises(ProcessArticleUrlError):
            use_case.execute(ProcessArticleUrlCommand("https://example.com/post"))

        article = repository.find_by_normalized_url("https://example.com/post")
        self.assertIsNotNone(article)
        self.assertEqual(article.status, ArticleStatus.FAILED)
        self.assertEqual(recorder.records[0][3], ProcessingStage.FETCHING)
        self.assertEqual(recorder.records[0][4], "ArticleFetchError")

    def test_execute_marks_failed_when_extraction_fails(self) -> None:
        """Ошибка извлечения переводит статью в failed и записывает диагностику."""
        repository = FakeArticleRepository()
        recorder = FakeProcessingErrorRecorder()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=FakeHtmlFetcher(),
            text_extractor=FakeTextExtractor(ArticleExtractionError("no text")),
            error_recorder=recorder,
        )

        with self.assertRaises(ProcessArticleUrlError):
            use_case.execute(ProcessArticleUrlCommand("https://example.com/post"))

        article = repository.find_by_normalized_url("https://example.com/post")
        self.assertIsNotNone(article)
        self.assertEqual(article.status, ArticleStatus.FAILED)
        self.assertEqual(recorder.records[0][3], ProcessingStage.EXTRACTION)
        self.assertEqual(recorder.records[0][4], "ArticleExtractionError")

    def test_execute_records_normalization_error(self) -> None:
        """Некорректный URL не создаёт статью и записывает ошибку нормализации."""
        repository = FakeArticleRepository()
        recorder = FakeProcessingErrorRecorder()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=FakeHtmlFetcher(),
            text_extractor=FakeTextExtractor(),
            error_recorder=recorder,
        )

        with self.assertRaises(ProcessArticleUrlError):
            use_case.execute(ProcessArticleUrlCommand("example.com/post"))

        self.assertEqual(repository.find_by_text_hash("anything"), [])
        self.assertEqual(recorder.records[0][0], None)
        self.assertEqual(recorder.records[0][3], ProcessingStage.NORMALIZATION)

    def test_execute_records_incoming_message_id_on_error(self) -> None:
        """Ошибка pipeline сохраняет ID входящего сообщения для диагностики."""
        repository = FakeArticleRepository()
        recorder = FakeProcessingErrorRecorder()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository,
            html_fetcher=FakeHtmlFetcher(ArticleFetchError("offline")),
            text_extractor=FakeTextExtractor(),
            error_recorder=recorder,
        )

        with self.assertRaises(ProcessArticleUrlError):
            use_case.execute(
                ProcessArticleUrlCommand(
                    source_url="https://example.com/post",
                    incoming_message_id=42,
                )
            )

        self.assertEqual(recorder.records[0][2], 42)


if __name__ == "__main__":
    unittest.main()
