from __future__ import annotations

from ipaddress import ip_address

from obs_chat_bot.capabilities.obsidian.domain.articles.url_utils import parse_supported_article_url


class UnsafeUrlError(ValueError):
    """Сообщает, что URL небезопасен для server-side загрузки."""


def validate_public_http_url(url: str) -> None:
    """Проверяет URL на базовые SSRF-риски перед HTTP-запросом.

    Args:
        url: URL, который планируется загрузить сервером.

    Raises:
        UnsafeUrlError: Если URL ведёт на локальный или служебный адрес.
        InvalidUrlError: Если URL не является поддерживаемым HTTP(S)-URL.
    """
    parsed = parse_supported_article_url(url)
    hostname = parsed.hostname
    if hostname is None:
        raise UnsafeUrlError("URL must contain a host")

    normalized_host = hostname.rstrip(".").lower()
    if _is_blocked_hostname(normalized_host) or _is_blocked_ip_literal(normalized_host):
        raise UnsafeUrlError("URL host is not allowed")


def _is_blocked_hostname(hostname: str) -> bool:
    return (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    )


def _is_blocked_ip_literal(hostname: str) -> bool:
    try:
        address = ip_address(hostname)
    except ValueError:
        return False

    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )
