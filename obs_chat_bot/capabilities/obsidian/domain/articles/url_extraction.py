from __future__ import annotations

import re

from obs_chat_bot.capabilities.obsidian.domain.articles.url_utils import is_supported_article_url


URL_CANDIDATE_PATTERN = re.compile(r"https?://[^\s<>\"]+")
TRAILING_URL_PUNCTUATION = ".,;:!?)]}'\""


def extract_first_supported_url(text: str) -> str | None:
    """Возвращает первую поддерживаемую ссылку из текста сообщения.

    Args:
        text: Текст входящего сообщения.

    Returns:
        Первая HTTP/HTTPS-ссылка или `None`, если подходящей ссылки нет.
    """
    for match in URL_CANDIDATE_PATTERN.finditer(text):
        candidate = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        if is_supported_article_url(candidate):
            return candidate
    return None
