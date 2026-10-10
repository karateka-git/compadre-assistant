"""Тесты доменной модели результата анализа статьи."""

from datetime import UTC, datetime
import unittest

from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult


class ArticleAnalysisResultTest(unittest.TestCase):
    """Проверяет инварианты результата LLM-анализа статьи."""

    def test_analysis_result_accepts_valid_data(self) -> None:
        """Корректный результат анализа создаётся без ошибок."""
        created_at = datetime(2026, 7, 16, tzinfo=UTC)

        result = ArticleAnalysisResult(
            id=1,
            article_id=10,
            llm_model="gpt-4.1-mini",
            prompt_version="article-summary-v1",
            result_text="## Кратко\nТекст анализа.",
            created_at=created_at,
        )

        self.assertEqual(result.article_id, 10)
        self.assertEqual(result.created_at, created_at)

    def test_analysis_result_rejects_invalid_ids(self) -> None:
        """Неположительные ID не считаются валидными ссылками на записи."""
        with self.assertRaises(ValueError):
            ArticleAnalysisResult(
                article_id=0,
                llm_model="gpt-4.1-mini",
                prompt_version="article-summary-v1",
                result_text="analysis",
            )

        with self.assertRaises(ValueError):
            ArticleAnalysisResult(
                id=0,
                article_id=10,
                llm_model="gpt-4.1-mini",
                prompt_version="article-summary-v1",
                result_text="analysis",
            )

    def test_analysis_result_rejects_empty_text_fields(self) -> None:
        """Пустые текстовые поля не проходят доменную валидацию."""
        invalid_values = [
            {"llm_model": " ", "prompt_version": "article-summary-v1", "result_text": "analysis"},
            {"llm_model": "gpt-4.1-mini", "prompt_version": " ", "result_text": "analysis"},
            {"llm_model": "gpt-4.1-mini", "prompt_version": "article-summary-v1", "result_text": " "},
        ]

        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ArticleAnalysisResult(article_id=10, **values)


if __name__ == "__main__":
    unittest.main()
