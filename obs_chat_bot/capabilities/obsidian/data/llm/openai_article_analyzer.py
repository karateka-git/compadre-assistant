from __future__ import annotations

from typing import Any

from obs_chat_bot.capabilities.obsidian.application.articles.errors import ArticleAnalysisError
from obs_chat_bot.capabilities.obsidian.application.articles.ports import ArticleAnalyzer
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article


DEFAULT_PROMPT_VERSION = "article-summary-v1"
MAX_ARTICLE_TEXT_CHARS = 24_000
MAX_ANALYSIS_RESPONSE_TOKENS = 1200
MAX_ANALYSIS_RESPONSE_CHARS = 8_000


class OpenAIArticleAnalyzer(ArticleAnalyzer):
    """Анализирует статьи через OpenAI-compatible Chat Completions API.

    Args:
        base_url: Базовый URL OpenAI-compatible API.
        api_key: API key провайдера.
        model: Имя модели для анализа статей.
        prompt_version: Версия prompt, сохраняемая вместе с результатом.
        client: Optional готовый SDK-клиент, полезный для тестов.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._prompt_version = prompt_version
        self._client = client

    def analyze(self, article: Article) -> ArticleAnalysisResult:
        """Возвращает Markdown-анализ очищенного текста статьи.

        Args:
            article: Сохранённая статья с ID и очищенным текстом.

        Returns:
            Доменная модель результата анализа без ID.

        Raises:
            ValueError: Если статья не содержит ID или очищенный текст.
            ArticleAnalysisError: Если LLM-запрос завершился ошибкой или вернул
                пустой ответ.
        """
        if article.id is None:
            raise ValueError("article must contain id")
        if not article.cleaned_text or not article.cleaned_text.strip():
            raise ValueError("article must contain cleaned_text")

        try:
            response = self._get_client().chat.completions.create(
                model=self._model,
                messages=_build_messages(article),
                temperature=0.2,
                max_tokens=MAX_ANALYSIS_RESPONSE_TOKENS,
            )
        except Exception as error:
            raise ArticleAnalysisError(f"LLM request failed: {error}") from error

        result_text = _limit_response_text(_extract_response_text(response))
        if not result_text:
            raise ArticleAnalysisError("LLM returned empty analysis")

        return ArticleAnalysisResult(
            article_id=article.id,
            llm_model=self._model,
            prompt_version=self._prompt_version,
            result_text=result_text,
        )

    def _get_client(self) -> Any:
        """Создаёт SDK-клиент лениво, чтобы тесты не зависели от `openai`."""
        if self._client is None:
            try:
                from openai import OpenAI
            except ModuleNotFoundError as error:
                raise ArticleAnalysisError("openai package is not installed") from error

            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
            )
        return self._client


def _build_messages(article: Article) -> list[dict[str, str]]:
    """Строит сообщения Chat Completions для анализа статьи."""
    title = article.title or "без заголовка"
    text = (article.cleaned_text or "")[:MAX_ARTICLE_TEXT_CHARS]

    return [
        {
            "role": "system",
            "content": (
                "Ты помогаешь быстро понять сохранённые статьи. "
                "Отвечай на русском языке готовым Markdown-текстом без преамбулы."
            ),
        },
        {
            "role": "user",
            "content": (
                "Проанализируй статью и верни Markdown с разделами:\n"
                "## Кратко\n"
                "## Основные идеи\n"
                "## Практическая польза\n"
                "## Темы\n\n"
                f"Название: {title}\n"
                f"URL: {article.source_url}\n\n"
                f"Текст статьи:\n{text}"
            ),
        },
    ]


def _extract_response_text(response: Any) -> str:
    """Достаёт текст первого варианта ответа Chat Completions."""
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as error:
        raise ArticleAnalysisError("LLM response has unexpected format") from error

    if not isinstance(content, str):
        raise ArticleAnalysisError("LLM response content is not text")

    return content.strip()


def _limit_response_text(text: str) -> str:
    """Ограничивает размер LLM-ответа перед сохранением в БД."""
    stripped_text = text.strip()
    if len(stripped_text) <= MAX_ANALYSIS_RESPONSE_CHARS:
        return stripped_text

    return (
        stripped_text[:MAX_ANALYSIS_RESPONSE_CHARS].rstrip()
        + "\n\n[Ответ LLM был сокращен до безопасного размера.]"
    )
