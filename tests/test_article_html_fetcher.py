"""Тесты HTTP-загрузчика HTML статей."""

from email.message import Message
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from obs_chat_bot.capabilities.obsidian.application.articles.errors import ArticleFetchError
from obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher import UrllibArticleHtmlFetcher


class FakeHttpResponse:
    """Минимальный fake HTTP-ответ для тестов `UrllibArticleHtmlFetcher`."""

    def __init__(
        self,
        *,
        status: int = 200,
        content: bytes = b"<html><body>Article</body></html>",
        content_type: str = "text/html; charset=utf-8",
        final_url: str = "https://example.com/article",
    ) -> None:
        self._status = status
        self._content = content
        self._final_url = final_url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        """Возвращает HTTP-статус fake-ответа."""
        return self._status

    def geturl(self) -> str:
        """Возвращает финальный URL fake-ответа."""
        return self._final_url

    def read(self) -> bytes:
        """Возвращает тело fake-ответа."""
        return self._content


class UrllibArticleHtmlFetcherTest(unittest.TestCase):
    """Проверяет загрузку HTML через стандартный HTTP-клиент."""

    def test_fetch_returns_article_html(self) -> None:
        """Успешный HTML-ответ преобразуется в application-модель."""
        fetcher = UrllibArticleHtmlFetcher(timeout_seconds=1)

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            return_value=FakeHttpResponse(),
        ):
            result = fetcher.fetch("https://example.com/article")

        self.assertEqual(result.source_url, "https://example.com/article")
        self.assertEqual(result.final_url, "https://example.com/article")
        self.assertEqual(result.content, "<html><body>Article</body></html>")
        self.assertEqual(result.content_type, "text/html; charset=utf-8")

    def test_fetch_rejects_non_success_status(self) -> None:
        """HTTP-статус вне диапазона 2xx превращается в ошибку загрузки."""
        fetcher = UrllibArticleHtmlFetcher(timeout_seconds=1)

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            return_value=FakeHttpResponse(status=500),
        ):
            with self.assertRaises(ArticleFetchError):
                fetcher.fetch("https://example.com/article")

    def test_fetch_rejects_non_html_response(self) -> None:
        """Ответ не-HTML типа не проходит как страница статьи."""
        fetcher = UrllibArticleHtmlFetcher(timeout_seconds=1)

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            return_value=FakeHttpResponse(content_type="application/json"),
        ):
            with self.assertRaises(ArticleFetchError):
                fetcher.fetch("https://example.com/article")

    def test_fetch_rejects_empty_html(self) -> None:
        """Пустое тело ответа считается ошибкой загрузки."""
        fetcher = UrllibArticleHtmlFetcher(timeout_seconds=1)

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            return_value=FakeHttpResponse(content=b"   "),
        ):
            with self.assertRaises(ArticleFetchError):
                fetcher.fetch("https://example.com/article")

    def test_fetch_wraps_http_error(self) -> None:
        """HTTP-ошибка не повторяется и превращается в application-ошибку."""
        delays: list[float] = []
        fetcher = UrllibArticleHtmlFetcher(
            timeout_seconds=1,
            sleeper=delays.append,
        )

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            side_effect=HTTPError(
                url="https://example.com/article",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,
            ),
        ) as opener:
            with self.assertRaises(ArticleFetchError):
                fetcher.fetch("https://example.com/article")

        opener.assert_called_once()
        self.assertEqual(delays, [])

    def test_fetch_wraps_network_error(self) -> None:
        """Сетевая ошибка после исчерпания попыток становится application-ошибкой."""
        delays: list[float] = []
        fetcher = UrllibArticleHtmlFetcher(
            timeout_seconds=1,
            retry_base_delay_seconds=0.5,
            sleeper=delays.append,
        )

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            side_effect=URLError("offline"),
        ) as opener:
            with self.assertRaises(ArticleFetchError):
                fetcher.fetch("https://example.com/article")

        self.assertEqual(opener.call_count, 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_fetch_retries_network_error_and_returns_response(self) -> None:
        """Временная сетевая ошибка повторяется до успешного HTML-ответа."""
        delays: list[float] = []
        fetcher = UrllibArticleHtmlFetcher(
            timeout_seconds=1,
            retry_base_delay_seconds=0.5,
            sleeper=delays.append,
        )

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            side_effect=[URLError("temporary"), FakeHttpResponse()],
        ) as opener:
            result = fetcher.fetch("https://example.com/article")

        self.assertEqual(result.content, "<html><body>Article</body></html>")
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(delays, [0.5])

    def test_fetch_rejects_localhost_without_request(self) -> None:
        """Локальный URL отклоняется до HTTP-запроса."""
        fetcher = UrllibArticleHtmlFetcher(timeout_seconds=1)

        with patch("obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen") as opener:
            with self.assertRaises(ArticleFetchError):
                fetcher.fetch("http://127.0.0.1/admin")

        opener.assert_not_called()

    def test_fetch_rejects_unsafe_redirect_target(self) -> None:
        """Redirect на локальный адрес не проходит финальную проверку."""
        fetcher = UrllibArticleHtmlFetcher(timeout_seconds=1)

        with patch(
            "obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher.urlopen",
            return_value=FakeHttpResponse(final_url="http://localhost/internal"),
        ):
            with self.assertRaises(ArticleFetchError):
                fetcher.fetch("https://example.com/article")

    def test_fetch_validates_timeout(self) -> None:
        """Неположительный timeout отклоняется сразу."""
        with self.assertRaises(ValueError):
            UrllibArticleHtmlFetcher(timeout_seconds=0)

    def test_fetch_validates_retry_settings(self) -> None:
        """Некорректные параметры retry отклоняются при создании fetcher."""
        with self.assertRaises(ValueError):
            UrllibArticleHtmlFetcher(max_attempts=0)
        with self.assertRaises(ValueError):
            UrllibArticleHtmlFetcher(retry_base_delay_seconds=-0.1)


if __name__ == "__main__":
    unittest.main()
