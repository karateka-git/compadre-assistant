"""Проверяет доставку управляемых ответов через VK API 5.199."""

from dataclasses import replace
from io import BytesIO
from itertools import count
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_contracts import ActionScope
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import ReplyAction, SectionActionsQueryPort
from compadre_assistant.core.presentation.message_content import TextSpan, TextStyle, compose_content
from compadre_assistant.presentation.vk.bot import (
    VkApiClient, VkBotError, _create_vk_menu_update_handler, _handle_update, _safe_send_vk_message,
)
from tests.test_response_views import RECOVERY, EditableCapability


class VkEditableCapability(EditableCapability, SectionActionsQueryPort):
    """Нейтральный сценарий меняет контекстное меню после первого выбора."""

    def handle(self, context, event):
        """Выделяет состояние, чтобы проверить, что редактируемый ответ едет plain."""
        result = super().handle(context, event)
        content = compose_content(
            TextSpan(f"Выбрано: <{', '.join(self.choices)}>.", TextStyle.EMPHASIS),
            " Открыть: /show_view.",
        )
        return replace(result, response=replace(result.response, content=content))

    def section_actions(self, app_user, address, mode_revision, pending):
        """Возвращает действия раздела, которые меняются при первом выборе."""
        actions = (
            ReplyAction("Открыть вид", "/show_view", scope=ActionScope.SECTION),
            ReplyAction("Очистить выбор", "/clear_view", scope=ActionScope.SECTION),
        )
        if self.choices:
            return actions
        return (*actions, ReplyAction("Изменить вид", "/adjust_view", scope=ActionScope.SECTION))


class VkResponseViewsTest(unittest.TestCase):
    """Использует настоящий процессор и клиент VK с локальным HTTP-ответом."""

    def setUp(self) -> None:
        """Готовит два ID для сообщения VK, отличных от ID события."""
        database = Path(self.enterContext(TemporaryDirectory(prefix="vk-response-views-"))) / "app.db"
        with connect_database(database) as connection:
            apply_migrations(connection)
            identity = create_user_identity_service(connection)
            identity.register_with_name(IncomingIdentity("vk", "2002", "2002"), display_name="Тест")
        self.capability = VkEditableCapability()
        self.processor = ChannelMessageProcessor(database_path=database, capabilities=(self.capability,))
        self.processor(IncomingEvent("vk", "2002", "1", "/selected", "2002"))
        self.client = VkApiClient(token="fake-vk-token")
        self.logger = logging.getLogger("test.vk.response_views")
        self.menu = _create_vk_menu_update_handler(self.client, logger=self.logger)
        self.request_ids = count(100)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.send_response = [{"peer_id": 2002, "message_id": 9001, "conversation_message_id": 42}]
        self.edit_response = 1
        self.bottom_menu_error = False
        self.enterContext(patch("compadre_assistant.presentation.vk.bot.urlopen", side_effect=self.fake_vk))

    def fake_vk(self, request, *, timeout):
        """Возвращает схемы VK для peer_ids, peer_id, edit и ACK без сети."""
        method = request.full_url.rsplit("/", 1)[-1]
        params = {key: value[0] for key, value in parse_qs(request.data.decode()).items()}
        self.calls.append((method, params))
        if method == "messages.send":
            if "peer_ids" in params:
                result = self.send_response
            else:
                if self.bottom_menu_error and params.get("keyboard"):
                    raise VkBotError("VK не принял нижнее меню")
                result = 9002
        elif method == "messages.edit":
            result = self.edit_response
        elif method == "messages.sendMessageEventAnswer":
            result = 1
        elif method == "messages.getByConversationMessageId":
            result = {"items": [{"id": 9001, "text": "Выбрано: <>. Открыть: /show_view."}]}
        else:
            self.fail(f"Неожиданный вызов: {method}")
        return BytesIO(json.dumps({"response": result}).encode())

    def send(self, text: str, *, with_menu: bool = False) -> None:
        """Передаёт входящую ручную команду через VK Long Poll."""
        self.handle({"type": "message_new", "object": {"message": {
            "peer_id": 2002, "from_id": 2002, "id": next(self.request_ids), "text": text,
        }}}, with_menu=with_menu)

    def press(self, token: str, cmid: object = 42, *, with_menu: bool = False) -> None:
        """Передаёт callback с реальным cmid и независимым event_id."""
        self.handle({"type": "message_event", "object": {
            "peer_id": 2002, "user_id": 2002, "event_id": "callback-id",
            "conversation_message_id": cmid, "payload": json.dumps({"token": token}),
        }}, with_menu=with_menu)

    def handle(self, update: dict, *, with_menu: bool) -> None:
        """Запускает production-маршрут адаптера."""
        _handle_update(
            update, incoming_message_processor=self.processor, client=self.client,
            logger=self.logger, menu_update_handler=self.menu if with_menu else None,
        )

    def requests(self, method: str) -> list[dict[str, str]]:
        """Выбирает сохранённые POST-параметры одного метода."""
        return [params for called, params in self.calls if called == method]

    def open_view(self, *, with_menu: bool = False) -> list[str]:
        """Открывает управляемый ответ и возвращает видимые кнопки."""
        self.send("/show_view", with_menu=with_menu)
        published = next(params for params in reversed(self.requests("messages.send")) if "peer_ids" in params)
        return [token for _, token in self.inline_actions(published)]

    def inline_actions(self, params: dict[str, str]) -> list[tuple[str, str]]:
        """Извлекает подписи и токены одной inline-клавиатуры."""
        keyboard = json.loads(params["keyboard"])
        self.assertTrue(keyboard["inline"])
        return [
            (button["action"]["label"], json.loads(button["action"]["payload"])["token"])
            for row in keyboard["buttons"] for button in row
        ]

    def bottom_labels(self, params: dict[str, str]) -> set[str]:
        """Извлекает подписи нижней клавиатуры."""
        keyboard = json.loads(params["keyboard"])
        self.assertFalse(keyboard["inline"])
        return {button["action"]["label"] for row in keyboard["buttons"] for button in row}

    def test_same_cmid_is_used_for_sequential_selection_and_manual_clear(self) -> None:
        """VK редактирует cmid из send, выпускает свежие токены и снимает кнопки."""
        first, old_sibling, _ = self.open_view()
        publication, = self.requests("messages.send")
        self.assertEqual(publication["peer_ids"], "2002")
        self.assertNotIn("format_data", publication)
        self.assertIn("Выбрано: <>.", publication["message"])
        for wrong_cmid in (9001, 0, True, "42"):
            with self.subTest(wrong_cmid=wrong_cmid):
                self.press(first, wrong_cmid)
        self.assertEqual(self.requests("messages.edit"), [])
        self.assertEqual(self.capability.choices, [])
        self.assertIn("больше недоступна", self.requests("messages.send")[-1]["message"])
        sent = len(self.requests("messages.send"))

        self.press(first)
        self.assertEqual(self.requests("messages.sendMessageEventAnswer")[-1]["event_id"], "callback-id")
        edit, = self.requests("messages.edit")
        self.assertEqual((edit["peer_id"], edit["cmid"]), ("2002", "42"))
        self.assertNotIn("format_data", edit)
        self.assertIn("Выбрано: <A>.", edit["message"])
        self.assertEqual(len(self.requests("messages.send")), sent)
        actions = self.inline_actions(edit)
        self.assertEqual([label for label, _ in actions], ["B", "C"])
        new_sibling = actions[0][1]
        self.assertNotEqual(old_sibling, new_sibling)

        self.press(old_sibling)
        self.assertEqual(len(self.requests("messages.edit")), 1)
        self.assertIn("больше недоступна", self.requests("messages.send")[-1]["message"])
        self.press(new_sibling)
        self.assertEqual(self.capability.choices, ["A", "B"])
        self.assertEqual([label for label, _ in self.inline_actions(self.requests("messages.edit")[-1])], ["C"])

        self.send("/clear_view")
        edits = self.requests("messages.edit")
        self.assertEqual([params["cmid"] for params in edits], ["42"] * 3)
        self.assertEqual(json.loads(edits[-1]["keyboard"]), {"inline": True, "buttons": []})
        self.assertIn("Выбрано: <A, B>.", edits[-1]["message"])
        self.assertEqual(len(self.requests("messages.send")), sent + 1)
        self.assertEqual(self.requests("messages.getByConversationMessageId"), [])

    def test_changed_lower_menu_is_sent_separately_from_inline_edit(self) -> None:
        """Сверка публикует нижнюю клавиатуру один раз на новый снимок."""
        first, *_ = self.open_view(with_menu=True)
        _, first_menu = self.requests("messages.send")
        self.assertIn("Изменить вид", self.bottom_labels(first_menu))

        self.press(first, with_menu=True)
        edit, = self.requests("messages.edit")
        self.assertTrue(self.inline_actions(edit))
        updated_menu = self.requests("messages.send")[-1]
        self.assertIn("Действия раздела:", updated_menu["message"])
        self.assertIn("Открыть вид", updated_menu["message"])
        self.assertIn("/help", updated_menu["message"])
        self.assertNotIn("Изменить вид", self.bottom_labels(updated_menu))
        self.assertIn("Очистить выбор", self.bottom_labels(updated_menu))
        self.assertEqual([method for method, _ in self.calls[-3:]],
                         ["messages.sendMessageEventAnswer", "messages.edit", "messages.send"])

        self.send("/clear_view", with_menu=True)
        self.assertEqual(len(self.requests("messages.send")), 3)
        self.assertEqual(len(self.requests("messages.edit")), 2)

    def test_failed_edit_keeps_menu_and_sends_recovery_without_repeat(self) -> None:
        """Отрицательная квитанция отдаёт меню с подсказкой после единственной мутации."""
        first, *_ = self.open_view(with_menu=True)
        self.edit_response = 0
        with self.assertLogs(self.logger, level="ERROR"):
            self.press(first, with_menu=True)
        self.assertEqual(self.capability.choices, ["A"])
        self.assertEqual(len(self.requests("messages.edit")), 1)
        recovery = self.requests("messages.send")[-1]
        self.assertEqual(recovery["message"], RECOVERY)
        self.assertNotIn("Изменить вид", self.bottom_labels(recovery))
        self.send("/adjust_view", with_menu=True)
        self.press(first, with_menu=True)
        self.assertEqual(self.capability.choices, ["A"])
        self.assertEqual(len(self.requests("messages.edit")), 1)

    def test_failed_menu_delivery_does_not_discard_successful_edit(self) -> None:
        """Кнопки остаются активными после сбоя отдельного сообщения с нижним меню."""
        first, *_ = self.open_view(with_menu=True)
        self.bottom_menu_error = True
        with self.assertLogs(self.logger, level="ERROR"):
            self.press(first, with_menu=True)
        token = self.inline_actions(self.requests("messages.edit")[-1])[0][1]

        self.bottom_menu_error = False
        self.press(token, with_menu=True)
        self.assertEqual(self.capability.choices, ["A", "B"])
        self.assertEqual(len(self.requests("messages.edit")), 2)
        self.assertIn("Очистить выбор", self.bottom_labels(self.requests("messages.send")[-1]))

    def test_unverified_send_cmid_does_not_enable_actions(self) -> None:
        """Одиночный message_id, чужой peer и нулевой cmid не становятся целью."""
        for invalid in (9001, [{"peer_id": 3003, "conversation_message_id": 42}],
                        [{"peer_id": 2002, "conversation_message_id": 0}]):
            with self.subTest(invalid=invalid):
                self.send_response = invalid
                with self.assertLogs(self.logger, level="ERROR"):
                    first, *_ = self.open_view()
                self.press(first)
                self.assertEqual(self.capability.choices, [])
                self.send("/adjust_view")
                self.assertEqual(self.requests("messages.send")[-1]["message"], RECOVERY)
        self.assertEqual(self.requests("messages.edit"), [])

    def test_pending_edit_is_skipped_if_newer_result_already_delivered(self) -> None:
        """Устаревший результат даёт подсказку без сетевого edit."""
        self.open_view()
        earlier = self.processor(IncomingEvent("vk", "2002", "4", "/adjust_view", "2002"))
        self.send("/clear_view")
        _safe_send_vk_message(self.client, peer_id=2002, response=earlier, logger=self.logger)
        self.assertEqual(len(self.requests("messages.edit")), 1)
        self.assertEqual(self.requests("messages.send")[-1]["message"], RECOVERY)
