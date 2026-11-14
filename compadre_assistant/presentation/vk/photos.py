"""Преобразует метаданные фото VK в ленивые вложения."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import aiohttp

from compadre_assistant.core.presentation.incoming.events import ImageAttachment, ImageLoadError


# Поддомены CDN для фото VK. Основание перечислено в IMAGE_PLAN.md.
_PHOTO_CDN_HOSTS = ("userapi.com", "vkuserphoto.ru")
_PHOTO_MAX_BYTES = 10 * 1024 * 1024
_PHOTO_TOTAL_SECONDS = 15
_PHOTO_CHUNK_BYTES = 64 * 1024
_PHOTO_SIGNATURES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def vk_photo_attachments(attachments: object) -> tuple[ImageAttachment, ...]:
    """Создаёт по одному вложению на каждый объект VK с типом `photo`.

    Args:
        attachments: Поле `attachments` из сообщения VK Long Poll.

    Returns:
        Вложения в исходном порядке. Фото без пригодной ссылки выдаёт
        безопасную ошибку только при чтении.
    """
    if not isinstance(attachments, list):
        return ()
    return tuple(
        _photo_attachment(_select_url(attachment.get("photo")))
        for attachment in attachments
        if isinstance(attachment, dict) and attachment.get("type") == "photo"
    )


def _select_url(photo: object) -> str | None:
    """Выбирает наибольший доступный размер по данным VK."""
    if not isinstance(photo, dict) or not isinstance(photo.get("sizes"), list):
        return None
    best_url = None
    best_area = -1
    for variant in photo["sizes"]:
        if not isinstance(variant, dict):
            continue
        width, height = variant.get("width"), variant.get("height")
        if type(width) is not int or type(height) is not int or width < 0 or height < 0:
            continue
        area = width * height
        url = variant.get("url") or variant.get("src")
        if area > best_area and _is_photo_cdn_url(url):
            best_area = area
            best_url = url
    return best_url


def _is_photo_cdn_url(url: object) -> bool:
    """Принимает только HTTPS на обычном порту CDN с путём к файлу."""
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.netloc.lower() in {host, f"{host}:443"}
            and all(_DNS_LABEL.fullmatch(label) for label in host.split("."))
            and any(host == domain or host.endswith(f".{domain}") for domain in _PHOTO_CDN_HOSTS)
            and parsed.path not in {"", "/"}
            and "#" not in url
            and "\\" not in url
        )
    except ValueError:
        return False


def _photo_attachment(url: str | None) -> ImageAttachment:
    """Передаёт CDN URL только функции, которая скачивает вложение."""
    def read() -> bytes:
        if url is None:
            raise ImageLoadError()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ImageLoadError()
        # VK вызывает loader из синхронного worker. Закрытие этого цикла
        # не ждёт системный DNS после отмены запроса по общему таймауту.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_download(url))
        finally:
            loop.close()

    return ImageAttachment(read)


async def _download(url: str) -> bytes:
    """Получает JPEG или PNG с ограничением времени и фактического размера."""
    async with asyncio.timeout(_PHOTO_TOTAL_SECONDS):
        connector = aiohttp.TCPConnector(use_dns_cache=False)
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
        ) as session:
            async with session.get(
                url,
                headers={"Accept-Encoding": "identity"},
                allow_redirects=False,
            ) as response:
                signature = _PHOTO_SIGNATURES.get(response.content_type)
                declared_size = response.content_length
                if (response.status != 200 or signature is None
                        or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                        or (declared_size is not None and not 0 < declared_size <= _PHOTO_MAX_BYTES)):
                    raise ImageLoadError()
                data = bytearray()
                while chunk := await response.content.read(_PHOTO_CHUNK_BYTES):
                    if len(data) + len(chunk) > _PHOTO_MAX_BYTES:
                        raise ImageLoadError()
                    data.extend(chunk)
                    if len(data) >= len(signature) and not data.startswith(signature):
                        raise ImageLoadError()
                if not data.startswith(signature) or len(data) == len(signature):
                    raise ImageLoadError()
                return bytes(data)
