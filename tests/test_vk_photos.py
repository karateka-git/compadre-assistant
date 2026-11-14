"""Проверяет VK-вложения и ограничения загрузки фото без живого CDN."""

import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

import aiohttp

from compadre_assistant.core.presentation.incoming.events import ImageLoadError
from compadre_assistant.presentation.vk.photos import vk_photo_attachments


JPEG = b"\xff\xd8\xffimage"
PNG = b"\x89PNG\r\n\x1a\nimage"
CDN_URL = "https://sun9-15.userapi.com:443/image.jpg?signature=private"


def _photo(*variants: dict) -> dict:
    """Собирает одно вложение VK с заданными вариантами фото."""
    return {"type": "photo", "photo": {"sizes": list(variants)}}


class _CdnResponse:
    """Имитирует поток и запоминает количество прочитанных блоков."""

    def __init__(self, chunks=(), *, status=200, mime="image/jpeg", length=None, headers=None,
                 delay=0, failure=None):
        self.status = status
        self.content_type = mime
        self.content_length = length
        self.headers = headers or {}
        self.content = self
        self.chunks = chunks
        self.pulled = 0
        self.delay = delay
        self.failure = failure

    async def read(self, _chunk_size):
        if self.failure:
            raise self.failure
        if self.pulled == len(self.chunks):
            return b""
        if self.delay:
            await asyncio.sleep(self.delay)
        chunk = self.chunks[self.pulled]
        self.pulled += 1
        return chunk


class _CdnSession:
    """Подменяет aiohttp ClientSession без открытия сетевого соединения."""

    def __init__(self, response, *, delay=0):
        self.response = response
        self.delay = delay
        self.requests = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    @asynccontextmanager
    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        yield self.response


class VkPhotoDownloadTest(unittest.TestCase):
    """Проверяет, что загрузчик отклоняет неподходящие адреса и ответы CDN."""

    def setUp(self):
        self.setup_transport = patch("compadre_assistant.presentation.vk.photos.aiohttp.TCPConnector")
        self.setup_transport.start()
        self.addCleanup(self.setup_transport.stop)

    def test_reads_highest_resolution_cdn_variant_only_on_request(self):
        photos = vk_photo_attachments([
            _photo({"url": "https://sun9.userapi.com/small.jpg", "width": 32, "height": 32},
                   {"url": CDN_URL, "width": 640, "height": 640},
                   {"url": "https://cdn.example.org/large.jpg", "width": 1024, "height": 1024}),
            _photo({"src": "https://sun2.vkuserphoto.ru/p.png", "width": 128, "height": 128}),
        ])
        first = _CdnSession(_CdnResponse([JPEG[:2], JPEG[2:]], length=len(JPEG)))
        second = _CdnSession(_CdnResponse([PNG], mime="image/png"))
        with self._sessions(first, second) as sessions:
            sessions.assert_not_called()
            self.assertEqual(len(photos), 2)
            self.assertNotIn("signature", repr(photos))
            self.assertEqual(photos[0].read_bytes(), JPEG)
            self.assertEqual(photos[1].read_bytes(), PNG)

        self.assertEqual(first.requests, [(CDN_URL, {
            "headers": {"Accept-Encoding": "identity"}, "allow_redirects": False,
        })])
        self.assertEqual(second.requests[0][0], "https://sun2.vkuserphoto.ru/p.png")
        self.assertTrue(first.closed and second.closed)
        self.assertFalse(sessions.call_args.kwargs["trust_env"])
        self.assertFalse(sessions.call_args.kwargs["auto_decompress"])
        self.assertIsInstance(sessions.call_args.kwargs["cookie_jar"], aiohttp.DummyCookieJar)
        self.assertNotIn("auth", sessions.call_args.kwargs)

    def test_rejects_non_cdn_urls_without_network(self):
        with self._sessions() as sessions:
            for url in (
                "http://sun9.userapi.com/image.jpg",
                "https://sun9.userapi.com:444/image.jpg",
                "https://account:pwd@sun9.userapi.com/image.jpg",
                "https://sun9.userapi.com.evil/image.jpg",
                "https://notuserapi.com/image.jpg",
                "https://sun9..userapi.com/image.jpg",
                "https://sun9.userapi.com@127.0.0.1/image.jpg",
                "https://127.0.0.1/image.jpg",
                "https://sun9.userapi.com\\evil/image.jpg",
                "https://sun9.userapi.com/image.jpg#private",
            ):
                with self.subTest(url=url):
                    photo, = vk_photo_attachments([_photo({"url": url, "width": 64, "height": 64})])
                    with self.assertRaises(ImageLoadError):
                        photo.read_bytes()
            photo, = vk_photo_attachments([{"type": "photo"}])
            with self.assertRaises(ImageLoadError):
                photo.read_bytes()
            sessions.assert_not_called()

    def test_rejects_unsuitable_headers_before_reading_body(self):
        for response in (
            _CdnResponse([JPEG], status=302, headers={"Location": "https://other.test/image.jpg"}),
            _CdnResponse([JPEG], status=206),
            _CdnResponse([JPEG], mime="text/html"),
            _CdnResponse([JPEG], length=0),
            _CdnResponse([JPEG], length=10 * 1024 * 1024 + 1),
            _CdnResponse([JPEG], headers={"Content-Encoding": "gzip"}),
        ):
            with self.subTest(status=response.status, mime=response.content_type,
                              length=response.content_length, headers=response.headers):
                session = _CdnSession(response)
                with self._sessions(session), self.assertRaises(ImageLoadError):
                    self._attachment().read_bytes()
                self.assertEqual(response.pulled, 0)
                self.assertTrue(session.closed)

    def test_reads_only_until_size_limit_or_signature_mismatch(self):
        cases = [
            (_CdnResponse([JPEG, b"too large", b"end"]), 2),
            (_CdnResponse([PNG, b"end"]), 1),
            (_CdnResponse([b""]), 1),
        ]
        for response, pulled in cases:
            with self.subTest(response=response.chunks):
                with self._sessions(_CdnSession(response)), patch(
                    "compadre_assistant.presentation.vk.photos._PHOTO_MAX_BYTES", len(PNG),
                ), self.assertRaises(ImageLoadError):
                    self._attachment().read_bytes()
                self.assertEqual(response.pulled, pulled)

    def test_total_timeout_covers_headers_and_slow_stream(self):
        sessions = [
            _CdnSession(_CdnResponse([JPEG]), delay=0.2),
            _CdnSession(_CdnResponse([JPEG, b"tail", b"end"], delay=0.04)),
        ]
        for session in sessions:
            with self.subTest(delay=session.delay):
                with self._sessions(session), patch(
                    "compadre_assistant.presentation.vk.photos._PHOTO_TOTAL_SECONDS", 0.09,
                ), self.assertRaises(ImageLoadError):
                    self._attachment().read_bytes()
                self.assertTrue(session.closed)
                self.assertLess(session.response.pulled, len(session.response.chunks))

    def test_read_error_hides_cdn_url_and_reason(self):
        failure = RuntimeError(CDN_URL)
        with self._sessions(_CdnSession(_CdnResponse(failure=failure))):
            with self.assertRaises(ImageLoadError) as caught:
                self._attachment().read_bytes()
        self.assertNotIn(CDN_URL, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def _sessions(self, *sessions):
        return patch("compadre_assistant.presentation.vk.photos.aiohttp.ClientSession", side_effect=sessions)

    def _attachment(self):
        photo, = vk_photo_attachments([_photo({"url": CDN_URL, "width": 64, "height": 64})])
        return photo
