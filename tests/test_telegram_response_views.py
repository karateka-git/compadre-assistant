"""Проверяет доставку управляемого ответа Telegram без запросов в сеть."""

import asyncio
import logging
import unittest
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse, ReplyAction
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.telegram.bot import _register_handlers, safe_send_telegram_reply
from tests.test_incoming_boundary import FakeCapability
from tests.test_response_views import RECOVERY, EditableCapability
from tests.test_telegram_bot import FakeAiogram


class TelegramResponseViewsTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет выход Bot API и кнопки через настоящий processor."""

    def setUp(self) -> None:
        """Готовит отдельную БД, известный чат и зарегистрированные handlers."""
        self.db_path = Path(self.enterContext(TemporaryDirectory(prefix="telegram-response-views-"))) / "app.db"
        with connect_database(self.db_path) as connection:
            apply_migrations(connection)
            identity = create_user_identity_service(connection)
            identity.register_with_name(IncomingIdentity("telegram", "1001", "1001"), display_name="Тест")
        self.chat = SimpleNamespace(id=1001)
        self.author = SimpleNamespace(id=1001)
        self.message_ids = count(100)
        self.sent_ids = count(42)
        self.bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=lambda **_: SimpleNamespace(message_id=next(self.sent_ids))),
            edit_message_text=AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(message_id=kwargs["message_id"])),
            edit_message_reply_markup=AsyncMock(),
        )
        self.logger = logging.getLogger("test.telegram.response_views")
        self.start_runtime()
        self.processor(IncomingEvent("telegram", "1001", "99", "/selected", "1001"))

    def start_runtime(self) -> None:
        """Запускает свежие handlers и процессный реестр на имеющейся БД."""
        self.capability = EditableCapability()
        self.processor = ChannelMessageProcessor(
            database_path=self.db_path, capabilities=(self.capability, FakeCapability("other", "/other")),
        )
        aiogram = FakeAiogram()
        _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=self.processor, logger=self.logger)
        self.router = aiogram.dispatcher.included_router

    def message(self, text: str) -> SimpleNamespace:
        """Создаёт входящее SDK-сообщение с отдельными ID запроса и ответа."""
        async def answer(value, **kwargs):
            return await self.bot.send_message(chat_id=self.chat.id, text=value, **kwargs)

        return SimpleNamespace(
            chat=self.chat, message_id=next(self.message_ids), from_user=self.author,
            text=text, bot=self.bot, answer=answer,
        )

    async def send(self, text: str) -> None:
        """Вызывает обработчик обычного входящего сообщения."""
        await self.router.handler(self.message(text))

    async def press(self, token: str, source: object = 42) -> None:
        """Вызывает обработчик нажатия с ID события, отличным от ID сообщения."""
        callback = SimpleNamespace(
            id="click", from_user=self.author, bot=self.bot, data=token,
            message=SimpleNamespace(chat=self.chat, message_id=source), answer=AsyncMock(),
        )
        await self.router.callback_handler(callback)
        callback.answer.assert_awaited_once()

    async def open_view(self) -> list[str]:
        """Публикует представление и возвращает видимые callback tokens."""
        await self.send("/show_view")
        keyboard = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        return [row[0].callback_data for row in keyboard]

    async def test_choices_and_manual_command_replace_text_and_keyboard_in_one_message(self) -> None:
        """После двух выборов ручная команда снимает кнопки одним edit."""
        first, old_sibling, _ = await self.open_view()
        for invalid in (True, 0, "42", 43):
            with self.subTest(invalid=invalid):
                await self.press(first, invalid)
                self.assertEqual(self.capability.choices, [])
        self.bot.edit_message_text.assert_not_awaited()
        sent = self.bot.send_message.await_count

        await self.press(first)
        self.bot.edit_message_text.assert_awaited_once()
        updated = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual(updated["message_id"], 42)
        self.assertEqual(int(updated["chat_id"]), self.chat.id)
        self.assertEqual(updated["parse_mode"], "HTML")
        self.assertIn("Состояние: A.", updated["text"])
        current = updated["reply_markup"].inline_keyboard
        self.assertEqual([row[0].text for row in current], ["B", "C"])
        new_sibling = current[0][0].callback_data
        self.assertNotEqual(new_sibling, old_sibling)
        self.assertEqual(self.bot.send_message.await_count, sent)

        await self.press(old_sibling)
        self.bot.edit_message_text.assert_awaited_once()
        self.assertIn("больше недоступна", self.bot.send_message.await_args.kwargs["text"])
        await self.press(new_sibling)
        self.assertEqual(self.capability.choices, ["A", "B"])
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 42)
        self.assertEqual(
            [row[0].text for row in self.bot.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard],
            ["C"],
        )

        await self.send("/clear_view")
        final = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual(final["message_id"], 42)
        self.assertIn("Состояние: A, B.", final["text"])
        self.assertEqual(final["reply_markup"].inline_keyboard, [])
        self.assertEqual(self.bot.edit_message_text.await_count, 3)
        self.assertEqual(self.bot.send_message.await_count, sent + 1)
        self.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_failed_edit_requests_reopen_without_repeating_choice(self) -> None:
        """Сетевая ошибка снимает подтверждение цели и оставляет ручную подсказку."""
        first, *_ = await self.open_view()
        self.bot.edit_message_text.side_effect = RuntimeError("test edit rejection")
        with self.assertLogs(self.logger, level="ERROR"):
            await self.press(first)
        self.assertEqual(self.capability.choices, ["A"])
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)
        await self.send("/adjust_view")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)
        await self.press(first)
        self.assertEqual(self.capability.choices, ["A"])
        self.bot.edit_message_text.assert_awaited_once()

    async def test_cancelled_edit_discards_pending_buttons_and_releases_delivery(self) -> None:
        """Прерванный Telegram-запрос не оставляет зарезервированное сообщение."""
        await self.open_view()
        entered = asyncio.Event()

        async def edit(**kwargs):
            entered.set()
            await asyncio.Future()

        self.bot.edit_message_text.side_effect = edit
        edit_task = asyncio.create_task(self.send("/adjust_view"))
        await asyncio.wait_for(entered.wait(), timeout=3)
        edit_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await edit_task
        self.assertEqual(self.bot.send_message.await_count, 1)
        await self.send("/adjust_view")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)
        self.bot.edit_message_text.assert_awaited_once()

    async def test_latest_edit_wins_and_outdated_result_does_not_reach_api(self) -> None:
        """Поздно доставляемый результат даёт подсказку без второго edit."""
        await self.open_view()
        first = self.message("/adjust_view")
        older = self.processor(IncomingEvent("telegram", "1001", str(first.message_id), first.text, "1001"))
        await self.send("/clear_view")
        await safe_send_telegram_reply(first, older, logger=self.logger)
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)

    async def test_delivery_requires_correct_message_ids_from_telegram(self) -> None:
        """Ноль в send и чужой ID в edit оставляют представление без подтверждённой цели."""
        send = self.bot.send_message.side_effect
        self.bot.send_message.side_effect = lambda **_: SimpleNamespace(message_id=0)
        await self.open_view()
        await self.send("/adjust_view")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)
        self.bot.edit_message_text.assert_not_awaited()

        self.bot.send_message.side_effect = send
        await self.open_view()
        self.bot.edit_message_text.side_effect = lambda **_: SimpleNamespace(message_id=43)
        await self.send("/adjust_view")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)
        await self.send("/adjust_view")
        self.bot.edit_message_text.assert_awaited_once()

    async def test_ordinary_callback_can_publish_new_managed_message(self) -> None:
        """Обычная кнопка гаснет, а Bot.send_message подтверждает ID нового представления."""
        ordinary = IncomingResponse(text_content("Открыть: /show_view."), suggested_actions=(
            ReplyAction("Открыть", "/show_view"),
        ))
        with patch.object(IncomingMessageDispatcher, "execute", return_value=ordinary):
            await self.send("Сообщение")
        token = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

        await self.press(token)
        self.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=1001, message_id=42, reply_markup=None,
        )
        first = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        await self.press(first, 43)
        self.assertEqual(self.capability.choices, ["A"])
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 43)
        self.assertEqual(self.bot.edit_message_reply_markup.await_count, 1)

    async def test_oversized_edit_sends_only_recovery_and_invalidates_target(self) -> None:
        """После отказа только последняя часть подсказки предлагает справку."""
        await self.open_view()
        message = self.message("/adjust_view")
        response = self.processor(IncomingEvent("telegram", "1001", str(message.message_id), message.text, "1001"))
        # Лимит позволяет подсказку отправить частями, но не позволяет edit.
        await safe_send_telegram_reply(message, response, logger=self.logger, limit=20)
        self.bot.edit_message_text.assert_not_awaited()
        recovery = self.bot.send_message.await_args_list[1:]
        self.assertEqual(len(recovery), 3)
        self.assertTrue(all("reply_markup" not in call.kwargs for call in recovery[:-1]))
        keyboard = recovery[-1].kwargs["reply_markup"]
        self.assertEqual([button.text for row in keyboard.inline_keyboard for button in row], ["Помощь"])
        await self.send("/adjust_view")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)

    async def test_mode_switch_closes_old_view_across_reentry(self) -> None:
        """Смена на другой режим и возврат требуют открыть ответ заново."""
        first, *_ = await self.open_view()
        await self.send("/other")
        await self.press(first)
        self.assertIn("больше недоступна", self.bot.send_message.await_args.kwargs["text"])
        await self.send("/selected")
        await self.send("/adjust_view")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)
        self.assertEqual(self.capability.choices, [])
        self.bot.edit_message_text.assert_not_awaited()

    async def test_restart_preserves_mode_and_requires_new_view(self) -> None:
        """Новый runtime отклоняет старую кнопку и редактирует лишь новую публикацию."""
        old, *_ = await self.open_view()
        self.start_runtime()
        await self.send("/adjust_view")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], RECOVERY)

        current, *_ = await self.open_view()
        await self.press(old)
        self.assertIn("больше недоступна", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self.capability.choices, [])
        self.bot.edit_message_text.assert_not_awaited()
        await self.press(current, 44)
        self.assertEqual(self.capability.choices, ["A"])
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 44)
        self.assertTrue(all(call.kwargs["message_id"] == 42
                            for call in self.bot.edit_message_reply_markup.await_args_list))
