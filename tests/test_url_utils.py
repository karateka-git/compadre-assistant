"""Тесты нормализации URL статей."""

import unittest

from compadre_assistant.capabilities.obsidian.domain.articles.url_utils import (
    InvalidUrlError,
    is_supported_article_url,
    normalize_article_url,
)


class ArticleUrlUtilsTest(unittest.TestCase):
    """Проверяет подготовку URL к сохранению и поиску дублей."""

    def test_normalize_article_url_strips_noise(self) -> None:
        """URL приводится к стабильному виду без tracking-частей."""
        normalized_url = normalize_article_url(
            "  HTTPS://Example.COM:443/articles/42?utm_source=tg&id=7#comments  "
        )

        self.assertEqual(normalized_url, "https://example.com/articles/42?id=7")

    def test_normalize_article_url_keeps_non_default_port(self) -> None:
        """Нестандартный порт сохраняется как часть адреса."""
        normalized_url = normalize_article_url("http://example.com:8080/news")

        self.assertEqual(normalized_url, "http://example.com:8080/news")

    def test_normalize_article_url_adds_root_path(self) -> None:
        """URL без пути получает явный корневой путь."""
        normalized_url = normalize_article_url("https://example.com")

        self.assertEqual(normalized_url, "https://example.com/")

    def test_normalize_article_url_removes_known_tracking_params(self) -> None:
        """Типовые tracking-параметры не участвуют в поиске дублей."""
        normalized_url = normalize_article_url(
            "https://example.com/post?fbclid=abc&gclid=def&yclid=ghi&slug=main"
        )

        self.assertEqual(normalized_url, "https://example.com/post?slug=main")

    def test_is_supported_article_url_accepts_http_and_https(self) -> None:
        """HTTP и HTTPS считаются поддерживаемыми схемами."""
        self.assertTrue(is_supported_article_url("http://example.com/a"))
        self.assertTrue(is_supported_article_url("https://example.com/a"))

    def test_is_supported_article_url_rejects_invalid_values(self) -> None:
        """Пустые строки, относительные пути и другие схемы отклоняются."""
        self.assertFalse(is_supported_article_url(""))
        self.assertFalse(is_supported_article_url("/article/42"))
        self.assertFalse(is_supported_article_url("ftp://example.com/file"))

    def test_normalize_article_url_raises_for_invalid_url(self) -> None:
        """Некорректный URL дает явную ошибку."""
        with self.assertRaises(InvalidUrlError):
            normalize_article_url("example.com/article")


if __name__ == "__main__":
    unittest.main()
