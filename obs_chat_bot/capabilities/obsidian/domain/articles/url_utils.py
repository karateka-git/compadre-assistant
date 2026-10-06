from __future__ import annotations

from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse, urlunparse


# Параметры, которые обычно не меняют содержимое статьи, но ломают поиск дублей.
TRACKING_QUERY_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "yclid",
        "mc_cid",
        "mc_eid",
    }
)


class InvalidUrlError(ValueError):
    """Сообщает, что строка не является поддерживаемым URL статьи."""


def is_supported_article_url(value: str) -> bool:
    """Проверяет, можно ли рассматривать строку как URL для обработки.

    Args:
        value: Исходная строка от пользователя или из внешнего источника.

    Returns:
        True, если строка содержит URL с поддерживаемой схемой и доменом.
    """
    try:
        parse_supported_article_url(value)
    except InvalidUrlError:
        return False
    return True


def normalize_article_url(value: str) -> str:
    """Возвращает стабильный URL для поиска дублей статей.

    Нормализация сохраняет смысловую часть ссылки, но убирает фрагмент
    страницы и распространенные tracking-параметры, которые не должны
    создавать новую статью в базе.

    Args:
        value: Исходный URL.

    Returns:
        Нормализованный URL для поля ``normalized_url``.

    Raises:
        InvalidUrlError: Если URL пустой, не содержит домен или использует
            неподдерживаемую схему.
    """
    parsed = parse_supported_article_url(value)
    normalized_netloc = _normalize_netloc(parsed)
    normalized_path = parsed.path or "/"
    normalized_query = _normalize_query(parsed.query)

    return urlunparse(
        (
            parsed.scheme.lower(),
            normalized_netloc,
            normalized_path,
            "",
            normalized_query,
            "",
        )
    )


def parse_supported_article_url(value: str) -> ParseResult:
    """Разбирает строку как поддерживаемый URL статьи.

    Args:
        value: Исходная строка для разбора.

    Returns:
        Результат ``urllib.parse.urlparse`` для валидного URL.

    Raises:
        InvalidUrlError: Если строка пустая, не содержит домен или схема не
            входит в список поддерживаемых.
    """
    stripped_value = value.strip()
    if not stripped_value:
        raise InvalidUrlError("URL must not be empty")

    parsed = urlparse(stripped_value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidUrlError("URL scheme must be http or https")
    if not parsed.hostname:
        raise InvalidUrlError("URL must contain a host")

    return parsed


def _normalize_netloc(parsed: ParseResult) -> str:
    hostname = parsed.hostname
    if hostname is None:
        raise InvalidUrlError("URL must contain a host")

    netloc = hostname.lower()
    if parsed.port and not _is_default_port(parsed.scheme, parsed.port):
        netloc = f"{netloc}:{parsed.port}"

    return netloc


def _is_default_port(scheme: str, port: int) -> bool:
    return (scheme.lower(), port) in {("http", 80), ("https", 443)}


def _normalize_query(query: str) -> str:
    query_items = parse_qsl(query, keep_blank_values=True)
    meaningful_items = [
        (name, value)
        for name, value in query_items
        if not _is_tracking_param(name)
    ]
    return urlencode(meaningful_items, doseq=True)


def _is_tracking_param(name: str) -> bool:
    lower_name = name.lower()
    return lower_name.startswith("utm_") or lower_name in TRACKING_QUERY_PARAMS
