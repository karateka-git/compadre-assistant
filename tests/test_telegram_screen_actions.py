"""Проверяет проекцию общего контракта и доставку действий в Telegram."""

from dataclasses import replace
from itertools import count
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiogram

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import BoundReplyAction, IncomingResponse, ReplyAction, ResponseView
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.telegram.bot import (
    TELEGRAM_ACTION_LAYOUT, _incoming_message_from_telegram, _register_handlers,
    _serialize_telegram_inline_keyboard, safe_send_telegram_reply,
)
from tests.test_screen_actions_core import ActionCapability
from tests.test_telegram_bot import FakeAiogram


class TelegramActionProjectionTest(unittest.TestCase):
    """Проверяет безопасную геометрию и взаимоисключающие цели Bot API."""

    def test_both_scopes_and_url_keep_declared_order_and_rows(self):
        """Действия раздела и сообщения остаются рядом с исходным текстом."""
        response = IncomingResponse(text_content("Экран"), bound_actions=(
            BoundReplyAction("Открыть", "a" * 32, "response", scope=ActionScope.SECTION),
            BoundReplyAction("1. Выбор", "b" * 32, "response", purpose=ActionPurpose.SELECTION),
            BoundReplyAction("Сайт", "", "response", row=2, url="https://example.com"),
            BoundReplyAction("Ещё сайт", "", "response", row=2, url="https://example.org"),
        ))
        rows = _serialize_telegram_inline_keyboard(response, aiogram).inline_keyboard
        self.assertEqual([[button.text for button in row] for row in rows],
                         [["Открыть"], ["1. Выбор"], ["Сайт", "Ещё сайт"]])
        self.assertEqual(rows[0][0].callback_data, "a" * 32)
        self.assertIsNone(rows[0][0].url)
        self.assertEqual(rows[2][0].url, "https://example.com")
        self.assertIsNone(rows[2][0].callback_data)

    def test_keyboard_limits_match_advertised_capacity_without_cropping(self):
        """Переполнение отвергает клавиатуру целиком, не меняя видимые номера."""
        action = BoundReplyAction("Объект", "a" * 32, "response")
        response = IncomingResponse(text_content("Введи номер или /cancel."))
        cases = (
            tuple(replace(action, token=str(index)) for index in range(101)),
            tuple(replace(action, token=str(index), row=0) for index in range(9)),
            (replace(action, label="x" * 65),),
            (replace(action, token="я" * 33),),
            (replace(action, token="same"), replace(action, token="same")),
            (replace(action, row=-1),),
            (replace(action, row=True),),
            (replace(action, row=0), replace(action, row=1, token="b"), replace(action, row=0, token="c")),
            (action, replace(action, response_id="other", token="b")),
            (replace(action, url="https://example.com"),),
            (replace(action, token="", url="javascript:alert(1)"),),
        )
        for actions in cases:
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                _serialize_telegram_inline_keyboard(replace(response, bound_actions=actions), aiogram)
        boundary = tuple(replace(action, token=str(index)) for index in range(100))
        self.assertEqual(len(_serialize_telegram_inline_keyboard(
            replace(response, bound_actions=boundary), aiogram).inline_keyboard), TELEGRAM_ACTION_LAYOUT.max_rows)
        unicode_button = replace(action, token="я" * 32)
        self.assertEqual(_serialize_telegram_inline_keyboard(
            replace(response, bound_actions=(unicode_button,)), aiogram).inline_keyboard[0][0].callback_data,
            unicode_button.token)

    def test_raw_url_and_empty_actions_never_create_a_keyboard(self):
        """URL из описания владельца требует успешной привязки ядром."""
        response = IncomingResponse(text_content("Текст"), suggested_actions=(
            ReplyAction("Сайт", url="https://example.com"),
        ))
        self.assertIsNone(_serialize_telegram_inline_keyboard(response, aiogram))
        self.assertIsNone(_serialize_telegram_inline_keyboard(IncomingResponse(text_content("Текст")), aiogram))


class TelegramScreenDeliveryTest(unittest.IsolatedAsyncioTestCase):
    """Проводит нейтральный экран через процессор и обработчики Telegram."""

    def setUp(self):
        """Готовит изолированный runtime и наблюдаемый Bot API."""
        self.path = Path(self.enterContext(TemporaryDirectory(prefix="telegram-screen-actions-"))) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            create_user_identity_service(connection).register_with_name(
                IncomingIdentity("telegram", "1001", "1001"), display_name="Тест",
            )
        self.owner = ActionCapability()
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(self.owner,))
        self.event = IncomingEvent("telegram", "1001", "request", "/selected", "1001")
        self.processor(self.event)
        self.ids = count(42)
        self.bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=lambda **_: SimpleNamespace(message_id=next(self.ids))),
            edit_message_text=AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(message_id=kwargs["message_id"])),
            edit_message_reply_markup=AsyncMock(),
        )
        self.logger = logging.getLogger("test.telegram.screen-actions")
        sdk = FakeAiogram()
        _register_handlers(sdk.dispatcher, sdk, incoming_message_processor=self.processor, logger=self.logger)
        self.router = sdk.dispatcher.included_router
        self.chat = SimpleNamespace(id=1001)
        self.author = SimpleNamespace(id=1001)

    def message(self, text="/choose"):
        """Создаёт сообщение с настоящим адресом тестового чата."""
        async def answer(value, **kwargs):
            return await self.bot.send_message(chat_id=1001, text=value, **kwargs)

        return SimpleNamespace(chat=self.chat, from_user=self.author, message_id=10,
                               text=text, bot=self.bot, answer=answer)

    def issue(self, response):
        """Получает привязанный экран без подмены проверки ядра."""
        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            return self.processor(_incoming_message_from_telegram(self.message()))

    async def press(self, token, message_id=42):
        """Передаёт callback реальному обработчику адаптера."""
        callback = SimpleNamespace(
            id="callback", from_user=self.author, bot=self.bot, data=token, answer=AsyncMock(),
            message=SimpleNamespace(chat=self.chat, message_id=message_id),
        )
        await self.router.callback_handler(callback)
        callback.answer.assert_awaited_once()

    async def test_limits_arrive_before_rendering_for_messages_and_callbacks(self):
        """Оба входа передают владельцу тот же бюджет страницы."""
        await self.router.handler(self.message())
        self.assertEqual(self.owner.layout, TELEGRAM_ACTION_LAYOUT)
        self.assertEqual(self.owner.layout.selection_capacity(reserved_rows=2, reserved_buttons=3), 97)
        token = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.owner.layout = None
        await self.press(token)
        self.assertEqual(self.owner.layout, TELEGRAM_ACTION_LAYOUT)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")

    async def test_url_and_callback_attach_only_to_last_chunk(self):
        """Многочастный текст получает одну общую клавиатуру в конце."""
        response = self.issue(IncomingResponse(text_content("Длинный текст. " * 12 + "Команда: /choose first."), suggested_actions=(
            ReplyAction("Выбрать", "/choose first", scope=ActionScope.SECTION),
            ReplyAction("Сайт", url="https://example.com"),
        )))
        self.assertTrue(await safe_send_telegram_reply(self.message(), response, logger=self.logger, limit=50))
        sent = self.bot.send_message.await_args_list
        self.assertGreater(len(sent), 1)
        self.assertTrue(all("reply_markup" not in call.kwargs for call in sent[:-1]))
        rows = sent[-1].kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[1][0].url, "https://example.com")
        self.assertIsNone(rows[1][0].callback_data)

    async def test_hidden_buttons_preserve_text_manual_command_and_delivery(self):
        """Невместимая подпись сохраняет полный текст и ручную команду."""
        confirmed = []
        content = "Выбери через /choose stable-id или вернись через /selected."
        response = self.issue(IncomingResponse(text_content(content), suggested_actions=(
            ReplyAction("Слишком длинная подпись " * 10, "/choose stable-id", purpose=ActionPurpose.SELECTION),
        ), view=ResponseView("choice", on_delivered=lambda: confirmed.append(True))))
        with self.assertLogs(self.logger, level="WARNING"):
            self.assertTrue(await safe_send_telegram_reply(self.message(), response, logger=self.logger))
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], content)
        self.assertNotIn("reply_markup", self.bot.send_message.await_args.kwargs)
        self.assertEqual(confirmed, [True])
        await self.router.handler(self.message("/choose stable-id"))
        self.assertEqual(self.owner.calls[-1][1].text, "/choose stable-id")

    async def test_managed_edit_keeps_url_and_removes_it_with_empty_action_set(self):
        """URL участвует в том же edit, что текст и callback-кнопки."""
        description = IncomingResponse(text_content("Открой /choose first."), suggested_actions=(
            ReplyAction("Сайт", url="https://example.com"),
        ), view=ResponseView("screen"))
        self.assertTrue(await safe_send_telegram_reply(self.message(), self.issue(description), logger=self.logger))
        updated = replace(description, view=ResponseView("screen", update=True, unavailable=text_content("Повтори /choose.")))
        self.assertTrue(await safe_send_telegram_reply(self.message(), self.issue(updated), logger=self.logger))
        rows = self.bot.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[0][0].url, "https://example.com")
        self.assertTrue(await safe_send_telegram_reply(
            self.message(), self.issue(replace(updated, suggested_actions=())), logger=self.logger,
        ))
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard, [])

    async def test_multipart_replacement_confirms_final_message_before_cleanup(self):
        """Новая многочастная страница активируется после последней части."""
        response = IncomingResponse(text_content("Выбор /choose first."), suggested_actions=(
            ReplyAction("1. Объект", "/choose first", purpose=ActionPurpose.SELECTION),
            ReplyAction("Сайт", url="https://example.com"),
        ), view=ResponseView("selection", cleanup_previous=True, allow_multiple_messages=True))
        self.assertTrue(await safe_send_telegram_reply(self.message(), self.issue(response), logger=self.logger))
        replacement = self.issue(replace(response, content=text_content("Новая страница. " * 12 + "/choose first."),
                                        view=replace(response.view, update=True, unavailable=text_content("Повтори /choose."))))
        self.assertTrue(await safe_send_telegram_reply(self.message(), replacement, logger=self.logger, limit=45))
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=1001, message_id=42, reply_markup=None)
        final_id = 42 + self.bot.send_message.await_count - 1
        await self.press(replacement.bound_actions[0].token, final_id)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")

    async def test_failed_replacement_leaves_old_keyboard_and_new_callback_inactive(self):
        """Сбой новой части не снимает прежний экран и не активирует новую цель."""
        response = IncomingResponse(text_content("Выбор /choose first."), suggested_actions=(
            ReplyAction("1. Объект", "/choose first", purpose=ActionPurpose.SELECTION),
        ), view=ResponseView("selection", cleanup_previous=True, allow_multiple_messages=True))
        old = self.issue(response)
        await safe_send_telegram_reply(self.message(), old, logger=self.logger)
        replacement = self.issue(replace(response, content=text_content("Новая страница. " * 12)))
        self.bot.send_message.side_effect = RuntimeError("send failed")
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertFalse(await safe_send_telegram_reply(
                self.message(), replacement, logger=self.logger, limit=40,
            ))
        self.bot.edit_message_reply_markup.assert_not_awaited()
        self.processor.select_action(replace(self.event, callback_message_id="42"), replacement.bound_actions[0].token)
        self.assertEqual(self.owner.calls, [])
        self.processor.select_action(replace(self.event, callback_message_id="42"), old.bound_actions[0].token)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")

    async def test_failed_ordinary_callback_reply_does_not_remove_source_keyboard(self):
        """Даже неуправляемая клавиатура снимается только после доставки ответа."""
        await self.router.handler(self.message())
        token = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.bot.send_message.side_effect = RuntimeError("send failed")
        with self.assertLogs(self.logger, level="ERROR"):
            await self.press(token)
        self.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_stale_url_view_does_not_reach_bot_api(self):
        """Устаревший резерв доставки не публикует URL старого поколения."""
        description = IncomingResponse(text_content("Открой /choose."), suggested_actions=(
            ReplyAction("Сайт", url="https://example.com"),
        ), view=ResponseView("url"))
        old = self.issue(description)
        current = self.issue(description)
        self.assertTrue(await safe_send_telegram_reply(self.message(), current, logger=self.logger))
        sent_count = self.bot.send_message.await_count
        self.assertFalse(await safe_send_telegram_reply(self.message(), old, logger=self.logger))
        self.assertEqual(self.bot.send_message.await_count, sent_count)
        self.bot.edit_message_text.assert_not_awaited()

    async def test_disabled_buttons_advertise_zero_capacity_and_preserve_manual_text(self):
        """Владелец узнаёт отсутствие кнопок до форматирования ответа."""
        with patch("compadre_assistant.presentation.telegram.bot.TELEGRAM_INLINE_ACTIONS_ENABLED", False):
            event = _incoming_message_from_telegram(self.message())
        self.assertEqual(event.action_layout.selection_capacity(), 0)
        response = self.processor(event)
        self.assertEqual(self.owner.layout.selection_capacity(), 0)
        self.assertTrue(await safe_send_telegram_reply(
            self.message(), response, logger=self.logger, include_bound_actions=False,
        ))
        self.assertNotIn("reply_markup", self.bot.send_message.await_args.kwargs)
        self.assertIn("/choose", self.bot.send_message.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
