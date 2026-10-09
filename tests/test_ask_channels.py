"""Проверяет одноразовый Ask через настоящие входы Telegram и VK без сети."""

import asyncio
from dataclasses import replace
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from obs_chat_bot.bootstrap import create_channel_message_processor
from obs_chat_bot.capabilities.ask.data.configuration import AskConfig
from obs_chat_bot.capabilities.ask.domain.errors import (
    AskModelTimeoutError, AskModelUnavailableError, InvalidAskAnswerError,
)
from obs_chat_bot.capabilities.obsidian.data.configuration.config import ObsidianConfig
from obs_chat_bot.capabilities.obsidian.domain.pending import PendingOrigin
from obs_chat_bot.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.presentation.telegram.bot import _register_handlers
from obs_chat_bot.presentation.vk.bot import _handle_update
from obs_chat_bot.presentation.cli.main import run_telegram_bot_command, run_vk_bot_command
from test_telegram_bot import FakeAiogram, TelegramMessage
from test_vk_bot import FakeVkClient
from test_process_incoming_message import FakeArticleUrlUseCase, FakeConfirmationService, _proposal


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

    def send(self, channel, processor, text, chat_id=42):
        """Проводит сообщение через канал и возвращает доставленный текст."""
        if channel == "telegram":
            aiogram = FakeAiogram()
            _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)
            message = TelegramMessage()
            message.chat = SimpleNamespace(id=chat_id)
            message.from_user = SimpleNamespace(id=7, username=None, full_name="Тест")
            message.message_id = 1
            message.text = text
            asyncio.run(aiogram.dispatcher.included_router.handler(message))
            return "\n".join(message.answers)
        client = FakeVkClient()
        _handle_update(
            {"type": "message_new", "object": {"message": {"peer_id": chat_id, "from_id": 7, "id": 1, "text": text}}},
            incoming_message_processor=processor, client=client, logger=self.logger,
        )
        return "\n".join(item[1] for item in client.messages)

    def test_registered_questions_are_independent_and_do_not_select_mode(self) -> None:
        """Оба канала требуют завершённую регистрацию и выполняют только /ask."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel), patch("obs_chat_bot.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
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
                    self.assertIn("/ask <вопрос>", self.send(channel, processor, "/ask"))
                    self.assertIn("100", self.send(channel, processor, "/ask " + "я" * 101))
                    self.assertEqual(model.answer.call_count, 2)
                    with connect_database(self.path) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_chat_modes").fetchone()[0], 0)
                model.close.assert_called_once_with()

    def test_pending_obsidian_and_each_chat_mode_survive_answers_and_errors(self) -> None:
        """Ask обходит настоящий Obsidian handler, не применяя pending и ссылки."""
        for channel in ("telegram", "vk"):
            proposal = replace(_proposal(), origin=PendingOrigin(channel, "42"))
            confirmation = FakeConfirmationService(proposal)
            article = FakeArticleUrlUseCase()
            obsidian = ObsidianMessageHandler(article_url_use_case=article, obsidian_confirmation_service=confirmation)
            with self.subTest(channel=channel), patch("obs_chat_bot.bootstrap.ObsidianCapability", return_value=obsidian), patch("obs_chat_bot.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
                model = model_type.return_value
                model.answer.return_value = "/confirm obsidian review 1 1 yes"
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("obsidian", "ask"), obsidian_config=ObsidianConfig(), ask_config=self.config) as processor:
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "/name Тест")
                    self.assertIn("да", self.send(channel, processor, "/obsidian"))
                    with connect_database(self.path) as connection:
                        before = tuple(connection.execute("SELECT * FROM active_chat_modes").fetchall())
                    self.assertEqual(self.send(channel, processor, "/ask https://example.com/article"), model.answer.return_value)
                    for error in (AskModelTimeoutError("timeout"), AskModelUnavailableError("private provider details"), InvalidAskAnswerError("invalid")):
                        model.answer.side_effect = error
                        reply = self.send(channel, processor, "/ask вопрос")
                        self.assertIn("позже", reply)
                        self.assertNotIn(str(error), reply)
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
        with patch("obs_chat_bot.capabilities.ask.bootstrap.load_ask_config") as loader, patch("obs_chat_bot.capabilities.ask.bootstrap.OpenAIAskModel") as model:
            with create_channel_message_processor(database_path=self.path, enabled_capabilities=()) as processor:
                for channel in ("telegram", "vk"):
                    self.send(channel, processor, "/register")
                    self.send(channel, processor, "/name Тест")
                    self.assertIn("отключ", self.send(channel, processor, "/ask вопрос"))
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

            with self.subTest(channel=channel), patch("obs_chat_bot.capabilities.ask.bootstrap.load_ask_config", return_value=self.config) as loader, patch("obs_chat_bot.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, patch(f"obs_chat_bot.presentation.cli.main.run_{channel}_bot", side_effect=polling):
                model_type.return_value.answer.return_value = "Ответ"
                self.assertEqual(command(database_path=self.path, token="123456:ABCDEF_test_token_123456789", logger=self.logger, enabled_capabilities=("ask",), **extra), 0)
                loader.assert_called_once_with()
                model_type.return_value.close.assert_called_once_with()

    def test_registry_failure_closes_created_ask_client(self) -> None:
        """Сбой регистрации после сборки Ask освобождает принадлежащий ему клиент."""
        with patch("obs_chat_bot.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, patch("obs_chat_bot.bootstrap.ChannelMessageProcessor", side_effect=ValueError("registry")):
            with self.assertRaisesRegex(ValueError, "registry"):
                with create_channel_message_processor(database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config):
                    self.fail("Сбой регистрации должен прервать запуск")
            model_type.return_value.close.assert_called_once_with()
