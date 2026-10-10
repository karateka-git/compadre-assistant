from __future__ import annotations

from typing import Any

from compadre_assistant.capabilities.obsidian.domain.articles.errors import ArticleExtractionError
from compadre_assistant.capabilities.obsidian.domain.articles.extracted import ExtractedArticle
from compadre_assistant.capabilities.obsidian.domain.articles.html import ArticleHtml
from compadre_assistant.capabilities.obsidian.domain.articles.ports import ArticleTextExtractor


class TrafilaturaArticleTextExtractor(ArticleTextExtractor):
    """Извлекает чистый текст статьи через библиотеку `trafilatura`."""

    def extract(self, html: ArticleHtml) -> ExtractedArticle:
        """Извлекает заголовок и очищенный текст из HTML.

        Args:
            html: HTML страницы, полученный загрузчиком.

        Returns:
            Очищенная статья в application-модели.

        Raises:
            ArticleExtractionError: Если HTML пустой, `trafilatura` недоступна
                или библиотека не смогла извлечь содержательный текст.
        """
        if not html.content.strip():
            raise ArticleExtractionError("HTML content must not be empty")

        cleaned_text = _extract_clean_text(html)
        if cleaned_text is None or not cleaned_text.strip():
            raise ArticleExtractionError("Could not extract article text from HTML")

        title = _extract_title(html)

        return ExtractedArticle(
            source_url=html.source_url,
            final_url=html.final_url,
            title=title,
            cleaned_text=cleaned_text.strip(),
        )


def _extract_clean_text(html: ArticleHtml) -> str | None:
    """Вызывает `trafilatura.extract` с настройками для plain text."""
    trafilatura = _load_trafilatura()
    return trafilatura.extract(
        html.content,
        url=html.final_url,
        output_format="txt",
        include_comments=False,
        include_tables=True,
    )


def _extract_title(html: ArticleHtml) -> str | None:
    """Пробует получить заголовок статьи из metadata `trafilatura`."""
    trafilatura = _load_trafilatura()
    metadata_module = getattr(trafilatura, "metadata", None)
    if metadata_module is None:
        return None

    extract_metadata = getattr(metadata_module, "extract_metadata", None)
    if extract_metadata is None:
        return None

    metadata = extract_metadata(html.content, default_url=html.final_url)
    title = getattr(metadata, "title", None)
    if title is None or not title.strip():
        return None
    return title.strip()


def _load_trafilatura() -> Any:
    """Загружает `trafilatura` только в момент реального извлечения."""
    try:
        import trafilatura
    except ModuleNotFoundError as error:
        raise ArticleExtractionError("trafilatura is not installed") from error

    return trafilatura
