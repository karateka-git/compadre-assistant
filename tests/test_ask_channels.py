"""Проверяет режим ИИ через настоящие входы Telegram и VK без сети."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import logging
from html import escape
import json
import re
import sqlite3
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aiogram
import httpx
from openai import OpenAI

from compadre_assistant.bootstrap import create_channel_message_processor
from compadre_assistant.capabilities.ask.data.configuration import AskConfig
from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelTimeoutError, AskModelUnavailableError, InvalidAskAnswerError,
)
from compadre_assistant.capabilities.obsidian.data.configuration.config import ObsidianConfig
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
from compadre_assistant.core.presentation.incoming.events import ImageAttachment, IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.presentation.telegram.bot import _create_telegram_menu_update_handler, _register_handlers
from compadre_assistant.presentation.vk.bot import (
    VkBotError, _VkUpdateWorkers, _create_vk_menu_update_handler, _handle_update,
)
from compadre_assistant.presentation.cli.main import run_telegram_bot_command, run_vk_bot_command
from tests.response_assertions import response_text
from tests.test_telegram_bot import FakeAiogram, MenuTelegramBot, TelegramMessage
from tests.test_vk_bot import FakeVkClient
from tests.test_process_incoming_message import FakeArticleUrlUseCase, FakeConfirmationService, _proposal


class AskChannelsTest(unittest.TestCase):
    """Проверяет маршрутизацию, регистрацию и сохранение состояния обоих каналов."""

    def setUp(self) -> None:
        """Создаёт отдельную временную БД и независимый профиль Ask."""
        directory = TemporaryDirectory(prefix="ask-channels-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
        self.config = AskConfig(base_url="https://example.com/v1", api_key="test", model="ask", max_question_chars=100)
        self.logger = logging.getLogger("ask-channel-test")
        self.logger.disabled = True
        self.addCleanup(setattr, self.logger, "disabled", False)

    def send(self, channel, processor, text, chat_id=42, on_delivery=None, *, images=(), load=None):
        """Проводит сообщение через канал и возвращает доставленный текст."""
        receipt = "Фото получено. Распознаю текст. Для отмены введи /cancel."
        progress = ("Вопрос принят. Готовлю ответ… Для отмены введи /cancel.", receipt)
        finished = threading.Event()

        def process(event, completion):
            def complete(reply):
                completion(reply)
                if channel == "vk":
                    finished.set()

            return processor(event, complete)

        if channel == "telegram":
            aiogram = FakeAiogram()
            _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=process, logger=self.logger)
            message = TelegramMessage()
            original_answer = message.answer
            async def answer(text, *, parse_mode, reply_markup=None):
                await original_answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
                if on_delivery:
                    on_delivery(text)
            message.answer = answer
            async def send_message(*, chat_id, text, parse_mode, reply_markup=None):
                await answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
                finished.set()
            message.send_message = send_message
            message.chat = SimpleNamespace(id=chat_id)
            message.from_user = SimpleNamespace(id=7, username=None, full_name="Тест")
            message.message_id = 1
            message.text = text
            if images:
                message.text = None
                message.caption = text
                message.photo = [SimpleNamespace(file_id=images[0], file_size=None, width=30, height=30)]

                async def download(file_id, *, destination, seek):
                    destination.write(await asyncio.to_thread(load, file_id))

                message.download = download
            async def handle():
                await aiogram.dispatcher.included_router.handler(message)
                if message.answers and message.answers[0] in progress:
                    self.assertTrue(await asyncio.to_thread(finished.wait, 5))

            asyncio.run(handle())
            self.delivered = message.answers
            self.keyboard = message.reply_markups[-1]
            return "\n".join(text for text in message.answers if text not in progress)
        client = FakeVkClient()
        original_send = client.send_message
        def send_message(**kwargs):
            result = original_send(**kwargs)
            if on_delivery:
                on_delivery(kwargs["text"])
            return result
        client.send_message = send_message
        attachments = [{"type": "photo", "photo": {"sizes": [{
            "url": f"https://sun1.userapi.com/{file_id}", "width": 30, "height": 30,
        }]}} for file_id in images]

        def handle_update():
            _handle_update({"type": "message_new", "object": {"message": {
                "peer_id": chat_id, "from_id": 7, "id": 1, "text": text, "attachments": attachments,
            }}}, incoming_message_processor=process, client=client, logger=self.logger)

        if images:
            async def download(url):
                return load(url.rsplit("/", 1)[-1])

            with patch("compadre_assistant.presentation.vk.photos._download", side_effect=download):
                handle_update()
                if client.messages[0][1] in progress:
                    self.assertTrue(finished.wait(5))
        else:
            handle_update()
            if client.messages[0][1] in progress:
                self.assertTrue(finished.wait(5))
        self.delivered = [item[1] for item in client.messages]
        self.keyboard = client.keyboards[-1]
        return "\n".join(text for text in self.delivered if text not in progress)

    def test_vk_mode_entry_and_exit_send_changed_menu_with_reply_and_keep_inline_actions(self) -> None:
        """Переходы доставляют нижнее меню с основным ответом и сохраняют callback."""
        client = FakeVkClient()
        publish = _create_vk_menu_update_handler(client, logger=self.logger)
        with create_channel_message_processor(
            database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config,
        ) as processor:
            def send(text: str) -> None:
                _handle_update({"type": "message_new", "object": {"message": {
                    "peer_id": 7, "from_id": 7, "id": 1, "text": text,
                }}}, incoming_message_processor=processor, client=client,
                    logger=self.logger, menu_update_handler=publish)

            send("/register")
            send("Тест")
            for command, text, expected_commands, expected_labels in (
                ("/ai", "Режим ИИ включён", {"/ask", "/get_text_from_image", "/exit"},
                 ["Задать вопрос", "Извлечь текст с фото"]),
                ("/exit", "Режим этого чата завершён", {"/help", "/ai"},
                 ["Посмотреть команды"]),
            ):
                with self.subTest(command=command):
                    start = len(client.messages)
                    send(command)
                    main_reply, hint = [item[1] for item in client.messages[start:]]
                    self.assertIn(text, main_reply)
                    self.assertEqual(hint, "Что сделать дальше?")
                    menu, inline = map(json.loads, client.keyboards[start:])
                    self.assertFalse(menu["inline"])
                    self.assertTrue(inline["inline"])
                    commands = {button["action"]["label"] for row in menu["buttons"] for button in row}
                    actions = [button["action"] for row in inline["buttons"] for button in row]
                    self.assertLessEqual(expected_commands, commands)
                    self.assertEqual([action["label"] for action in actions], expected_labels)

            _handle_update({"type": "message_event", "object": {
                "peer_id": 7, "user_id": 7, "event_id": "help-button",
                "conversation_message_id": 42, "payload": actions[0]["payload"],
            }}, incoming_message_processor=processor, client=client,
                logger=self.logger, menu_update_handler=publish)
            self.assertIn("/capabilities", client.messages[-1][1])
            self.assertEqual(client.cleared, [(7, 42)])

    def test_vk_keeps_menu_until_a_reply_in_the_same_chat(self) -> None:
        """Запуск и смена доступа из Telegram не публикуют меню в VK."""
        client = FakeVkClient()
        publish = _create_vk_menu_update_handler(client, logger=self.logger)
        def send(processor, text: str) -> None:
            _handle_update({"type": "message_new", "object": {"message": {
                "peer_id": 7, "from_id": 7, "id": 1, "text": text,
            }}}, incoming_message_processor=processor, client=client,
                logger=self.logger, menu_update_handler=publish)

        def commands() -> set[str]:
            menu = next(json.loads(item) for item in reversed(client.keyboards)
                        if item and not json.loads(item)["inline"])
            return {button["action"]["label"] for row in menu["buttons"] for button in row}

        with create_channel_message_processor(
            database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config,
        ) as processor:
            self.send("telegram", processor, "/register", chat_id=7)
            self.send("telegram", processor, "Тест", chat_id=7)
            link_reply = self.send("telegram", processor, "/link_code", chat_id=7)
            code = re.search(r"/link ([A-Za-z0-9_-]+)", link_reply)
            self.assertIsNotNone(code)
            send(processor, code.group(0))

        # Новый процесс не знает, что отображается в клиенте VK.
        before = len(client.messages)
        with create_channel_message_processor(
            database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config,
        ) as restarted:
            restarted.reconcile_menus(publish, notify_expiry=publish.send_expiry)
            restarted.reconcile_menus(publish, notify_expiry=publish.send_expiry)
            self.assertEqual(len(client.messages), before)
            self.assertFalse(restarted.menu_delivery_failures)

            send(restarted, "/ai")
            self.assertIn("/ask", commands())
            before = len(client.messages)
            disabled = self.send("telegram", restarted, "/disable ask", chat_id=7)
            self.assertIn("выключена", disabled)
            self.assertEqual(len(client.messages), before)
            restarted.reconcile_menus(publish, notify_expiry=publish.send_expiry)
            self.assertEqual(len(client.messages), before)
            self.assertIn("/ask", commands())

            send(restarted, "/ask")
            reply = "\n".join(text for _, text in client.messages[before:])
            self.assertIn("лично выключена", reply)
            self.assertIn("/enable ask", reply)
            self.assertNotIn("/ask", commands())

    def test_vk_pending_input_expires_once_and_restores_menu(self) -> None:
        """Сверка доставляет уведомление с меню и повторяет отправку со стабильным ID."""
        client = FakeVkClient()
        publish = _create_vk_menu_update_handler(client, logger=self.logger)
        attempts = []
        drop_next = True
        original_send = client.send_message

        def send_with_loss(**kwargs):
            nonlocal drop_next
            if kwargs["text"].startswith("Срок ожидания ввода истёк"):
                attempts.append(kwargs["random_id"])
                if drop_next:
                    drop_next = False
                    raise VkBotError("send failed")
            original_send(**kwargs)

        client.send_message = send_with_loss
        now = datetime(2026, 11, 14, tzinfo=UTC)
        expired = "Срок ожидания ввода истёк. Повтори {}, чтобы начать заново."
        address = ReplyAddress("vk", "7")
        with create_channel_message_processor(
            database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config,
        ) as processor:
            processor._pending_inputs = PendingInputStore(lambda: now)

            def send(text: str) -> None:
                _handle_update({"type": "message_new", "object": {"message": {
                    "peer_id": 7, "from_id": 7, "id": 1, "text": text,
                }}}, incoming_message_processor=processor, client=client,
                    logger=self.logger, menu_update_handler=publish)

            def reconcile() -> None:
                processor.reconcile_menus(publish, notify_expiry=publish.send_expiry)

            send("/register")
            now += timedelta(minutes=10)
            with self.assertLogs("compadre_assistant.bootstrap", level="ERROR"):
                reconcile()
            self.assertIsNotNone(processor._pending_inputs.get(address))
            reconcile()
            self.assertEqual(client.messages[-1], (7, expired.format("/register")))
            self.assertEqual(attempts[0], attempts[1])
            self.assertIsNone(processor._pending_inputs.get(address))
            send("/register")
            send("Тест")
            send("/ai")
            self.send("telegram", processor, "/register", chat_id=7)
            now += timedelta(minutes=10)
            reconcile()
            self.assertIsNotNone(processor._pending_inputs.get(ReplyAddress("telegram", "7")))
            self.assertEqual(len(attempts), 2)

            send("/ask")
            now += timedelta(minutes=10)
            drop_next = True
            with self.assertLogs("compadre_assistant.bootstrap", level="ERROR"):
                reconcile()
            old = processor._pending_inputs.get(address)
            send("/ask")
            new = processor._pending_inputs.get(address)
            self.assertNotEqual(old.request_id, new.request_id)
            reconcile()
            self.assertEqual(len(attempts), 3)
            now += timedelta(minutes=10)
            reconcile()
            self.assertNotEqual(attempts[-1], attempts[-2])

            for command in ("/ask", "/get_text_from_image"):
                with self.subTest(command=command):
                    send(command)
                    start = len(client.messages)
                    now += timedelta(minutes=9, seconds=59)
                    reconcile()
                    self.assertEqual(len(client.messages), start)
                    now += timedelta(seconds=1)
                    reconcile()
                    reconcile()
                    self.assertEqual(client.messages[start:], [(7, expired.format(command))])
                    keyboard = json.loads(client.keyboards[-1])
                    self.assertFalse(keyboard["inline"])
                    self.assertEqual(
                        {button["action"]["label"] for row in keyboard["buttons"] for button in row},
                        {"/ask", "/get_text_from_image", "/exit"},
                    )

    def test_vk_pending_expiry_yields_to_incoming_or_changed_profile(self) -> None:
        """Старое ожидание не уведомляет после отмены, замены или смены владельца."""
        client = FakeVkClient()
        publish = _create_vk_menu_update_handler(client, logger=self.logger)
        now = datetime(2026, 11, 14, tzinfo=UTC)
        with create_channel_message_processor(
            database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config,
        ) as processor:
            processor._pending_inputs = PendingInputStore(lambda: now)

            def send(text: str) -> None:
                _handle_update({"type": "message_new", "object": {"message": {
                    "peer_id": 7, "from_id": 7, "id": 1, "text": text,
                }}}, incoming_message_processor=processor, client=client,
                    logger=self.logger, menu_update_handler=publish)

            def reconcile() -> None:
                processor.reconcile_menus(publish, notify_expiry=publish.send_expiry)

            send("/register")
            send("Тест")
            send("/ai")
            expiry_text = "Срок ожидания ввода истёк"
            for incoming in ("/cancel", "/get_text_from_image", "Опоздавший вопрос"):
                with self.subTest(incoming=incoming):
                    send("/ask")
                    now += timedelta(minutes=10)
                    start = len(client.messages)
                    send(incoming)
                    reconcile()
                    notices = [text for _, text in client.messages[start:] if text.startswith(expiry_text)]
                    self.assertEqual(len(notices), int(incoming == "Опоздавший вопрос"))
                    if incoming == "/get_text_from_image":
                        self.assertEqual(
                            processor._pending_inputs.get(ReplyAddress("vk", "7")).retry_command, incoming,
                        )

            send("/ask")
            now += timedelta(minutes=10)
            start = len(client.messages)
            with connect_database(self.path) as connection:
                connection.execute(
                    "DELETE FROM external_identities WHERE channel = 'vk' AND external_user_id = '7'"
                )
                connection.commit()
            reconcile()
            self.assertEqual(client.messages[start:], [])

    def test_registered_questions_require_ai_mode_and_remain_independent(self) -> None:
        """Оба канала требуют регистрацию и явный вход в режим ИИ."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel), patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
                model = model_type.return_value
                model.answer.side_effect = lambda **kwargs: "Ответ: " + kwargs["question"]
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config) as processor:
                    self.send(channel, processor, "/ask до регистрации")
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "/ask до имени")
                    model.answer.assert_not_called()
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "Тест")
                    denied = self.send(channel, processor, "/ask до входа")
                    self.assertIn("войди в режим через", denied)
                    self.assertIn("/ai", denied)
                    self.assertIn("/ai без аргументов", self.send(channel, processor, "/ai /ask вопрос"))
                    model.answer.assert_not_called()
                    self.assertIn("Режим ИИ включён", self.send(channel, processor, "/ai"))
                    for question in ("первый", "https://example.com/article"):
                        self.assertEqual(self.send(channel, processor, "/ask " + question), "Ответ: " + question)
                    self.assertEqual([call.kwargs["question"] for call in model.answer.call_args_list], ["первый", "https://example.com/article"])
                    for text in ("обычный вопрос", "/unknown вопрос", "/asker вопрос"):
                        self.send(channel, processor, text)
                    self.assertEqual(model.answer.call_count, 2)
                    self.assertIn("Введи один вопрос", self.send(channel, processor, "/ask"))
                    self.assertIn("100", self.send(channel, processor, "/ask " + "я" * 101))
                    self.assertEqual(model.answer.call_count, 2)
                    with connect_database(self.path) as connection:
                        self.assertEqual(connection.execute(
                            "SELECT capability_id FROM active_chat_modes WHERE channel = ?", (channel,)
                        ).fetchone()[0], "ask")
                    self.send(channel, processor, "/exit")
                    denied = self.send(channel, processor, "/ask после выхода")
                    self.assertIn("войди в режим через", denied)
                    self.assertIn("/ai", denied)
                    self.assertEqual(model.answer.call_count, 2)
                model.close.assert_called_once_with()

    def test_pending_question_accepts_retry_from_both_channels(self) -> None:
        """Пробельный текст достигает владельца ввода, а пустой вне ввода не запускает Ask."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel), patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
                model = model_type.return_value
                model.answer.return_value = "Ответ"
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config) as processor:
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "Тест")
                    self.assertIn("Пришли текстовое", self.send(channel, processor, "  "))
                    self.send(channel, processor, "/ai")
                    self.send(channel, processor, "/ask")
                    self.assertIn("Вопрос пуст", self.send(channel, processor, "  "))
                    self.assertIn("Сократи вопрос", self.send(channel, processor, "x" * 101))
                    model.answer.assert_not_called()
                    self.assertEqual(self.send(channel, processor, "вопрос"), "Ответ")
                    self.assertEqual(model.answer.call_count, 1)

    def test_ask_can_be_disabled_and_enabled_without_restart(self) -> None:
        """Оба канала показывают публичное название без автоматического входа."""
        with patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
            model_type.return_value.answer.return_value = "Ответ"
            with create_channel_message_processor(
                database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config
            ) as processor:
                for channel in ("telegram", "vk"):
                    with self.subTest(channel=channel):
                        self.send(channel, processor, "/register")
                        self.send(channel, processor, "Тест")
                        self.assertIn("выключена", self.send(channel, processor, "/disable ask"))
                        self.assertEqual(self.send(channel, processor, "/ai"),
                                         "Возможность «Режим ИИ» лично выключена. "
                                         "Включить: /enable ask. Список: /capabilities.")
                        self.assertEqual(self.send(channel, processor, "/enable ask"),
                                         "Личный доступ к возможности «Режим ИИ» включён. "
                                         "Выбор действует во всех привязанных каналах.")
                        self.assertIn("«Режим ИИ» уже включён", self.send(channel, processor, "/enable ask"))
                        denied = self.send(channel, processor, "/ask вопрос")
                        self.assertIn("войди в режим", denied)
                        self.assertIn("/ai", denied)
                        self.send(channel, processor, "/ai")
                        self.assertEqual(self.send(channel, processor, "/ask вопрос"), "Ответ")
                self.assertEqual(model_type.return_value.answer.call_count, 2)

    def test_inflight_ask_delivers_after_disable_and_keeps_model_open(self) -> None:
        """Принятый Ask отвечает в исходный канал после disable, runtime живёт."""
        for channel in ("telegram", "vk"):
            started = threading.Event()
            release = threading.Event()

            def answer(**kwargs):
                """Ожидает отключения только на первом вопросе."""
                if kwargs["question"] == "долгий":
                    started.set()
                    if not release.wait(5):
                        raise TimeoutError("Тест не освободил модель")
                return "Ответ: " + kwargs["question"]

            with self.subTest(channel=channel), patch(
                "compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel"
            ) as model_type:
                model = model_type.return_value
                model.answer.side_effect = answer
                with create_channel_message_processor(
                    database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config
                ) as processor:
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "Тест")
                    self.send(channel, processor, "/ai")
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(processor, IncomingEvent(channel, "42", "direct", "/ask долгий", "7"))
                        try:
                            self.assertTrue(started.wait(5))
                            self.assertIn("выключена", self.send(channel, processor, "/disable ask"))
                            model.close.assert_not_called()
                        finally:
                            release.set()
                        self.assertEqual(response_text(future.result(timeout=5)), "Ответ: долгий")
                    self.assertIn("/enable ask", self.send(channel, processor, "/ask следующий"))
                    self.assertEqual(model.answer.call_count, 1)
                    self.assertIn("«Режим ИИ» включён", self.send(channel, processor, "/enable ask"))
                    self.assertEqual(self.send(channel, processor, "/ask другой"), "Ответ: другой")
                model.close.assert_called_once_with()

    def test_pending_obsidian_and_each_chat_mode_survive_answers_and_errors(self) -> None:
        """Смена режима сохраняет подтверждения Obsidian и изолирует другие чаты."""
        for channel in ("telegram", "vk"):
            proposal = replace(_proposal(), origin=PendingOrigin(channel, "42"))
            confirmation = FakeConfirmationService(proposal)
            article = FakeArticleUrlUseCase()
            obsidian = ObsidianMessageHandler(article_url_use_case=article, obsidian_confirmation_service=confirmation)
            with self.subTest(channel=channel), patch("compadre_assistant.bootstrap.ObsidianCapability", return_value=obsidian), patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
                model = model_type.return_value
                model.answer.return_value = "/confirm obsidian review 1 1 yes"
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("obsidian", "ask"), obsidian_config=ObsidianConfig(), ask_config=self.config) as processor:
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "Тест")
                    with connect_database(self.path) as connection:
                        owner = connection.execute(
                            "SELECT app_user_id FROM external_identities WHERE channel = ? AND external_user_id = '7'",
                            (channel,),
                        ).fetchone()[0]
                        SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                            UserCapabilityPreference(owner, "obsidian", True)
                        )
                    self.assertIn("да", self.send(channel, processor, "/obsidian"))
                    self.assertIn("/ai", self.send(channel, processor, "/ask вопрос"))
                    model.answer.assert_not_called()
                    self.send(channel, processor, "/ai")
                    with connect_database(self.path) as connection:
                        before = tuple(connection.execute("SELECT * FROM active_chat_modes").fetchall())
                    self.assertEqual(self.send(channel, processor, "/ask https://example.com/article"), model.answer.return_value)
                    for error in (AskModelTimeoutError("timeout"), AskModelUnavailableError("private provider details"), InvalidAskAnswerError("invalid")):
                        model.answer.side_effect = error
                        reply = self.send(channel, processor, "/ask вопрос")
                        self.assertIn("позже", reply)
                        self.assertNotIn(str(error), reply)
                    model.answer.side_effect = None
                    model.answer.return_value = "   "
                    self.assertIn("позже", self.send(channel, processor, "/ask пустой ответ"))
                    self.send(channel, processor, "/ask")
                    self.send(channel, processor, "/ask " + "я" * 101)
                    self.send(channel, processor, "/ask другой чат", chat_id=43)
                    with connect_database(self.path) as connection:
                        after = tuple(connection.execute("SELECT * FROM active_chat_modes").fetchall())
                    self.assertEqual(before, after)
                    self.assertEqual(confirmation.confirm_calls, 0)
                    self.assertEqual(confirmation.cancel_calls, 0)
                    self.assertIs(confirmation.proposal, proposal)
                    self.assertEqual(article.commands, [])

    def test_disabled_ask_does_not_load_settings_or_create_model(self) -> None:
        """Отключённая команда доступна только как метаданные без внешнего клиента."""
        with patch("compadre_assistant.capabilities.ask.bootstrap.load_ask_config") as loader, patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model:
            with create_channel_message_processor(database_path=self.path, enabled_capabilities=()) as processor:
                for channel in ("telegram", "vk"):
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "Тест")
                    self.assertEqual(self.send(channel, processor, "/ai"),
                                     "Возможность «Режим ИИ» недоступна на сервере. Список: /capabilities.")
                    self.assertIn("недоступна на сервере", self.send(channel, processor, "/ask вопрос"))
            loader.assert_not_called()
            model.assert_not_called()

    def test_cli_channel_commands_register_ask_and_close_its_client(self) -> None:
        """CLI обоих каналов собирает Ask из профиля без Obsidian/GitHub/vault."""
        for channel, command, extra in (
            ("telegram", run_telegram_bot_command, {}),
            ("vk", run_vk_bot_command, {"group_id": 123}),
        ):
            def polling(*, incoming_message_processor, **_kwargs):
                """Проверяет вход из CLI вместо реального сетевого polling."""
                self.send(channel, incoming_message_processor, "/register")
                self.send(channel, incoming_message_processor, "Тест")
                self.send(channel, incoming_message_processor, "/ai")
                self.assertEqual(self.send(channel, incoming_message_processor, "/ask вопрос"), "Ответ")

            with self.subTest(channel=channel), patch("compadre_assistant.bootstrap.load_ask_config", return_value=self.config), patch("compadre_assistant.capabilities.ask.bootstrap.load_ask_config", return_value=self.config) as loader, patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, patch(f"compadre_assistant.presentation.cli.main.run_{channel}_bot", side_effect=polling):
                model_type.return_value.answer.return_value = "Ответ"
                self.assertEqual(command(database_path=self.path, token="123456:ABCDEF_test_token_123456789", logger=self.logger, enabled_capabilities=("ask",), **extra), 0)
                loader.assert_called_once_with()
                model_type.return_value.close.assert_called_once_with()

    def press_ocr(self, channel, processor, keyboard, chat_id=7):
        """Передаёт нажатие кнопки оригинальному callback handler канала."""
        if channel == "telegram":
            aiogram = FakeAiogram()
            _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)
            button = next(button for row in keyboard.inline_keyboard for button in row
                          if button.text == "Извлечь текст с фото")
            message = TelegramMessage()
            async def acknowledge():
                pass
            async def edit(**_kwargs):
                pass
            message.edit_message_reply_markup = edit
            callback = SimpleNamespace(
                answer=acknowledge, id="ocr-callback", data=button.callback_data,
                from_user=SimpleNamespace(id=7), bot=message,
                message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=1, photo=[object()]),
            )
            asyncio.run(aiogram.dispatcher.included_router.callback_handler(callback))
            return message.answers[-1]

        client = FakeVkClient()
        button = next(button["action"] for row in json.loads(keyboard)["buttons"] for button in row
                      if button["action"]["label"] == "Извлечь текст с фото")
        _handle_update({"type": "message_event", "object": {
            "peer_id": chat_id, "user_id": 7, "event_id": "ocr-callback", "conversation_message_id": 1,
            "payload": button["payload"], "attachments": [{"type": "photo", "photo": {}}],
        }}, incoming_message_processor=processor, client=client, logger=self.logger)
        return client.messages[-1][1]

    def test_ocr_modes_callbacks_pending_input_and_real_shared_usage(self) -> None:
        """Проводит ожидание и OCR через каналы, SDK transport и общую SQLite."""
        command = "/get_text_from_image"
        image = b"\xff\xd8\xffsynthetic-photo"
        timestamp = datetime.now(UTC)
        requests = []
        downloads = []
        transcription = "Строка <b>\nКод 8415"
        responses = iter((f"TEXT\n{transcription}", "NO_TEXT", "", "Ответ на вопрос") * 2)

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "model": "fallback", "choices": [{
                    "message": {"content": next(responses)}, "finish_reason": "stop",
                }], "usage": {"prompt_tokens": 19, "completion_tokens": 2},
            })

        def load(file_id):
            downloads.append(file_id)
            if file_id == "broken":
                raise RuntimeError("provider filename must remain private")
            return image

        profile = replace(self.config, usage_provider="test-gateway")
        with httpx.Client(transport=httpx.MockTransport(handle)) as transport, \
                OpenAI(base_url=profile.base_url, api_key=profile.api_key, max_retries=0, http_client=transport) as client, \
                patch("compadre_assistant.capabilities.ask.data.llm.openai_ask_model.OpenAI", return_value=client) as sdk, \
                patch("compadre_assistant.bootstrap.PendingInputStore", return_value=PendingInputStore(clock=lambda: timestamp)), \
                create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                 ask_config=profile) as processor:
            for channel in ("telegram", "vk"):
                with self.subTest(channel=channel):
                    def send(text, *, chat_id=7, **kwargs):
                        return self.send(channel, processor, text, chat_id=chat_id, load=load, **kwargs)

                    def photo(caption="", *, file_id="photo", **kwargs):
                        return send(caption, images=(file_id,), **kwargs)

                    initial_downloads, initial_requests = len(downloads), len(requests)
                    send("/register")
                    send("Тест")
                    self.assertIn("войди в режим", send(command))
                    self.assertNotIn(command, photo("/ai"))
                    self.assertIn(command, send("/ai"))
                    self.assertIn(command, send("/help"))
                    self.assertIn(command, photo(command))
                    self.assertIn("Пришли одно фото", self.press_ocr(channel, processor, self.keyboard))
                    self.assertIn("Пришли одно фото", send("обычный текст"))
                    if channel == "vk":
                        self.assertIn("Пришли одно фото", send("/ask", images=("one", "two")))
                    self.assertNotIn(command, photo(chat_id=43))
                    self.assertEqual((len(downloads), len(requests)), (initial_downloads, initial_requests))
                    result = photo("/exit")
                    self.assertEqual(result, escape(transcription, quote=False) if channel == "telegram" else transcription)
                    self.assertIn(command, photo())
                    self.assertIn("Пришли одно фото", send(command))
                    self.assertEqual(photo(), "На фото нет текста.")
                    send(command)
                    send("/cancel")
                    self.assertIn(command, photo())
                    send(command)
                    # Обычное неподходящее значение не продлевает исходный срок.
                    timestamp += timedelta(minutes=9)
                    self.assertIn("Пришли одно фото", send("обычный текст"))
                    timestamp += timedelta(minutes=1)
                    self.assertIn("истёк", photo())
                    send(command)
                    send("/exit")
                    self.assertNotIn(command, photo())
                    send("/ai")
                    send(command)
                    send("/disable ask")
                    self.assertNotIn(command, photo())
                    self.assertIn("выключена", send(command))
                    send("/enable ask")
                    send(command)
                    self.assertIn("Не удалось прочитать фото", photo(file_id="broken"))
                    self.assertIn(command, photo())
                    send(command)
                    self.assertIn("Не удалось распознать", photo())
                    self.assertIn("Введи один вопрос", send("/ask"))
                    self.assertIn("Пришли текстовое", photo("/cancel"))
                    self.assertEqual(send("вопрос"), "Ответ на вопрос")
                    self.assertEqual(len(downloads) - initial_downloads, 4)
                    self.assertEqual(len(requests) - initial_requests, 4)
                    self.assertEqual(requests[initial_requests]["messages"][0]["content"][1]["type"], "image_url")
                    self.assertEqual(requests[initial_requests + 3]["messages"][1]["content"], "вопрос")

        sdk.assert_called_once()
        with connect_database(self.path) as connection:
            observations = connection.execute("SELECT provider, actual_model, tokens_json FROM ask_usage").fetchall()
        self.assertEqual(len(observations), 8)
        for provider, model, tokens in observations:
            self.assertEqual((provider, model), ("test-gateway", "fallback"))
            self.assertEqual(json.loads(tokens)["prompt_tokens"], 19)
            self.assertEqual(json.loads(tokens)["completion_tokens"], 2)

    def test_ocr_competing_photos_claim_one_pending_input(self) -> None:
        """Второе фото получает подсказку, пока первый загрузчик заблокирован."""
        for channel in ("telegram", "vk"):
            loading = threading.Event()
            release = threading.Event()
            downloaded = []

            def load(file_id):
                downloaded.append(file_id)
                loading.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release photo")
                return b"\xff\xd8\xffimage"

            with self.subTest(channel=channel), patch(
                "compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel"
            ) as model_type, create_channel_message_processor(
                database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config,
            ) as processor, ThreadPoolExecutor(max_workers=1) as executor:
                model_type.return_value.recognize_text.return_value = SimpleNamespace(text="Первое фото")
                self.send(channel, processor, "/register")
                self.send(channel, processor, "Тест")
                self.send(channel, processor, "/ai")
                self.send(channel, processor, "/get_text_from_image")
                first = executor.submit(self.send, channel, processor, "", images=("first",), load=load)
                try:
                    self.assertTrue(loading.wait(5))
                    self.assertIn("Обработка уже идёт", self.send(
                        channel, processor, "", images=("second",), load=load,
                    ))
                    self.assertEqual(downloaded, ["first"])
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5), "Первое фото")
                model_type.return_value.recognize_text.assert_called_once()

    def test_questions_can_be_cancelled_in_both_channels_while_model_keeps_running(self) -> None:
        """Обработчик чата принимает отмену, а начатые запросы сохраняют учёт."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                entered, release = threading.Event(), threading.Event()
                completed = threading.Event()
                sent, reports = [], []

                def model(request):
                    reports.append(json.loads(request.content)["messages"][1]["content"])
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("Тест не освободил модель")
                    return httpx.Response(200, json={
                        "model": "test-ask", "choices": [{"message": {"content": "Лишний итог"},
                                                           "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 15, "completion_tokens": 3},
                    })

                with httpx.Client(transport=httpx.MockTransport(model)) as transport, \
                        OpenAI(base_url=self.config.base_url, api_key=self.config.api_key,
                               max_retries=0, http_client=transport) as sdk, \
                        patch("compadre_assistant.capabilities.ask.data.llm.openai_ask_model.OpenAI", return_value=sdk), \
                        create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                         ask_config=self.config) as processor:
                    run_action = processor._deferred_actions._run

                    def observe_completion(*args):
                        try:
                            run_action(*args)
                        finally:
                            completed.set()

                    async def exercise(send, commands):
                        await send("/register")
                        await send("Тест")
                        await send("/ai")
                        normal = commands()
                        self.assertIn("/ask", normal)
                        await send("/get_text_from_image")
                        self.assertEqual(commands(), ("/cancel",))
                        await send("/cancel")
                        self.assertEqual(commands(), normal)
                        if channel == "telegram":
                            await send("/ask")
                            self.assertEqual(commands(), ("/cancel",))
                            await send("  ")
                            self.assertEqual(commands(), ("/cancel",))
                            self.assertEqual(reports, [])
                        await send("пошаговый" if channel == "telegram" else "/ask прямой")
                        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                        self.assertIn("Вопрос принят", sent[-1])
                        self.assertEqual(commands(), ("/cancel",))
                        await send("/cancel")
                        self.assertIn("Обработка отменена", sent[-1])
                        self.assertEqual(commands(), normal)
                        release.set()
                        self.assertTrue(await asyncio.to_thread(completed.wait, 5))

                    async def telegram():
                        fake = FakeAiogram()
                        bot = MenuTelegramBot()

                        async def send_message(**kwargs):
                            sent.append(kwargs["text"])

                        bot.send_message = send_message
                        publish = _create_telegram_menu_update_handler(
                            bot, loop=asyncio.get_running_loop(), aiogram=aiogram, logger=self.logger,
                        )
                        _register_handlers(fake.dispatcher, fake, incoming_message_processor=processor,
                                           logger=self.logger, menu_update_handler=publish)

                        async def send(value):
                            message = TelegramMessage()
                            message.text = value
                            message.from_user = SimpleNamespace(id=7)
                            message.chat = SimpleNamespace(id=7)
                            message.message_id = 1
                            message.answer = lambda text, **kwargs: send_message(text=text, **kwargs)
                            message.bot = bot
                            await fake.dispatcher.included_router.handler(message)

                        try:
                            await exercise(send, lambda: tuple(f"/{item.command}" for item in bot.menu_calls[-1][0]))
                        finally:
                            release.set()
                            await asyncio.to_thread(processor.close_deferred_actions)

                    async def vk():
                        client = FakeVkClient()
                        original_send = client.send_message

                        def record(**kwargs):
                            original_send(**kwargs)
                            sent.append(kwargs["text"])

                        client.send_message = record
                        publish = _create_vk_menu_update_handler(client, logger=self.logger)
                        handled = threading.Event()

                        def handle(update):
                            try:
                                _handle_update(update, incoming_message_processor=processor, client=client,
                                               logger=self.logger, menu_update_handler=publish)
                            finally:
                                handled.set()

                        workers = _VkUpdateWorkers(handler=handle, logger=self.logger, worker_count=1, max_queue_size=8)

                        async def send(value):
                            handled.clear()
                            workers.submit("7", {"type": "message_new", "object": {"message": {
                                "peer_id": 7, "from_id": 7, "id": 1, "text": value,
                            }}})
                            self.assertTrue(await asyncio.to_thread(handled.wait, 5))

                        def commands():
                            keyboard = next(json.loads(item) for item in reversed(client.keyboards)
                                            if item and not json.loads(item)["inline"])
                            return tuple(button["action"]["label"] for row in keyboard["buttons"] for button in row)

                        try:
                            await exercise(send, commands)
                        finally:
                            release.set()
                            workers.close()
                            processor.close_deferred_actions()

                    with patch.object(processor._deferred_actions, "_run", side_effect=observe_completion):
                        asyncio.run(telegram() if channel == "telegram" else vk())
                    self.assertNotIn("Лишний итог", sent)
                    self.assertEqual(reports, ["пошаговый" if channel == "telegram" else "прямой"])

        with connect_database(self.path) as connection:
            rows = connection.execute("SELECT actual_model, tokens_json FROM ask_usage").fetchall()
        self.assertEqual([(name, json.loads(tokens)["prompt_tokens"]) for name, tokens in rows], [("test-ask", 15)] * 2)

    def test_ocr_cancels_during_download_or_model_and_drains_usage(self) -> None:
        """VK очередь и Telegram handler принимают отмену при заблокированной сети."""
        receipt = "Фото получено. Распознаю текст. Для отмены введи /cancel."
        marker = "Распознанный ответ"
        command = "/get_text_from_image"
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                stages = []
                entered_download, entered_model = threading.Event(), threading.Event()
                release_download, release_model = threading.Event(), threading.Event()
                first_finished = threading.Event()
                restored = threading.Event()
                result_delivered = threading.Event()
                messages = []
                menu_calls = []
                keyboards_by_reply = []

                def record_menu(chat, commands):
                    menu_calls.append((chat, commands))
                    if chat == 7 and "/ask" in commands and "/cancel" not in commands:
                        restored.set()

                def download(file_id):
                    stages.append(f"download:{file_id}")
                    if file_id == "first":
                        entered_download.set()
                        if not release_download.wait(5):
                            raise TimeoutError("Тест не освободил загрузчик")
                    if file_id == "broken":
                        return b"invalid image"
                    return b"\xff\xd8\xffsample"

                def model(_request):
                    stages.append("model")
                    entered_model.set()
                    if not release_model.wait(5):
                        raise TimeoutError("Тест не освободил модель")
                    return httpx.Response(200, json={
                        "model": "test-ocr", "choices": [{"message": {"content": f"TEXT\n{marker}"},
                                                          "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 15, "completion_tokens": 3},
                    })

                def record(chat, answer):
                    messages.append((int(chat), answer))
                    if answer == receipt:
                        stages.append("receipt")
                    if marker in answer or "Не удалось прочитать фото" in answer:
                        result_delivered.set()

                with httpx.Client(transport=httpx.MockTransport(model)) as transport, \
                        OpenAI(base_url=self.config.base_url, api_key=self.config.api_key,
                               max_retries=0, http_client=transport) as sdk, \
                        patch("compadre_assistant.capabilities.ask.data.llm.openai_ask_model.OpenAI", return_value=sdk), \
                        create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                         ask_config=self.config) as processor:
                    async def exercise(send, choose, publish):
                        await send("/register")
                        await send("Тест")
                        keyboard = await send("/ai")
                        normal = menu_calls[-1]
                        self.assertIn("/ask", normal[1])
                        await send("/register", chat=43)
                        await send("Другой профиль", chat=43)
                        await send("/ai", chat=43)
                        self.assertEqual(menu_calls[-1], (43, normal[1]))
                        await send(command)
                        message_count = len(messages)
                        await send("", photo="undelivered", fail_receipt=True)
                        self.assertEqual(stages, [])
                        if channel == "telegram":
                            self.assertEqual(menu_calls[-2:], [(7, ("/cancel",)), normal])
                        else:
                            self.assertEqual(len(messages), message_count)
                            self.assertEqual(menu_calls[-1], (7, ("/cancel",)))
                            await send("/help")
                            self.assertEqual(menu_calls[-1], normal)
                            self.assertEqual(keyboards_by_reply[-1], (messages[-1][1], normal[1]))
                        await send(command)
                        self.assertIn("Пришли одно фото", messages[-1][1])
                        await send("/exit", photo="first")
                        self.assertTrue(await asyncio.to_thread(entered_download.wait, 5))
                        self.assertEqual(stages, ["receipt", "download:first"])
                        self.assertEqual(menu_calls[-1], (7, ("/cancel",)))
                        if channel == "vk":
                            self.assertEqual(keyboards_by_reply[-1], (receipt, ("/cancel",)))
                        call_count = len(menu_calls)
                        await asyncio.to_thread(processor.reconcile_menus, publish)
                        for text, photo in (("/exit", None), ("/disable ask", None), ("текст", None),
                                            ("", "ignored"), ("", None)):
                            await send(text, photo=photo)
                            self.assertIn("Обработка уже идёт", messages[-1][1])
                        await choose(keyboard)
                        self.assertIn("Обработка уже идёт", messages[-1][1])
                        self.assertEqual(len(menu_calls), call_count)
                        await send("/ai", chat=43)
                        self.assertIn("Режим ИИ включён", messages[-1][1])
                        await send("/cancel")
                        self.assertIn("Обработка отменена", messages[-1][1])
                        self.assertEqual(menu_calls[-1], normal)
                        if channel == "vk":
                            self.assertEqual(keyboards_by_reply[-1], (messages[-1][1], normal[1]))
                        await send(command)
                        await send("", photo="second")
                        self.assertTrue(await asyncio.to_thread(entered_model.wait, 5))
                        call_count = len(menu_calls)
                        release_download.set()
                        self.assertTrue(await asyncio.to_thread(first_finished.wait, 5))
                        await asyncio.to_thread(processor.reconcile_menus, publish)
                        self.assertEqual(len(menu_calls), call_count)
                        self.assertEqual(menu_calls[-1], (7, ("/cancel",)))
                        self.assertEqual([commands for chat, commands in menu_calls if chat == 43][-1], normal[1])
                        await send("/cancel")
                        self.assertIn("Обработка отменена", messages[-1][1])
                        release_model.set()
                        for photo, expected in (("broken", "Не удалось прочитать фото"), ("fast", marker)):
                            restored.clear()
                            result_delivered.clear()
                            await send(command)
                            self.assertIn("Пришли одно фото", messages[-1][1])
                            await send("", photo=photo)
                            self.assertTrue(await asyncio.to_thread(restored.wait, 5))
                            self.assertTrue(await asyncio.to_thread(result_delivered.wait, 5))
                            self.assertEqual(menu_calls[-2:], [(7, ("/cancel",)), normal])
                            self.assertIn(expected, messages[-1][1])
                            if channel == "vk" and photo == "fast":
                                self.assertEqual(keyboards_by_reply[-1], (messages[-1][1], normal[1]))

                    async def telegram():
                        fake = FakeAiogram()
                        menu_bot = MenuTelegramBot()
                        original_set = menu_bot.set_my_commands

                        async def set_my_commands(*, commands, scope):
                            await original_set(commands=commands, scope=scope)
                            record_menu(scope.chat_id, tuple(f"/{item.command}" for item in commands))

                        menu_bot.set_my_commands = set_my_commands
                        publish = _create_telegram_menu_update_handler(
                            menu_bot, loop=asyncio.get_running_loop(), aiogram=aiogram, logger=self.logger,
                        )
                        _register_handlers(fake.dispatcher, fake, incoming_message_processor=processor,
                                           logger=self.logger, menu_update_handler=publish)
                        router = fake.dispatcher.included_router

                        async def send(text, *, photo=None, chat=7, fail_receipt=False):
                            message = TelegramMessage()
                            message.chat = SimpleNamespace(id=chat)
                            message.message_id = 1
                            message.from_user = SimpleNamespace(id=chat)
                            message.text = text if photo is None else None
                            message.caption = text if photo is not None else None
                            if photo is not None:
                                message.photo = [SimpleNamespace(file_id=photo, width=40, height=40, file_size=None)]

                            async def download_image(file_id, *, destination, seek):
                                destination.write(await asyncio.to_thread(download, file_id))

                            original_answer = message.answer

                            async def answer(text, **kwargs):
                                if fail_receipt and text == receipt:
                                    raise RuntimeError("Канал не принял квитанцию")
                                await original_answer(text, **kwargs)
                                record(chat, text)

                            async def send_message(*, chat_id, text, **_kwargs):
                                record(chat_id, text)

                            message.download = download_image
                            message.answer = answer
                            message.send_message = send_message
                            await router.handler(message)
                            return message.reply_markups[-1] if message.reply_markups else None

                        async def choose(keyboard):
                            button = next(button for row in keyboard.inline_keyboard for button in row
                                          if button.text == "Извлечь текст с фото")
                            edits = []

                            async def acknowledge():
                                pass

                            async def edit(**kwargs):
                                edits.append(kwargs)

                            async def send_message(*, chat_id, text, **_kwargs):
                                record(chat_id, text)

                            await router.callback_handler(SimpleNamespace(
                                answer=acknowledge, id="busy-click", data=button.callback_data,
                                from_user=SimpleNamespace(id=7),
                                bot=SimpleNamespace(edit_message_reply_markup=edit, send_message=send_message),
                                message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=1),
                            ))
                            self.assertEqual(edits, [])

                        try:
                            await exercise(send, choose, publish)
                        finally:
                            release_download.set()
                            release_model.set()
                            await asyncio.to_thread(processor.close_deferred_actions)

                    async def vk():
                        client = FakeVkClient()
                        original_send = client.send_message
                        handled = threading.Event()
                        fail_next_receipt = False

                        def send_message(**kwargs):
                            if fail_next_receipt and kwargs["text"] == receipt:
                                raise RuntimeError("Канал не принял квитанцию")
                            result = original_send(**kwargs)
                            if kwargs["keyboard"]:
                                keyboard = json.loads(kwargs["keyboard"])
                                if not keyboard["inline"]:
                                    commands = tuple(button["action"]["label"]
                                                     for row in keyboard["buttons"] for button in row)
                                    keyboards_by_reply.append((kwargs["text"], commands))
                                    record_menu(kwargs["peer_id"], commands)
                            record(kwargs["peer_id"], kwargs["text"])
                            return result

                        client.send_message = send_message
                        publish = _create_vk_menu_update_handler(client, logger=self.logger)

                        def handle(update):
                            try:
                                _handle_update(update, incoming_message_processor=processor,
                                               client=client, logger=self.logger, menu_update_handler=publish)
                            finally:
                                handled.set()

                        workers = _VkUpdateWorkers(handler=handle, logger=self.logger, worker_count=1, max_queue_size=8)

                        async def submit(chat, update):
                            handled.clear()
                            workers.submit(str(chat), update)
                            self.assertTrue(await asyncio.to_thread(handled.wait, 5))

                        async def send(text, *, photo=None, chat=7, fail_receipt=False):
                            nonlocal fail_next_receipt
                            fail_next_receipt = fail_receipt
                            attachments = [] if photo is None else [{"type": "photo", "photo": {"sizes": [{
                                "url": f"https://sun1.userapi.com/{photo}", "width": 40, "height": 40,
                            }]}}]
                            await submit(chat, {"type": "message_new", "object": {"message": {
                                "id": 1, "peer_id": chat, "from_id": chat, "text": text, "attachments": attachments,
                            }}})
                            return next((keyboard for keyboard in reversed(client.keyboards)
                                         if keyboard and json.loads(keyboard)["inline"]), None)

                        async def choose(keyboard):
                            button = next(button["action"] for row in json.loads(keyboard)["buttons"] for button in row
                                          if button["action"]["label"] == "Извлечь текст с фото")
                            await submit(7, {"type": "message_event", "object": {
                                "peer_id": 7, "user_id": 7, "event_id": "busy-click", "conversation_message_id": 1,
                                "payload": button["payload"],
                            }})
                            self.assertEqual(client.cleared, [])

                        async def vk_download(url):
                            return download(url.rsplit("/", 1)[-1])

                        with patch("compadre_assistant.presentation.vk.photos._download", side_effect=vk_download):
                            try:
                                await exercise(send, choose, publish)
                            finally:
                                release_download.set()
                                release_model.set()
                                workers.close()
                                processor.close_deferred_actions()

                    execute_action = processor._deferred_actions._run

                    def observe_worker_exit(action, *args):
                        is_first = not entered_download.is_set()
                        try:
                            execute_action(action, *args)
                        finally:
                            if is_first:
                                first_finished.set()

                    with patch.object(processor._deferred_actions, "_run", side_effect=observe_worker_exit):
                        asyncio.run(telegram() if channel == "telegram" else vk())
                self.assertEqual(stages, ["receipt", "download:first", "receipt", "download:second", "model",
                                         "receipt", "download:broken", "receipt", "download:fast", "model"])
                self.assertEqual(sum(marker in answer for _, answer in messages), 1)

        with connect_database(self.path) as connection:
            rows = connection.execute("SELECT actual_model, tokens_json FROM ask_usage").fetchall()
        self.assertEqual([(name, json.loads(tokens)["prompt_tokens"]) for name, tokens in rows],
                         [("test-ocr", 15)] * 4)

    def test_linked_channel_disables_ocr_completion_in_origin_chat(self) -> None:
        """Отзыв персонального доступа в VK скрывает начатый результат в Telegram."""
        release, menu_restored = threading.Event(), threading.Event()
        command = "/get_text_from_image"
        receipt = "Фото получено. Распознаю текст. Для отмены введи /cancel."
        menus = []

        def publish(update):
            commands = tuple(item.command for item in update.menu.items)
            menus.append(commands)
            if len(menus) > 1 and "/cancel" not in commands:
                menu_restored.set()

        with patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, \
                create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                 ask_config=self.config) as processor:
            self.send("telegram", processor, "/register", chat_id=7)
            self.send("telegram", processor, "Тест", chat_id=7)
            link_reply = self.send("telegram", processor, "/link_code", chat_id=7)
            code = re.search(r"/link ([A-Za-z0-9_-]+)", link_reply)
            self.assertIsNotNone(code)
            self.assertIn("привязал", self.send("vk", processor, code.group(0), chat_id=7))
            self.send("telegram", processor, "/ai", chat_id=7)
            self.send("telegram", processor, command, chat_id=7)

            entered = threading.Event()

            def recognize(**_kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Тест не освободил модель")
                return SimpleNamespace(text="Текст после отзыва")

            model_type.return_value.recognize_text.side_effect = recognize
            fake = FakeAiogram()
            _register_handlers(fake.dispatcher, fake, incoming_message_processor=processor, logger=self.logger,
                               menu_update_handler=publish)
            message = TelegramMessage()
            message.chat = SimpleNamespace(id=7)
            message.from_user = SimpleNamespace(id=7)
            message.message_id = 1
            message.photo = [SimpleNamespace(file_id="photo", file_size=None, width=30, height=30)]
            delivered = []

            async def download(_file_id, *, destination, seek):
                destination.write(b"\xff\xd8\xffsample")

            async def send_message(**kwargs):
                delivered.append(kwargs["text"])

            message.download = download
            message.send_message = send_message

            async def exercise():
                try:
                    await fake.dispatcher.included_router.handler(message)
                    self.assertEqual(message.answers, [receipt])
                    self.assertEqual(menus[-1], ("/cancel",))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    disabled = await asyncio.to_thread(self.send, "vk", processor, "/disable ask", 7)
                    self.assertIn("Возможность ask выключена", disabled)
                    release.set()
                    self.assertTrue(await asyncio.to_thread(menu_restored.wait, 5))
                    self.assertNotIn("/ask", menus[-1])
                finally:
                    release.set()
                    await asyncio.to_thread(processor.close_deferred_actions)

            asyncio.run(exercise())
            self.assertEqual(delivered, [])

    def test_admitted_ocr_projects_cancel_only_until_owner_cancels(self) -> None:
        """Обычная публикация и периодическая сверка показывают состояние своего чата."""
        address = ReplyAddress("telegram", "7")
        other = ReplyAddress("telegram", "42")
        menus = {}
        updates = []
        downloads = []

        def publish(update):
            updates.append(update.address)
            menus[update.address] = tuple(item.command for item in update.menu.items)

        def load():
            downloads.append("first")
            return b"\xff\xd8\xffsample"

        with patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel"), \
                create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                 ask_config=self.config) as processor:
            def send(text, *, chat="7", images=()):
                return processor(IncomingEvent("telegram", chat, "update", text, chat, images=images),
                                 completion_handler=lambda _reply: None, menu_update_handler=publish)

            for chat in ("7", "42"):
                send("/register", chat=chat)
                send("Тест", chat=chat)
                send("/ai", chat=chat)
            normal_menu = menus[address]
            self.assertIn("/get_text_from_image", normal_menu)
            send("/get_text_from_image")
            self.assertEqual(menus[address], ("/cancel",))
            response = send("", images=(ImageAttachment(load),))
            self.assertIsNotNone(response.after_delivery)
            self.assertEqual(menus[address], ("/cancel",))
            self.assertEqual(menus[other], normal_menu)
            updates.clear()
            processor.reconcile_menus(publish)
            send("/exit")
            self.assertEqual(updates, [])
            send("/cancel")
            self.assertEqual(updates, [address])
            self.assertEqual(menus[address], normal_menu)
            response.after_delivery(True)
            self.assertEqual(downloads, [])

    def test_ocr_release_refreshes_current_menu_after_delivery_failures(self) -> None:
        """Непринятая квитанция, успех и ошибка восстанавливают меню без нового ввода."""
        address = ReplyAddress("telegram", "7")
        restored = threading.Event()
        menus = []
        replies = []

        def publish(update):
            self.assertEqual(update.address, address)
            commands = tuple(item.command for item in update.menu.items)
            menus.append(commands)
            if "/get_text_from_image" in commands and "/cancel" not in commands:
                restored.set()

        with patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, \
                create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                 ask_config=self.config) as processor:
            model_type.return_value.recognize_text.return_value = SimpleNamespace(text="Готовый текст")

            def send(text, images=()):
                return processor(IncomingEvent("telegram", "7", "update", text, "7", images=images),
                                 completion_handler=replies.append, menu_update_handler=publish)

            send("/register")
            send("Тест")
            send("/ai")
            normal_menu = menus[-1]

            for delivered, photo, result in (
                (False, b"\xff\xd8\xffsample", None),
                (True, b"\xff\xd8\xffsample", "Готовый текст"),
                (True, b"invalid image", "Не удалось прочитать фото"),
            ):
                with self.subTest(delivered=delivered, result=result):
                    replies.clear()
                    restored.clear()
                    send("/get_text_from_image")
                    receipt = send("", (ImageAttachment(lambda: photo),))
                    self.assertEqual(menus[-1], ("/cancel",))
                    receipt.after_delivery(delivered)
                    self.assertTrue(restored.wait(5))
                    self.assertEqual(menus[-1], normal_menu)
                    if result is None:
                        self.assertEqual(replies, [])
                    else:
                        self.assertEqual(len(replies), 1)
                        self.assertIn(result, response_text(replies[0].response))
                    before = len(menus)
                    processor.reconcile_menus(publish)
                    self.assertEqual(len(menus), before)

    def test_cli_channel_runtime_drains_real_usage_to_shared_database(self) -> None:
        """Сохраняет доступные метаданные всех вызовов после выхода каждого канала."""
        profile = replace(self.config, usage_provider="test-gateway")
        success = {"model": "fallback", "choices": [{"message": {"content": "Ответ"}}],
                   "usage": {"prompt_tokens": 19, "completion_tokens": 2}}
        for index, (channel, command, extra) in enumerate((
            ("telegram", run_telegram_bot_command, {}), ("vk", run_vk_bot_command, {"group_id": 123}),
        ), start=1):
            with self.subTest(channel=channel):
                responses = iter((
                    httpx.Response(200, json=success),
                    httpx.Response(200, json={**success, "choices": []}),
                    httpx.Response(500, json={"error": {"message": "synthetic failure"}}),
                    httpx.Response(200, json={"model": "fallback", "choices": success["choices"]}),
                ))
                requests = []

                def handle(request):
                    requests.append(request)
                    return next(responses)

                def polling(*, incoming_message_processor, **_kwargs):
                    self.send(channel, incoming_message_processor, "/register")
                    self.send(channel, incoming_message_processor, "Тест")
                    self.send(channel, incoming_message_processor, "/ai")
                    self.send(channel, incoming_message_processor, "/ask")
                    self.assertIn("Сократи вопрос", self.send(channel, incoming_message_processor, "x" * 101))
                    self.assertEqual(requests, [])
                    self.assertEqual(self.send(channel, incoming_message_processor, "ожидаемый вопрос"), "Ответ")
                    self.assertIn("Не удалось получить ответ", self.send(channel, incoming_message_processor, "/ask пустой"))
                    self.assertIn("Не удалось получить ответ", self.send(channel, incoming_message_processor, "/ask сбой"))
                    self.assertEqual(self.send(channel, incoming_message_processor, "/ask без учёта"), "Ответ")

                with httpx.Client(transport=httpx.MockTransport(handle)) as transport, \
                        OpenAI(base_url=profile.base_url, api_key=profile.api_key,
                               max_retries=0, http_client=transport) as client, \
                        patch("compadre_assistant.bootstrap.load_ask_config", return_value=profile), \
                        patch("compadre_assistant.capabilities.ask.bootstrap.load_ask_config", return_value=profile), \
                        patch("compadre_assistant.capabilities.ask.data.llm.openai_ask_model.OpenAI", return_value=client), \
                        patch(f"compadre_assistant.presentation.cli.main.run_{channel}_bot", side_effect=polling):
                    self.assertEqual(command(database_path=self.path, token="123456:ABCDEF_test_token_123456789",
                                             logger=self.logger, enabled_capabilities=("ask",), **extra), 0)
                self.assertEqual(len(requests), 4)
                with connect_database(self.path) as connection:
                    observations = connection.execute("SELECT provider, actual_model, tokens_json FROM ask_usage").fetchall()
                self.assertEqual(len(observations), index * 4)
                self.assertTrue(all(row["provider"] == "test-gateway" for row in observations))
                self.assertCountEqual([
                    (row["actual_model"], row["tokens_json"] is not None) for row in observations
                ], [("fallback", True), ("fallback", True), (None, False), ("fallback", False)] * index)

    def test_usage_write_error_preserves_answer_and_later_observations(self) -> None:
        """Ошибка реальной SQLite-записи не влияет на доставку и следующий вопрос."""
        with connect_database(self.path) as connection, connection:
            connection.execute("""
                CREATE TRIGGER reject_usage BEFORE INSERT ON ask_usage
                BEGIN SELECT RAISE(ABORT, 'private error'); END
            """)
        sdk_path = "compadre_assistant.capabilities.ask.data.llm.openai_ask_model.OpenAI"
        with patch(sdk_path) as sdk:
            sdk.return_value.chat.completions.create.return_value = SimpleNamespace(
                model="untargeted", choices=[SimpleNamespace(message=SimpleNamespace(content="Ответ"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )
            with self.assertLogs("compadre_assistant.capabilities.ask.data.background_usage_recorder", "ERROR") as logs:
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                      ask_config=self.config) as processor:
                    self.send("telegram", processor, "/register")
                    self.send("telegram", processor, "Тест")
                    self.send("telegram", processor, "/ai")
                    self.assertEqual(self.send("telegram", processor, "/ask первый"), "Ответ")
            self.assertEqual([record.getMessage() for record in logs.records], [
                f"AI usage recording failed: error_type={sqlite3.IntegrityError.__name__}",
            ])
            with connect_database(self.path) as connection, connection:
                connection.execute("DROP TRIGGER reject_usage")
            with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",),
                                                  ask_config=self.config) as processor:
                self.assertEqual(self.send("telegram", processor, "/ask второй"), "Ответ")
        with connect_database(self.path) as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT actual_model FROM ask_usage")], ["untargeted"])

    def test_registry_failure_closes_created_ask_client(self) -> None:
        """Сбой регистрации после сборки Ask освобождает принадлежащий ему клиент."""
        with patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, patch("compadre_assistant.bootstrap.ChannelMessageProcessor", side_effect=ValueError("registry")):
            with self.assertRaisesRegex(ValueError, "registry"):
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config):
                    self.fail("Сбой регистрации должен прервать запуск")
            model_type.return_value.close.assert_called_once_with()

    def test_acknowledgement_precedes_slow_model_and_long_answer_is_split(self) -> None:
        """Оба канала доставляют подтверждение до ответа и сохраняют длинный текст."""
        for channel in ("telegram", "vk"):
            accepted = threading.Event()
            answer = "я" * 10000
            def respond(**_kwargs):
                self.assertTrue(accepted.wait(2), "Подтверждение должно прийти до завершения API")
                return answer
            with self.subTest(channel=channel), patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
                model_type.return_value.answer.side_effect = respond
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config) as processor:
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "Тест")
                    self.send(channel, processor, "/ai")
                    receipt = "Вопрос принят. Готовлю ответ… Для отмены введи /cancel."
                    self.send(channel, processor, "/ask вопрос", on_delivery=lambda text: accepted.set() if text == receipt else None)
                    self.assertEqual(self.delivered[0], receipt)
                    self.assertGreater(len(self.delivered), 2)
                    self.assertEqual("".join(self.delivered[1:]), answer)
                    self.send(channel, processor, "/ask")
                    self.assertEqual(len(self.delivered), 1)

    def test_help_matrix_matches_enabled_capabilities(self) -> None:
        """Справка обоих каналов показывает только включённые входы и верный выход."""
        for enabled in ((), ("obsidian",), ("ask",), ("obsidian", "ask")):
            with self.subTest(enabled=enabled), patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=enabled, obsidian_config=ObsidianConfig(), ask_config=self.config) as processor:
                    for channel in ("telegram", "vk"):
                        self.send(channel, processor, "/register")
                        self.send(channel, processor, "Тест")
                        if "obsidian" in enabled:
                            with connect_database(self.path) as connection:
                                owner = connection.execute(
                                    "SELECT app_user_id FROM external_identities "
                                    "WHERE channel = ? AND external_user_id = '7'",
                                    (channel,),
                                ).fetchone()[0]
                                SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                                    UserCapabilityPreference(owner, "obsidian", True)
                                )
                        self.send(channel, processor, "/exit")
                        help_text = self.send(channel, processor, "/help")
                        self.assertNotIn("/ask", help_text)
                        self.assertEqual("/ai" in help_text, "ask" in enabled)
                        self.assertEqual("/obsidian" in help_text, "obsidian" in enabled)
                        if "ask" in enabled:
                            self.send(channel, processor, "/ai")
                            mode_help = self.send(channel, processor, "/help")
                            self.assertIn("/ask", mode_help)
                            self.assertIn("/exit", mode_help)
                if "ask" not in enabled:
                    model_type.assert_not_called()
