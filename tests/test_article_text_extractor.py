"""Тесты извлечения чистого текста статьи из HTML."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from obs_chat_bot.capabilities.obsidian.domain.articles.errors import ArticleExtractionError
from obs_chat_bot.capabilities.obsidian.domain.articles.html import ArticleHtml
from obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor import (
    TrafilaturaArticleTextExtractor,
)


class TrafilaturaArticleTextExtractorTest(unittest.TestCase):
    """Проверяет adapter извлечения текста через `trafilatura`."""

    def test_extract_returns_clean_text_and_title(self) -> None:
        """Успешное извлечение возвращает текст и заголовок."""
        extractor = TrafilaturaArticleTextExtractor()
        html = _article_html("<html><title>Ignored</title><body>Raw</body></html>")

        with (
            patch(
                "obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor."
                "_extract_clean_text",
                return_value=" Clean article text ",
            ),
            patch(
                "obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor."
                "_extract_title",
                return_value="Article title",
            ),
        ):
            result = extractor.extract(html)

        self.assertEqual(result.source_url, html.source_url)
        self.assertEqual(result.final_url, html.final_url)
        self.assertEqual(result.title, "Article title")
        self.assertEqual(result.cleaned_text, "Clean article text")

    def test_extract_allows_missing_title(self) -> None:
        """Отсутствующий заголовок не ломает извлечение текста."""
        extractor = TrafilaturaArticleTextExtractor()
        html = _article_html("<html><body>Raw</body></html>")

        with (
            patch(
                "obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor."
                "_extract_clean_text",
                return_value="Clean article text",
            ),
            patch(
                "obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor."
                "_extract_title",
                return_value=None,
            ),
        ):
            result = extractor.extract(html)

        self.assertIsNone(result.title)
        self.assertEqual(result.cleaned_text, "Clean article text")

    def test_extract_rejects_empty_html(self) -> None:
        """Пустой HTML не передаётся в extractor."""
        extractor = TrafilaturaArticleTextExtractor()

        with self.assertRaises(ArticleExtractionError):
            extractor.extract(_article_html("   "))

    def test_extract_rejects_empty_clean_text(self) -> None:
        """Пустой результат `trafilatura` считается ошибкой."""
        extractor = TrafilaturaArticleTextExtractor()
        html = _article_html("<html><body>Raw</body></html>")

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor."
            "_extract_clean_text",
            return_value="   ",
        ):
            with self.assertRaises(ArticleExtractionError):
                extractor.extract(html)

    def test_extract_wraps_missing_trafilatura(self) -> None:
        """Отсутствие библиотеки превращается в application-ошибку."""
        extractor = TrafilaturaArticleTextExtractor()
        html = _article_html("<html><body>Raw</body></html>")

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor."
            "_load_trafilatura",
            side_effect=ArticleExtractionError("trafilatura is not installed"),
        ):
            with self.assertRaises(ArticleExtractionError):
                extractor.extract(html)

    def test_extract_title_reads_metadata_title(self) -> None:
        """Заголовок читается из metadata `trafilatura`."""
        from obs_chat_bot.capabilities.obsidian.data.extraction import trafilatura_article_extractor

        html = _article_html("<html><body>Raw</body></html>")
        fake_trafilatura = SimpleNamespace(
            metadata=SimpleNamespace(
                extract_metadata=lambda *_args, **_kwargs: SimpleNamespace(
                    title="  Metadata title  "
                )
            )
        )

        with patch.object(
            trafilatura_article_extractor,
            "_load_trafilatura",
            return_value=fake_trafilatura,
        ):
            title = trafilatura_article_extractor._extract_title(html)

        self.assertEqual(title, "Metadata title")


def _article_html(content: str) -> ArticleHtml:
    """Создаёт application-модель HTML для тестов."""
    return ArticleHtml(
        source_url="https://example.com/source",
        final_url="https://example.com/final",
        content=content,
        content_type="text/html",
    )


if __name__ == "__main__":
    unittest.main()
