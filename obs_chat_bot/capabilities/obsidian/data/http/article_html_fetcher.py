from __future__ import annotations

from collections.abc import Callable
import logging
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from obs_chat_bot.capabilities.obsidian.domain.articles.errors import ArticleFetchError
from obs_chat_bot.capabilities.obsidian.domain.articles.html import ArticleHtml
from obs_chat_bot.capabilities.obsidian.domain.articles.ports import ArticleHtmlFetcher
from obs_chat_bot.capabilities.obsidian.data.http.url_safety import UnsafeUrlError, validate_public_http_url


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_FETCH_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.5
DEFAULT_USER_AGENT = "obsChatBot/0.1"
LOGGER = logging.getLogger(__name__)


class UrllibArticleHtmlFetcher(ArticleHtmlFetcher):
    """Загружает HTML страницы статьи через стандартный HTTP-клиент Python.

    Args:
        timeout_seconds: Максимальное время ожидания ответа.
        user_agent: Значение HTTP-заголовка `User-Agent`.
        max_attempts: Максимальное число попыток, включая первую.
        retry_base_delay_seconds: Базовая задержка exponential backoff.
        sleeper: Функция ожидания между попытками.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        max_attempts: int = DEFAULT_FETCH_ATTEMPTS,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not user_agent.strip():
            raise ValueError("user_agent must not be empty")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_base_delay_seconds < 0:
            raise ValueError("retry_base_delay_seconds must not be negative")

        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent
        self._max_attempts = max_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._sleeper = sleeper

    def fetch(self, url: str) -> ArticleHtml:
        """Загружает HTML по URL.

        Args:
            url: URL страницы статьи.

        Returns:
            Загруженный HTML и метаданные ответа.

        Raises:
            ArticleFetchError: Если сервер вернул ошибку, ответ не похож на HTML
                или произошла сетевая ошибка.
        """
        try:
            validate_public_http_url(url)
        except (UnsafeUrlError, ValueError) as error:
            raise ArticleFetchError(f"Unsafe article URL: {error}") from error

        request = Request(url, headers={"User-Agent": self._user_agent})

        for attempt in range(1, self._max_attempts + 1):
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    status = response.getcode()
                    if status < 200 or status >= 300:
                        raise ArticleFetchError(f"Unexpected HTTP status: {status}")

                    content_type = response.headers.get("Content-Type")
                    if not _is_html_content_type(content_type):
                        raise ArticleFetchError(
                            f"Response is not HTML: {content_type or 'unknown'}"
                        )

                    charset = response.headers.get_content_charset() or "utf-8"
                    content = response.read().decode(charset, errors="replace")
                    final_url = response.geturl()
                    try:
                        validate_public_http_url(final_url)
                    except (UnsafeUrlError, ValueError) as error:
                        raise ArticleFetchError(
                            f"Unsafe final article URL: {error}"
                        ) from error
            except HTTPError as error:
                raise ArticleFetchError(
                    f"HTTP error while fetching article: {error.code}"
                ) from error
            except (URLError, TimeoutError, socket.timeout) as error:
                if attempt >= self._max_attempts:
                    raise ArticleFetchError(
                        f"Network error while fetching article: {error}"
                    ) from error
                delay = self._retry_base_delay_seconds * (2 ** (attempt - 1))
                LOGGER.warning(
                    "Article fetch failed; retrying: failed_attempt=%s/%s "
                    "retry_delay_seconds=%.3f error_type=%s error=%s",
                    attempt,
                    self._max_attempts,
                    delay,
                    type(error).__name__,
                    error,
                )
                self._sleeper(delay)
                continue

            if not content.strip():
                raise ArticleFetchError("Fetched HTML is empty")

            return ArticleHtml(
                source_url=url,
                final_url=final_url,
                content=content,
                content_type=content_type,
            )

        raise RuntimeError("Article fetch retry loop completed unexpectedly")


def _is_html_content_type(content_type: str | None) -> bool:
    if content_type is None:
        return True
    return "html" in content_type.lower()
