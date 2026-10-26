"""Проверяет готовые части на границе renderer и API обоих каналов без сети."""

import asyncio
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from compadre_assistant.core.presentation.incoming.dispatcher import CallbackReplyDelivery
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction, IncomingResponse
from compadre_assistant.core.presentation.message_content import MessageContent, Paragraph, TextSpan, TextStyle, text_content
from compadre_assistant.presentation.telegram.bot import _register_handlers, safe_send_telegram_reply
from compadre_assistant.presentation.telegram.rendering import render_telegram_content
from compadre_assistant.presentation.vk.bot import _handle_update, _safe_send_vk_message, VkApiClient, VkTransientError
from compadre_assistant.presentation.vk.rendering import render_vk_content
from tests.test_telegram_bot import FakeAiogram, TelegramMessage
from tests.test_vk_bot import FakeVkClient, FakeHttpResponse, _update


class ChannelRenderingDeliveryTest(unittest.TestCase):
    """Проверяет адрес, порядок, оформление, plain fallback и границы повторов."""

    def setUp(self) -> None:
        """Готовит длинный ответ с динамическими данными и текстовым действием."""
        self.logger = logging.getLogger("test.rendered.delivery")
        self.response = IncomingResponse(
            MessageContent((
                Paragraph((TextSpan("Заголовок", TextStyle.EMPHASIS),)),
                Paragraph((TextSpan("<данные> & `literal` " * 450),)),
                Paragraph((TextSpan("/команда путь", TextStyle.CODE),)),
            )),
            actions=(ConfirmationAction("yes", "Подтвердить", "/confirm item 42 yes"),),
        )

    def test_telegram_direct_receipt_and_background_use_same_rendered_parts(self) -> None:
        """Все три пути сохраняют HTML, действия, последовательность и адрес."""
        expected = render_telegram_content(self.response.content, self.response.actions)
        message = TelegramMessage()
        message.text, message.message_id, message.from_user = "запрос", 1, None
        deliveries = []
        background_done = asyncio.Event()
        background_parts = []

        async def send_message(*, chat_id, text, parse_mode):
            background_parts.append((chat_id, text, parse_mode))
            if len(background_parts) == len(expected) * 2:
                background_done.set()

        message.send_message = send_message

        def processor(event, handler):
            delivery = CallbackReplyDelivery(handler)
            delivery.send(event.reply_address, self.response)
            deliveries.append(delivery)
            return self.response

        aiogram = FakeAiogram()
        _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)

        async def run():
            await aiogram.dispatcher.included_router.handler(message)
            await asyncio.to_thread(deliveries[0].send, ReplyAddress("telegram", "99"), self.response)
            await asyncio.wait_for(background_done.wait(), 2)

        asyncio.run(run())
        self.assertGreater(len(expected), 1)
        self.assertEqual(message.answers, [part.text for part in expected])
        self.assertEqual(message.parse_modes, ["HTML"] * len(expected))
        self.assertEqual(background_parts, [
            (chat, part.text, part.parse_mode)
            for chat in ("42", "99") for part in expected
        ])

    def test_vk_direct_receipt_and_background_use_same_rendered_parts(self) -> None:
        """VK передаёт локальные диапазоны каждой части и адрес ReplyDeliveryPort."""
        client = FakeVkClient()
        deliveries = []

        def processor(event, handler):
            delivery = CallbackReplyDelivery(handler)
            delivery.send(event.reply_address, self.response)
            deliveries.append(delivery)
            return self.response

        _handle_update(_update(1, 42, "запрос"), incoming_message_processor=processor, client=client, logger=self.logger)
        deliveries[0].send(ReplyAddress("vk", "99"), self.response)
        expected = render_vk_content(self.response.content, self.response.actions)
        self.assertGreater(len(expected), 1)
        self.assertEqual(client.messages, [(chat, part.text) for chat in (42, 42, 99) for part in expected])
        self.assertEqual(client.formats, [part.format_data for part in expected] * 3)
        self.assertIn("/confirm item 42 yes", client.messages[-1][1])
        self.assertNotIn("<code>", client.messages[-1][1])

    def test_immediate_response_needs_no_background_delivery(self) -> None:
        """Оба адаптера отправляют результат, когда processor не использует callback."""
        response = IncomingResponse(MessageContent((Paragraph((TextSpan("Готово", TextStyle.EMPHASIS),)),)))
        processor = Mock(return_value=response)
        aiogram = FakeAiogram()
        message = TelegramMessage()
        message.text, message.message_id, message.from_user = "запрос", 1, None
        _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)
        asyncio.run(aiogram.dispatcher.included_router.handler(message))
        self.assertEqual(message.answers, ["<b>Готово</b>"])
        self.assertEqual(message.parse_modes, ["HTML"])
        client = FakeVkClient()
        _handle_update(_update(1, 42, "запрос"), incoming_message_processor=processor, client=client, logger=self.logger)
        self.assertEqual(client.messages, [(42, "Готово")])
        self.assertEqual(json.loads(client.formats[0])["items"], [{"type": "bold", "offset": 0, "length": 6}])

    def test_short_replies_bypass_core_but_keep_renderer(self) -> None:
        """Ответ до ядра экранируется Telegram и остаётся буквальным в VK."""
        short = "Пришли <текст> & /help"
        processor = Mock(side_effect=AssertionError("ядро не должно вызываться"))
        aiogram = FakeAiogram()
        message = TelegramMessage()
        message.text = None
        _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger, empty_message_reply=short)
        asyncio.run(aiogram.dispatcher.included_router.handler(message))
        self.assertEqual(message.answers, ["Пришли &lt;текст&gt; &amp; /help"])
        self.assertEqual(message.parse_modes, ["HTML"])
        client = FakeVkClient()
        _handle_update(_update(1, 42, " "), incoming_message_processor=processor, client=client, logger=self.logger, empty_message_reply=short)
        self.assertEqual(client.messages, [(42, short)])
        self.assertEqual(client.formats, [None])
        processor.assert_not_called()

    def test_vk_transport_preserves_format_json_and_omits_plain_parameter(self) -> None:
        """HTTP получает готовый JSON без повторного разбиения и пустого format_data."""
        formatted = render_vk_content(MessageContent((Paragraph((TextSpan("Жирный", TextStyle.EMPHASIS),)),)))[0]
        requests = []

        def urlopen(request, timeout):
            requests.append(parse_qs(request.data.decode()))
            return FakeHttpResponse()

        client = VkApiClient(token="test")
        with patch("compadre_assistant.presentation.vk.bot.urlopen", urlopen):
            client.send_message(peer_id=42, text=formatted.text, format_data=formatted.format_data, random_id=123)
            client.send_message(peer_id=99, text="plain", random_id=124)
        self.assertEqual(requests[0]["format_data"], [formatted.format_data])
        self.assertEqual(requests[0]["message"], ["Жирный"])
        self.assertEqual(requests[0]["random_id"], ["123"])
        self.assertEqual(requests[0]["peer_id"], ["42"])
        self.assertNotIn("format_data", requests[1])

    def test_vk_unsupported_style_and_emoji_choose_plain_before_send(self) -> None:
        """CODE и неоднозначные emoji offsets не добавляют маркеров или повторов."""
        client = FakeVkClient()
        response = IncomingResponse(MessageContent((Paragraph((
            TextSpan("😀Заголовок", TextStyle.EMPHASIS), TextSpan("/help", TextStyle.CODE),
        )),)))
        _safe_send_vk_message(client, peer_id=42, response=response, logger=self.logger)
        self.assertEqual(client.messages, [(42, "😀Заголовок/help")])
        self.assertEqual(client.formats, [None])

    def test_vk_retry_keeps_chunk_format_and_id_without_replaying_previous_part(self) -> None:
        """Повторяется только сбойная часть с прежним JSON и random_id."""
        calls = []
        client = Mock()
        response = IncomingResponse(MessageContent((Paragraph((TextSpan("абвгдеёж", TextStyle.EMPHASIS),)),)))

        def send(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise VkTransientError("temporary")

        client.send_message.side_effect = send
        with self.assertLogs(self.logger, level="WARNING"):
            _safe_send_vk_message(client, peer_id=42, response=response, logger=self.logger, limit=4, sleeper=lambda _: None)
        self.assertEqual([call["text"] for call in calls], ["абвг", "деёж", "деёж"])
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(json.loads(calls[2]["format_data"])["items"], [{"type": "bold", "offset": 0, "length": 4}])

    def test_failed_telegram_part_stops_without_plain_fallback(self) -> None:
        """Окончательный сбой не пересылает ответ заново и не отправляет хвост."""
        calls = []

        async def answer(text, *, parse_mode):
            calls.append((text, parse_mode))
            if len(calls) == 2:
                raise RuntimeError("uncertain delivery")

        target = SimpleNamespace(chat=SimpleNamespace(id=42), answer=answer)
        with self.assertLogs(self.logger, level="ERROR"):
            asyncio.run(safe_send_telegram_reply(target, IncomingResponse(text_content("abcdefghij")), logger=self.logger, limit=4))
        self.assertEqual(calls, [("abcd", "HTML"), ("efgh", "HTML")])
