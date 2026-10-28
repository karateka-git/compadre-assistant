"""Сквозной выбор действий через адаптеры и процессный обработчик."""

import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    ConfirmationAction, ConfirmationQueryResult, IncomingResponse, PendingConfirmation, ReplyAction,
)
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.telegram.bot import _register_handlers
from compadre_assistant.presentation.vk.bot import _handle_update
from tests.test_incoming_boundary import FakeCapability
from tests.test_telegram_bot import FakeAiogram
from tests.test_vk_bot import FakeVkClient
from tests.response_assertions import response_text


class ActionAdapterFlowTest(unittest.TestCase):
    """Проверяет путь от ответа до выбора в двух каналах без сетевого API."""

    def setUp(self) -> None:
        directory = TemporaryDirectory(prefix="action-adapters-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            identity = create_user_identity_service(connection)
            identity.register_with_name(IncomingIdentity("telegram", "1001", "1001"), display_name="Первый")
            identity.register_with_name(IncomingIdentity("vk", "2002", "2002"), display_name="Второй")
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(FakeCapability(),))

    def name(self, channel: str, chat_id: str) -> str:
        """Читает сохранённое имя пользователя из SQLite."""
        with connect_database(self.path) as connection:
            return create_user_identity_service(connection).find_known_private_chat(channel, chat_id)[0].display_name

    def test_telegram_button_uses_real_processor_and_rejects_repeat_and_access_change(self) -> None:
        """Telegram callback выполняет команду, а устаревший token не действует."""
        class Bot:
            """Сохраняет отправку и очистку Telegram без сети."""

            def __init__(self) -> None:
                self.sent = []
                self.edits = []

            async def send_message(self, **kwargs):
                """Сохраняет ответ callback."""
                self.sent.append(kwargs)

            async def edit_message_reply_markup(self, **kwargs):
                """Сохраняет очистку inline-клавиатуры."""
                self.edits.append(kwargs)

        class Callback:
            """Передаёт фактические данные Telegram callback."""

            def __init__(self, token: str, bot: Bot) -> None:
                self.bot = bot
                self.data = token
                self.id = "click"
                self.from_user = SimpleNamespace(id=1001)
                self.message = SimpleNamespace(chat=SimpleNamespace(id=1001), message_id=7)
                self.acked = False

            async def answer(self) -> None:
                """Отмечает подтверждение callback."""
                self.acked = True

        response = IncomingResponse(text_content("Выбери имя"), suggested_actions=(
            ReplyAction("Сменить", "/name Новое"),
        ))
        event = IncomingEvent("telegram", "1001", "1", "запрос", "1001")
        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            issued = self.processor(event)
        token = issued.bound_actions[0].token
        bot = Bot()
        aiogram = FakeAiogram()
        _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=self.processor,
                           logger=logging.getLogger("test.action.telegram"))
        async def choose() -> None:
            for _ in range(2):
                callback = Callback(token, bot)
                await aiogram.dispatcher.included_router.callback_handler(callback)
                self.assertTrue(callback.acked)

        asyncio.run(choose())
        self.assertEqual(self.name("telegram", "1001"), "Новое")
        self.assertIn("больше недоступна", bot.sent[-1]["text"])
        self.assertEqual(len(bot.edits), 2)

        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            old = self.processor(event).bound_actions[0].token
        self.processor(IncomingEvent("telegram", "1001", "2", "/disable selected", "1001"))
        asyncio.run(aiogram.dispatcher.included_router.callback_handler(Callback(old, bot)))
        self.assertEqual(self.name("telegram", "1001"), "Новое")
        self.assertIn("больше недоступна", bot.sent[-1]["text"])

    def test_vk_callback_and_text_only_path_use_exact_commands(self) -> None:
        """VK payload передаёт token; ручной путь исполняет тот же текст."""
        client = FakeVkClient()
        response = IncomingResponse(text_content("Введи /name Кнопка"), suggested_actions=(
            ReplyAction("Сменить", "/name Кнопка"),
        ))
        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            _handle_update({"type": "message_new", "object": {"message": {
                "peer_id": 2002, "from_id": 2002, "id": 1, "text": "запрос",
            }}}, incoming_message_processor=self.processor, client=client,
                           logger=logging.getLogger("test.action.vk"))
        keyboard = json.loads(client.keyboards[-1])
        token = keyboard["buttons"][0][0]["action"]["payload"]
        self.assertNotIn("/name", token)
        event = {"type": "message_event", "object": {
            "peer_id": 2002, "user_id": 2002, "event_id": "first",
            "conversation_message_id": 1, "payload": token,
        }}
        _handle_update(event, incoming_message_processor=self.processor, client=client,
                       logger=logging.getLogger("test.action.vk"))
        self.assertEqual(self.name("vk", "2002"), "Кнопка")
        _handle_update(event, incoming_message_processor=self.processor, client=client,
                       logger=logging.getLogger("test.action.vk"))
        self.assertIn("больше недоступна", client.messages[-1][1])

        # Каналу без API кнопок достаточно точной команды из текста ответа.
        self.assertIn("/name Кнопка", response_text(response))
        text_only = IncomingEvent("vk", "2002", "2", "/name Вручную", "2002")
        self.assertIn("Вручную", response_text(self.processor(text_only)))
        self.assertEqual(self.name("vk", "2002"), "Вручную")

    def test_multiple_confirmations_keep_independent_addressed_paths(self) -> None:
        """Отдельные ответы VK выбирают точные ID; sibling первого устаревает."""
        event = IncomingEvent("vk", "2002", "3", "список", "2002")
        actions = tuple(ConfirmationAction(
            f"core:identity:{number}:1:yes", f"Выбрать {number}",
            f"/confirm core identity {number} 1 yes",
        ) for number in (1, 2))
        pending = tuple(PendingConfirmation("core", "identity", number, 1,
                                            event.reply_address, (action,))
                        for number, action in zip((1, 2), actions))
        response = IncomingResponse(text_content("Два ожидания"), actions=actions)
        second_response = IncomingResponse(text_content("Второе ожидание"), actions=(actions[1],))
        client = FakeVkClient()
        with patch.object(IncomingMessageDispatcher, "_action_confirmations",
                          return_value=ConfirmationQueryResult("complete", pending)):
            with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
                issued = self.processor(event)
            with patch.object(IncomingMessageDispatcher, "execute", return_value=second_response):
                other = self.processor(event)
            self.assertEqual(len(issued.bound_actions), 2)
            self.assertEqual(len(other.bound_actions), 1)
            with patch.object(IncomingMessageDispatcher, "execute",
                              return_value=IncomingResponse(text_content("Принято"))) as execute:
                for index, token in enumerate((issued.bound_actions[0].token,
                                                issued.bound_actions[1].token,
                                                other.bound_actions[0].token), start=1):
                    _handle_update({"type": "message_event", "object": {
                        "peer_id": 2002, "user_id": 2002, "event_id": f"choice-{index}",
                        "payload": json.dumps({"token": token}),
                    }}, incoming_message_processor=self.processor, client=client,
                                   logger=logging.getLogger("test.action.vk.confirm"))
                self.assertEqual([text for _, text in client.messages], [
                    "Принято", "Эта кнопка больше недоступна. Отправь /help или вызови нужную команду из текста ответа.",
                    "Принято",
                ])
                self.assertEqual([call.args[0].text for call in execute.call_args_list],
                                 [actions[0].command, actions[1].command])


if __name__ == "__main__":
    unittest.main()
