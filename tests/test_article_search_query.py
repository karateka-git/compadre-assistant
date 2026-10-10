"""Тесты compact query builder Этапа 10.6."""

import unittest

from compadre_assistant.capabilities.obsidian.domain.search.article_query import (
    MAX_LEXICAL_QUERY_CHARS,
    MAX_SEMANTIC_QUERY_CHARS,
    ArticleSearchQueryBuilder,
)
from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article


class ArticleSearchQueryBuilderTest(unittest.TestCase):
    """Проверяет разделение semantic/lexical текста и hard limits."""

    def test_builds_queries_from_summary_and_topics_only(self) -> None:
        """Основные идеи и польза не загрязняют поисковые представления."""
        query = ArticleSearchQueryBuilder().build(
            article=_article(title="Масштабирование Telegram-ботов"),
            analysis=_analysis(
                """## Кратко
Переход от polling к webhook и очередям.
## Основные идеи
Эта подробность не должна попасть в запрос.
## Практическая польза
И эта рекомендация тоже не должна попасть.
## Темы
- Telegram Bot API
- webhook
- очереди
"""
            ),
        )

        self.assertIn("Название: Масштабирование", query.semantic_text)
        self.assertIn("Кратко: Переход", query.semantic_text)
        self.assertIn("Темы: Telegram Bot API, webhook, очереди", query.semantic_text)
        self.assertEqual(
            query.lexical_text,
            "Масштабирование Telegram-ботов\nTelegram Bot API, webhook, очереди",
        )
        self.assertNotIn("подробность", query.semantic_text)
        self.assertNotIn("рекомендация", query.lexical_text)

    def test_hard_limits_large_analysis(self) -> None:
        """Ответ до 8000 символов не превращается в столь же большой query."""
        query = ArticleSearchQueryBuilder().build(
            article=_article(title="Очень длинный заголовок " * 50),
            analysis=_analysis(
                "## Кратко\n"
                + "содержательная сводка " * 500
                + "\n## Темы\n"
                + "semantic search " * 200
            ),
        )

        self.assertLessEqual(len(query.semantic_text), MAX_SEMANTIC_QUERY_CHARS)
        self.assertLessEqual(len(query.lexical_text), MAX_LEXICAL_QUERY_CHARS)

    def test_unstructured_analysis_uses_bounded_fallback(self) -> None:
        """Старый или нарушенный Markdown остаётся пригоден для поиска."""
        query = ArticleSearchQueryBuilder().build(
            article=_article(title=None),
            analysis=_analysis("Короткая неструктурированная сводка статьи."),
        )

        self.assertEqual(
            query.semantic_text,
            "Кратко: Короткая неструктурированная сводка статьи.",
        )
        self.assertEqual(
            query.lexical_text,
            "Короткая неструктурированная сводка статьи.",
        )

    def test_accepts_heading_aliases_and_numbered_topics(self) -> None:
        """Небольшие вариации LLM headings не ломают извлечение полей."""
        query = ArticleSearchQueryBuilder().build(
            article=_article(title="RAG"),
            analysis=_analysis(
                "## Краткая сводка:\nПоиск по заметкам.\n"
                "## Ключевые темы\n1. embeddings\n2) app_user_id\n3. FTS5"
            ),
        )

        self.assertIn("Кратко: Поиск по заметкам.", query.semantic_text)
        self.assertIn("Темы: embeddings, app_user_id, FTS5", query.semantic_text)

    def test_rejects_analysis_from_another_user_or_article(self) -> None:
        """Query builder сохраняет изоляцию по article ID и app_user_id."""
        builder = ArticleSearchQueryBuilder()
        with self.assertRaisesRegex(ValueError, "article"):
            builder.build(
                article=_article(),
                analysis=_analysis("текст", article_id=2),
            )
        with self.assertRaisesRegex(ValueError, "app user"):
            builder.build(
                article=_article(),
                analysis=_analysis("текст", app_user_id=2),
            )


def _article(*, title: str | None = "Статья") -> Article:
    """Создаёт сохранённую статью пользователя для unit-теста."""
    return Article(
        id=1,
        app_user_id=1,
        source_url="https://example.com/article",
        normalized_url="https://example.com/article",
        title=title,
    )


def _analysis(
    result_text: str,
    *,
    article_id: int = 1,
    app_user_id: int = 1,
) -> ArticleAnalysisResult:
    """Создаёт сохранённый результат анализа для unit-теста."""
    return ArticleAnalysisResult(
        id=10,
        app_user_id=app_user_id,
        article_id=article_id,
        llm_model="fake-llm",
        prompt_version="article-summary-v1",
        result_text=result_text,
    )


if __name__ == "__main__":
    unittest.main()
