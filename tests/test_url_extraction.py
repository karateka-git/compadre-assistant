"""Тесты извлечения ссылок из входящего текста."""

import unittest

from compadre_assistant.capabilities.obsidian.domain.articles.url_extraction import extract_first_supported_url


class UrlExtractionTest(unittest.TestCase):
    """Проверяет поиск первой поддерживаемой ссылки в сообщении."""

    def test_extract_first_supported_url_returns_first_http_link(self) -> None:
        """Из текста возвращается первая HTTP/HTTPS-ссылка."""
        url = extract_first_supported_url(
            "Посмотри https://example.com/a и https://example.com/b"
        )

        self.assertEqual(url, "https://example.com/a")

    def test_extract_first_supported_url_strips_trailing_punctuation(self) -> None:
        """Закрывающая пунктуация после URL не попадает в ссылку."""
        url = extract_first_supported_url("Ссылка: https://example.com/article).")

        self.assertEqual(url, "https://example.com/article")

    def test_extract_first_supported_url_ignores_unsupported_links(self) -> None:
        """Неподдерживаемые схемы не возвращаются как статья."""
        url = extract_first_supported_url("Файл ftp://example.com/file")

        self.assertIsNone(url)

    def test_extract_first_supported_url_returns_none_without_link(self) -> None:
        """Текст без ссылки возвращает `None`."""
        url = extract_first_supported_url("Просто текст без URL")

        self.assertIsNone(url)


if __name__ == "__main__":
    unittest.main()
