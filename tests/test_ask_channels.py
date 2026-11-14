"""Проверяет режим ИИ через настоящие входы Telegram и VK без сети."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import logging
from html import escape
import json
import sqlite3
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
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

    def send(self, channel, processor, text, chat_id=42, on_delivery=None, *, images=(), load=None):
        """Проводит сообщение через канал и возвращает доставленный текст."""
        receipt = "Фото получено. Распознаю текст. Для отмены введи /cancel."
        progress = ("Вопрос принят. Готовлю ответ…", receipt)
        ocr_finished = threading.Event()
        if channel == "telegram":
            aiogram = FakeAiogram()
            _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)
            message = TelegramMessage()
            original_answer = message.answer
            async def answer(text, *, parse_mode, reply_markup=None):
                await original_answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
                if on_delivery:
                    on_delivery(text)
            message.answer = answer
            async def send_message(*, chat_id, text, parse_mode, reply_markup=None):
                await answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
                ocr_finished.set()
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
                if receipt in message.answers:
                    self.assertTrue(await asyncio.to_thread(ocr_finished.wait, 5))

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
            if client.messages and client.messages[0][1] == receipt and len(client.messages) > 1:
                ocr_finished.set()
            return result
        client.send_message = send_message
        attachments = [{"type": "photo", "photo": {"sizes": [{
            "url": f"https://sun1.userapi.com/{file_id}", "width": 30, "height": 30,
        }]}} for file_id in images]

        def handle_update():
            _handle_update({"type": "message_new", "object": {"message": {
                "peer_id": chat_id, "from_id": 7, "id": 1, "text": text, "attachments": attachments,
            }}}, incoming_message_processor=processor, client=client, logger=self.logger)

        if images:
            async def download(url):
                return load(url.rsplit("/", 1)[-1])

            with patch("compadre_assistant.presentation.vk.photos._download", side_effect=download):
                handle_update()
                if client.messages[0][1] == receipt:
                    self.assertTrue(ocr_finished.wait(5))
        else:
            handle_update()
        self.delivered = [item[1] for item in client.messages]
        self.keyboard = client.keyboards[-1]
        return "\n".join(text for text in self.delivered if text not in progress)

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
        """Команды меняют личный допуск Ask при живом общем processor."""
        with patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model_type:
            model_type.return_value.answer.return_value = "Ответ"
            with create_channel_message_processor(
                database_path=self.path, enabled_capabilities=("ask",), ask_config=self.config
            ) as processor:
                self.send("telegram", processor, "/register")
                self.send("telegram", processor, "Тест")
                self.assertIn("выключена", self.send("telegram", processor, "/disable ask"))
                self.assertIn("/enable ask", self.send("telegram", processor, "/ask вопрос"))
                model_type.return_value.answer.assert_not_called()
                self.assertIn("включена", self.send("telegram", processor, "/enable ask"))
                self.send("telegram", processor, "/ai")
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
                    self.send(channel, processor, "Тест")
                    self.send(channel, processor, "/ai")
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
