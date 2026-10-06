"""Тесты application-контрактов LLM-анализа статей."""

from datetime import UTC, datetime
import unittest

from obs_chat_bot.capabilities.obsidian.domain.articles.errors import ArticleAnalysisError
from obs_chat_bot.capabilities.obsidian.domain.articles.ports import (
    ArticleAnalysisResultRepository,
    ArticleAnalyzer,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article


class ArticleAnalysisPortsTest(unittest.TestCase):
    """Проверяет ожидаемую форму ports для будущего use case анализа."""

    def test_analyzer_contract_returns_domain_result(self) -> None:
        """Analyzer port возвращает доменную модель результата анализа."""
        analyzer: ArticleAnalyzer = FakeArticleAnalyzer()

        result = analyzer.analyze(_article())

        self.assertEqual(result.article_id, 10)
        self.assertEqual(result.llm_model, "fake-llm")
        self.assertIn("Кратко", result.result_text)

    def test_repository_contract_saves_and_reads_latest_result(self) -> None:
        """Repository port сохраняет результат и отдаёт последний по статье."""
        repository: ArticleAnalysisResultRepository = FakeAnalysisResultRepository()
        first = _analysis_result("Первая версия")
        second = _analysis_result("Вторая версия")

        saved_first = repository.save(first)
        saved_second = repository.save(second)
        latest = repository.get_latest_for_article(10)

        self.assertEqual(saved_first.id, 1)
        self.assertEqual(saved_second.id, 2)
        self.assertEqual(latest, saved_second)

    def test_analysis_error_and_stage_are_available_for_pipeline(self) -> None:
        """Pipeline может использовать отдельную ошибку и stage анализа."""
        error = ArticleAnalysisError("LLM request failed")

        self.assertEqual(str(error), "LLM request failed")
        self.assertEqual(ProcessingStage.ANALYSIS.value, "analysis")


class FakeArticleAnalyzer:
    """Минимальная fake-реализация LLM-анализатора для contract-теста."""

    def analyze(self, article: Article) -> ArticleAnalysisResult:
        """Возвращает детерминированный результат анализа статьи."""
        if article.id is None:
            raise ValueError("article must contain id")
        if not article.cleaned_text:
            raise ValueError("article must contain cleaned_text")

        return ArticleAnalysisResult(
            article_id=article.id,
            llm_model="fake-llm",
            prompt_version="article-summary-v1",
            result_text="## Кратко\nСтатья разобрана.",
        )


class FakeAnalysisResultRepository:
    """Памятная fake-реализация хранения результатов анализа."""

    def __init__(self) -> None:
        self._results: list[ArticleAnalysisResult] = []

    def save(self, result: ArticleAnalysisResult) -> ArticleAnalysisResult:
        """Сохраняет результат в памяти и назначает ID."""
        saved = ArticleAnalysisResult(
            id=len(self._results) + 1,
            article_id=result.article_id,
            llm_model=result.llm_model,
            prompt_version=result.prompt_version,
            result_text=result.result_text,
            created_at=datetime(2026, 7, 16, tzinfo=UTC),
        )
        self._results.append(saved)
        return saved

    def get_latest_for_article(self, article_id: int) -> ArticleAnalysisResult | None:
        """Возвращает последний сохранённый результат для статьи."""
        for result in reversed(self._results):
            if result.article_id == article_id:
                return result
        return None


def _article() -> Article:
    """Создаёт статью, готовую к LLM-анализу."""
    return Article(
        id=10,
        source_url="https://example.com/article",
        normalized_url="https://example.com/article",
        title="Example",
        cleaned_text="Текст статьи.",
        text_hash="hash",
    )


def _analysis_result(text: str) -> ArticleAnalysisResult:
    """Создаёт несохранённый результат анализа для fake repository."""
    return ArticleAnalysisResult(
        article_id=10,
        llm_model="fake-llm",
        prompt_version="article-summary-v1",
        result_text=f"## Кратко\n{text}",
    )


if __name__ == "__main__":
    unittest.main()
