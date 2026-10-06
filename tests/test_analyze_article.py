"""Тесты use case LLM-анализа статьи."""

from dataclasses import replace
import unittest

from obs_chat_bot.capabilities.obsidian.application.articles.analysis import (
    AnalyzeArticleCommand,
    AnalyzeArticleError,
    AnalyzeArticleUseCase,
)
from obs_chat_bot.capabilities.obsidian.application.articles.errors import ArticleAnalysisError
from obs_chat_bot.capabilities.obsidian.application.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus


class AnalyzeArticleUseCaseTest(unittest.TestCase):
    """Проверяет сценарий анализа уже извлечённой статьи."""

    def test_execute_analyzes_article_and_saves_result(self) -> None:
        """Статья с очищенным текстом анализируется и получает статус analyzed."""
        article_repository = FakeArticleRepository(_article())
        analysis_repository = FakeAnalysisResultRepository()
        analyzer = FakeArticleAnalyzer()

        result = AnalyzeArticleUseCase(
            article_repository=article_repository,
            analyzer=analyzer,
            analysis_result_repository=analysis_repository,
        ).execute(AnalyzeArticleCommand(article_id=1))

        self.assertTrue(result.created)
        self.assertEqual(result.article.status, ArticleStatus.ANALYZED)
        self.assertEqual(result.analysis.id, 1)
        self.assertEqual(analyzer.article_ids, [1])
        self.assertEqual(
            article_repository.statuses,
            [ArticleStatus.ANALYZING, ArticleStatus.ANALYZED],
        )

    def test_execute_reuses_existing_analysis(self) -> None:
        """Повторный анализ возвращает последний сохранённый результат."""
        article = _article(status=ArticleStatus.ANALYZED)
        existing = _analysis_result(id=7)
        analyzer = FakeArticleAnalyzer()

        result = AnalyzeArticleUseCase(
            article_repository=FakeArticleRepository(article),
            analyzer=analyzer,
            analysis_result_repository=FakeAnalysisResultRepository(existing),
        ).execute(AnalyzeArticleCommand(article_id=1))

        self.assertFalse(result.created)
        self.assertEqual(result.analysis, existing)
        self.assertEqual(analyzer.article_ids, [])

    def test_execute_force_creates_new_analysis(self) -> None:
        """Принудительный анализ игнорирует сохранённый результат."""
        article = _article(status=ArticleStatus.ANALYZED)
        existing = _analysis_result(id=7)
        analyzer = FakeArticleAnalyzer()

        result = AnalyzeArticleUseCase(
            article_repository=FakeArticleRepository(article),
            analyzer=analyzer,
            analysis_result_repository=FakeAnalysisResultRepository(existing),
        ).execute(AnalyzeArticleCommand(article_id=1, force=True))

        self.assertTrue(result.created)
        self.assertEqual(analyzer.article_ids, [1])
        self.assertNotEqual(result.analysis.id, existing.id)

    def test_execute_rejects_article_without_cleaned_text(self) -> None:
        """Статья без очищенного текста не отправляется в LLM."""
        recorder = FakeProcessingErrorRecorder()

        with self.assertRaises(AnalyzeArticleError):
            AnalyzeArticleUseCase(
                article_repository=FakeArticleRepository(
                    replace(_article(), cleaned_text=None)
                ),
                analyzer=FakeArticleAnalyzer(),
                analysis_result_repository=FakeAnalysisResultRepository(),
                error_recorder=recorder,
            ).execute(AnalyzeArticleCommand(article_id=1, incoming_message_id=5))

        self.assertEqual(recorder.records[0]["stage"], ProcessingStage.ANALYSIS)
        self.assertEqual(recorder.records[0]["incoming_message_id"], 5)

    def test_execute_marks_article_failed_on_analyzer_error(self) -> None:
        """Ошибка LLM переводит статью в failed и сохраняет диагностику."""
        article_repository = FakeArticleRepository(_article())
        recorder = FakeProcessingErrorRecorder()

        with self.assertRaises(AnalyzeArticleError):
            AnalyzeArticleUseCase(
                article_repository=article_repository,
                analyzer=FakeArticleAnalyzer(error=ArticleAnalysisError("failed")),
                analysis_result_repository=FakeAnalysisResultRepository(),
                error_recorder=recorder,
            ).execute(AnalyzeArticleCommand(article_id=1))

        self.assertEqual(article_repository.article.status, ArticleStatus.FAILED)
        self.assertEqual(recorder.records[0]["stage"], ProcessingStage.ANALYSIS)


class FakeArticleRepository:
    """Памятная fake-реализация хранения статей для use case тестов."""

    def __init__(self, article: Article | None) -> None:
        self.article = article
        self.statuses: list[ArticleStatus] = []

    def create(self, article: Article) -> Article:
        """Не используется в сценарии анализа."""
        self.article = replace(article, id=1)
        return self.article

    def get_by_id(self, article_id: int) -> Article | None:
        """Возвращает статью, если ID совпадает."""
        if self.article is None or self.article.id != article_id:
            return None
        return self.article

    def find_by_normalized_url(
        self,
        _normalized_url: str,
        _app_user_id: int = 1,
    ) -> Article | None:
        """Не используется в сценарии анализа."""
        return None

    def find_by_text_hash(
        self,
        _text_hash: str,
        _app_user_id: int = 1,
    ) -> list[Article]:
        """Не используется в сценарии анализа."""
        return []

    def update_status(
        self,
        article_id: int,
        status: ArticleStatus,
    ) -> Article | None:
        """Обновляет статус статьи в памяти."""
        if self.article is None or self.article.id != article_id:
            return None
        self.statuses.append(status)
        self.article = replace(self.article, status=status)
        return self.article

    def update_content(
        self,
        article_id: int,
        *,
        title: str | None,
        cleaned_text: str,
        text_hash: str,
        status: ArticleStatus = ArticleStatus.EXTRACTED,
    ) -> Article | None:
        """Не используется в сценарии анализа."""
        if self.article is None or self.article.id != article_id:
            return None
        self.article = replace(
            self.article,
            title=title,
            cleaned_text=cleaned_text,
            text_hash=text_hash,
            status=status,
        )
        return self.article


class FakeArticleAnalyzer:
    """Fake LLM-анализатор для use case тестов."""

    def __init__(self, error: ArticleAnalysisError | None = None) -> None:
        self.article_ids: list[int] = []
        self._error = error

    def analyze(self, article: Article) -> ArticleAnalysisResult:
        """Возвращает результат анализа или заданную ошибку."""
        if self._error is not None:
            raise self._error
        if article.id is None:
            raise ValueError("article must contain id")
        self.article_ids.append(article.id)
        return _analysis_result(article_id=article.id)


class FakeAnalysisResultRepository:
    """Памятная fake-реализация хранения результатов анализа."""

    def __init__(self, existing: ArticleAnalysisResult | None = None) -> None:
        self._results = [existing] if existing is not None else []

    def save(self, result: ArticleAnalysisResult) -> ArticleAnalysisResult:
        """Сохраняет результат и назначает ID."""
        saved = replace(result, id=len(self._results) + 1)
        self._results.append(saved)
        return saved

    def get_by_id(self, result_id: int) -> ArticleAnalysisResult | None:
        """Возвращает результат по ID."""
        for result in self._results:
            if result.id == result_id:
                return result
        return None

    def get_latest_for_article(self, article_id: int) -> ArticleAnalysisResult | None:
        """Возвращает последний результат по статье."""
        for result in reversed(self._results):
            if result.article_id == article_id:
                return result
        return None


class FakeProcessingErrorRecorder:
    """Fake recorder диагностических ошибок обработки."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

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
        """Запоминает диагностическую ошибку в памяти."""
        self.records.append(
            {
                "article_id": article_id,
                "app_user_id": app_user_id,
                "incoming_message_id": incoming_message_id,
                "stage": stage,
                "error_type": error_type,
                "error_message": error_message,
            }
        )


def _article(status: ArticleStatus = ArticleStatus.EXTRACTED) -> Article:
    """Создаёт статью, готовую к анализу."""
    return Article(
        id=1,
        source_url="https://example.com/article",
        normalized_url="https://example.com/article",
        title="Article",
        cleaned_text="Clean article text",
        text_hash="hash",
        status=status,
    )


def _analysis_result(
    article_id: int = 1,
    id: int | None = None,
) -> ArticleAnalysisResult:
    """Создаёт результат анализа статьи."""
    return ArticleAnalysisResult(
        id=id,
        article_id=article_id,
        llm_model="fake-llm",
        prompt_version="article-summary-v1",
        result_text="## Кратко\nСтатья разобрана.",
    )


if __name__ == "__main__":
    unittest.main()
