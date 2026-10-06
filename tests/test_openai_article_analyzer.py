"""Тесты OpenAI-compatible adapter анализа статей."""

from types import SimpleNamespace
import unittest

from obs_chat_bot.capabilities.obsidian.domain.articles.errors import ArticleAnalysisError
from obs_chat_bot.capabilities.obsidian.data.llm.openai_article_analyzer import (
    MAX_ANALYSIS_RESPONSE_CHARS,
    MAX_ANALYSIS_RESPONSE_TOKENS,
    OpenAIArticleAnalyzer,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article


class OpenAIArticleAnalyzerTest(unittest.TestCase):
    """Проверяет adapter без реальных LLM-запросов."""

    def test_analyze_returns_domain_result_from_chat_completion(self) -> None:
        """Текст ответа SDK превращается в результат анализа статьи."""
        client = FakeOpenAIClient(result_text="## Кратко\nГотовый анализ.")
        analyzer = OpenAIArticleAnalyzer(
            base_url="https://llm.example/v1",
            api_key="token",
            model="fake-model",
            client=client,
        )

        result = analyzer.analyze(_article())

        self.assertEqual(result.article_id, 1)
        self.assertEqual(result.llm_model, "fake-model")
        self.assertEqual(result.prompt_version, "article-summary-v1")
        self.assertIn("Готовый анализ", result.result_text)
        self.assertEqual(client.requests[0]["model"], "fake-model")
        self.assertEqual(client.requests[0]["max_tokens"], MAX_ANALYSIS_RESPONSE_TOKENS)
        self.assertIn("Основные идеи", client.requests[0]["messages"][1]["content"])

    def test_analyze_rejects_article_without_text(self) -> None:
        """Статья без очищенного текста не отправляется в SDK."""
        analyzer = OpenAIArticleAnalyzer(
            base_url="https://llm.example/v1",
            api_key="token",
            model="fake-model",
            client=FakeOpenAIClient(result_text="unused"),
        )

        with self.assertRaises(ValueError):
            analyzer.analyze(
                Article(
                    id=1,
                    source_url="https://example.com/article",
                    normalized_url="https://example.com/article",
                )
            )

    def test_analyze_wraps_client_errors(self) -> None:
        """Ошибка SDK превращается в application-ошибку анализа."""
        analyzer = OpenAIArticleAnalyzer(
            base_url="https://llm.example/v1",
            api_key="token",
            model="fake-model",
            client=FakeOpenAIClient(error=RuntimeError("network failed")),
        )

        with self.assertRaises(ArticleAnalysisError):
            analyzer.analyze(_article())

    def test_analyze_rejects_empty_response(self) -> None:
        """Пустой ответ LLM считается ошибкой анализа."""
        analyzer = OpenAIArticleAnalyzer(
            base_url="https://llm.example/v1",
            api_key="token",
            model="fake-model",
            client=FakeOpenAIClient(result_text=" "),
        )

        with self.assertRaises(ArticleAnalysisError):
            analyzer.analyze(_article())

    def test_analyze_limits_long_response(self) -> None:
        """Слишком длинный ответ LLM сокращается перед сохранением."""
        analyzer = OpenAIArticleAnalyzer(
            base_url="https://llm.example/v1",
            api_key="token",
            model="fake-model",
            client=FakeOpenAIClient(result_text="x" * (MAX_ANALYSIS_RESPONSE_CHARS + 50)),
        )

        result = analyzer.analyze(_article())

        self.assertLess(len(result.result_text), MAX_ANALYSIS_RESPONSE_CHARS + 100)
        self.assertIn("Ответ LLM был сокращен", result.result_text)


class FakeOpenAIClient:
    """Fake OpenAI SDK client с вложенным `chat.completions.create`."""

    def __init__(
        self,
        *,
        result_text: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.requests: list[dict[str, object]] = []
        completions = _FakeCompletions(self.requests, result_text, error)
        self.chat = SimpleNamespace(completions=completions)


class _FakeCompletions:
    """Fake endpoint `chat.completions`."""

    def __init__(
        self,
        requests: list[dict[str, object]],
        result_text: str | None,
        error: Exception | None,
    ) -> None:
        self._requests = requests
        self._result_text = result_text
        self._error = error

    def create(self, **kwargs: object) -> object:
        """Запоминает запрос и возвращает fake response."""
        self._requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self._result_text),
                )
            ]
        )


def _article() -> Article:
    """Создаёт статью с очищенным текстом для adapter-тестов."""
    return Article(
        id=1,
        source_url="https://example.com/article",
        normalized_url="https://example.com/article",
        title="Article",
        cleaned_text="Clean text",
        text_hash="hash",
    )


if __name__ == "__main__":
    unittest.main()
