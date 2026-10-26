"""Проверяет одноразовый Ask через настоящие входы Telegram и VK без сети."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
from html import escape
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.presentation.telegram.bot import _register_handlers
from compadre_assistant.presentation.vk.bot import _handle_update
from compadre_assistant.presentation.cli.main import run_telegram_bot_command, run_vk_bot_command
from tests.test_telegram_bot import FakeAiogram, TelegramMessage
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

    def send(self, channel, processor, text, chat_id=42, on_delivery=None):
        """Проводит сообщение через канал и возвращает доставленный текст."""
        if channel == "telegram":
            aiogram = FakeAiogram()
            _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)
            message = TelegramMessage()
            original_answer = message.answer
            async def answer(text, *, parse_mode):
                await original_answer(text, parse_mode=parse_mode)
                if on_delivery:
                    on_delivery(text)
            message.answer = answer
            async def send_message(*, chat_id, text, parse_mode):
                await answer(text, parse_mode=parse_mode)
            message.send_message = send_message
            message.chat = SimpleNamespace(id=chat_id)
            message.from_user = SimpleNamespace(id=7, username=None, full_name="Тест")
            message.message_id = 1
            message.text = text
            asyncio.run(aiogram.dispatcher.included_router.handler(message))
            self.delivered = message.answers
            return "\n".join(text for text in message.answers if text != "Вопрос принят. Готовлю ответ…")
        client = FakeVkClient()
        original_send = client.send_message
        def send_message(**kwargs):
            result = original_send(**kwargs)
            if on_delivery:
                on_delivery(kwargs["text"])
            return result
        client.send_message = send_message
        _handle_update(
            {"type": "message_new", "object": {"message": {"peer_id": chat_id, "from_id": 7, "id": 1, "text": text}}},
            incoming_message_processor=processor, client=client, logger=self.logger,
        )
        self.delivered = [item[1] for item in client.messages]
        return "\n".join(text for text in self.delivered if text != "Вопрос принят. Готовлю ответ…")

    def test_registered_questions_are_independent_and_do_not_select_mode(self) -> None:
        """Оба канала требуют завершённую регистрацию и выполняют только /ask."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel), patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
                model = model_type.return_value
                model.answer.side_effect = lambda **kwargs: "Ответ: " + kwargs["question"]
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config) as processor:
                    self.send(channel, processor, "/ask до регистрации")
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "/ask до имени")
                    model.answer.assert_not_called()
                    self.send(channel, processor, "/name Тест")
                    for question in ("первый", "https://example.com/article"):
                        self.assertEqual(self.send(channel, processor, "/ask " + question), "Ответ: " + question)
                    self.assertEqual([call.kwargs["question"] for call in model.answer.call_args_list], ["первый", "https://example.com/article"])
                    for text in ("обычный вопрос", "/unknown вопрос", "/asker вопрос"):
                        self.send(channel, processor, text)
                    self.assertEqual(model.answer.call_count, 2)
                    self.assertIn(escape("/ask <вопрос>") if channel == "telegram" else "/ask <вопрос>", self.send(channel, processor, "/ask"))
                    self.assertIn("100", self.send(channel, processor, "/ask " + "я" * 101))
                    self.assertEqual(model.answer.call_count, 2)
                    with connect_database(self.path) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_chat_modes").fetchone()[0], 0)
                model.close.assert_called_once_with()

    def test_ask_can_be_disabled_and_enabled_without_restart(self) -> None:
        """Команды меняют личный допуск Ask при живом общем processor."""
        with patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
            model_type.return_value.answer.return_value = "Ответ"
            with create_channel_message_processor(
                database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config
            ) as processor:
                self.send("telegram", processor, "/register")
                self.send("telegram", processor, "/name Тест")
                self.assertIn("выключена", self.send("telegram", processor, "/disable ask"))
                self.assertIn("/enable ask", self.send("telegram", processor, "/ask вопрос"))
                model_type.return_value.answer.assert_not_called()
                self.assertIn("включена", self.send("telegram", processor, "/enable ask"))
                self.assertEqual(self.send("telegram", processor, "/ask вопрос"), "Ответ")

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
                    self.send(channel, processor, "/name Тест")
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(self.send, channel, processor, "/ask долгий")
                        try:
                            self.assertTrue(started.wait(5))
                            self.assertIn("выключена", self.send(channel, processor, "/disable ask"))
                            model.close.assert_not_called()
                        finally:
                            release.set()
                        self.assertEqual(future.result(timeout=5), "Ответ: долгий")
                    self.assertIn("/enable ask", self.send(channel, processor, "/ask следующий"))
                    self.assertEqual(model.answer.call_count, 1)
                    self.assertIn("включена", self.send(channel, processor, "/enable ask"))
                    self.assertEqual(self.send(channel, processor, "/ask другой"), "Ответ: другой")
                model.close.assert_called_once_with()

    def test_pending_obsidian_and_each_chat_mode_survive_answers_and_errors(self) -> None:
        """Ask обходит настоящий Obsidian handler, не применяя pending и ссылки."""
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
                    self.send(channel, processor, "/name Тест")
                    with connect_database(self.path) as connection:
                        owner = connection.execute(
                            "SELECT app_user_id FROM external_identities WHERE channel = ? AND external_user_id = '7'",
                            (channel,),
                        ).fetchone()[0]
                        SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                            UserCapabilityPreference(owner, "obsidian", True)
                        )
                    self.assertIn("да", self.send(channel, processor, "/obsidian"))
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
                    self.send(channel, processor, "/name Тест")
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
                self.send(channel, incoming_message_processor, "/name Тест")
                self.assertEqual(self.send(channel, incoming_message_processor, "/ask вопрос"), "Ответ")

            with self.subTest(channel=channel), patch("compadre_assistant.bootstrap.load_ask_config", return_value=self.config), patch("compadre_assistant.capabilities.ask.bootstrap.load_ask_config", return_value=self.config) as loader, patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, patch(f"compadre_assistant.presentation.cli.main.run_{channel}_bot", side_effect=polling):
                model_type.return_value.answer.return_value = "Ответ"
                self.assertEqual(command(database_path=self.path, token="123456:ABCDEF_test_token_123456789", logger=self.logger, enabled_capabilities=("ask",), **extra), 0)
                loader.assert_called_once_with()
                model_type.return_value.close.assert_called_once_with()

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
                    self.send(channel, processor, "/name Тест")
                    self.send(channel, processor, "/ask вопрос", on_delivery=lambda text: accepted.set() if text == "Вопрос принят. Готовлю ответ…" else None)
                    self.assertEqual(self.delivered[0], "Вопрос принят. Готовлю ответ…")
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
                        self.send(channel, processor, "/name Тест")
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
                        help_text = self.send(channel, processor, "/help")
                        self.assertEqual("/ask" in help_text, "ask" in enabled)
                        self.assertEqual("/obsidian" in help_text, "obsidian" in enabled)
                        for line in help_text.splitlines():
                            if "/ask" in line:
                                self.assertNotIn("/exit", line)
                if "ask" not in enabled:
                    model_type.assert_not_called()
