"""Проверяет общий экран и независимую проекцию раздела в VK."""

from dataclasses import replace
from itertools import count
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, SectionMenuDelivery
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    BoundReplyAction, CommandMenu, CommandMenuUpdate, CommandSuggestion, IncomingResponse, ReplyAction, ResponseView,
    ConfirmationQueryResult, PendingConfirmationQueryPort,
)
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.vk.bot import (
    VK_ACTION_LAYOUT, VkBotError, _create_vk_menu_update_handler, _handle_update,
    _safe_send_vk_message, _serialize_vk_command_menu, _serialize_vk_inline_keyboard,
)
from tests.test_screen_actions_core import ActionCapability
from tests.test_vk_bot import FakeVkClient


class VkActionProjectionTest(unittest.TestCase):
    """Проверяет области, однозначность и ограничения VK-клавиатур."""

    def test_section_and_screen_have_separate_projections_with_url(self):
        """Оба типа ссылок открывают URL без исполняемого payload."""
        section = (ReplyAction("Команда", "/choose first", scope=ActionScope.SECTION),
                   ReplyAction("Сайт раздела", url="https://example.com", scope=ActionScope.SECTION))
        menu = CommandMenu((CommandSuggestion("/help", "Справка"),), section_actions=section)
        bottom = json.loads(_serialize_vk_command_menu(CommandMenuUpdate(ReplyAddress("vk", "1001"), menu)))
        buttons = [button for row in bottom["buttons"] for button in row]
        self.assertEqual([item["action"]["label"] for item in buttons], ["Команда", "Сайт раздела"])
        self.assertEqual(buttons[0]["action"]["payload"], '{"menu":true}')
        self.assertEqual(buttons[1], {"action": {"type": "open_link", "label": "Сайт раздела",
                                               "link": "https://example.com"}})
        response = IncomingResponse(text_content("Экран"), bound_actions=(
            BoundReplyAction("Команда", "a", "r", scope=ActionScope.SECTION),
            BoundReplyAction("1. Предмет", "b", "r", purpose=ActionPurpose.SELECTION),
            BoundReplyAction("Сайт", "", "r", url="https://example.org"),
        ))
        inline = json.loads(_serialize_vk_inline_keyboard(response))
        self.assertTrue(inline["inline"])
        self.assertEqual([[item["action"]["label"] for item in row] for row in inline["buttons"]],
                         [["1. Предмет"], ["Сайт"]])
        self.assertEqual(json.loads(inline["buttons"][0][0]["action"]["payload"]), {"token": "b"})
        self.assertNotIn("payload", inline["buttons"][1][0]["action"])

    def test_empty_section_clears_menu_and_does_not_fall_back_to_catalog(self):
        """Пустой снимок отличается от ещё не перенесённого каталога."""
        menu = CommandMenu((CommandSuggestion("/help", "Справка"),), section_actions=())
        self.assertEqual(json.loads(_serialize_vk_command_menu(
            CommandMenuUpdate(ReplyAddress("vk", "1001"), menu)))["buttons"], [])

    def test_bottom_labels_are_unique_including_links(self):
        """Совпадение подписи ссылки и команды не создаёт неоднозначное меню."""
        actions = (ReplyAction("Открыть", "/choose first", scope=ActionScope.SECTION),
                   ReplyAction("Открыть", url="https://example.com", scope=ActionScope.SECTION))
        with self.assertRaisesRegex(ValueError, "unique"):
            _serialize_vk_command_menu(CommandMenuUpdate(ReplyAddress("vk", "1001"),
                                                        CommandMenu(section_actions=actions)))

    def test_selection_capacity_matches_individual_rows_and_payload_limit(self):
        """Семь адресных позиций не обрезаются до шести незаметно для владельца."""
        action = BoundReplyAction("Предмет", "a", "r", purpose=ActionPurpose.SELECTION)
        self.assertEqual(VK_ACTION_LAYOUT.selection_capacity(reserved_rows=2, reserved_buttons=3), 4)
        for actions in (
            tuple(replace(action, token=str(index)) for index in range(7)),
            (replace(action, label="x" * 41),),
            (replace(action, token="я" * 123),),
            (replace(action, row=True),),
            (replace(action, row=0), replace(action, row=1, token="b"), replace(action, row=0, token="c")),
            (replace(action, url="https://example.com"),),
        ):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                _serialize_vk_inline_keyboard(IncomingResponse(text_content("Введи номер."), bound_actions=actions))


class ProjectionCapability(ActionCapability, PendingConfirmationQueryPort):
    """Возвращает задаваемый тестом экран с тем же построителем раздела."""

    def __init__(self):
        """Разрешает переключать обычный ответ и адресный выбор."""
        super().__init__()
        self.reply = None

    def handle(self, context, event):
        """Сохраняет обычное исполнение и подставляет описание экрана."""
        result = super().handle(context, event)
        return replace(result, response=self.reply) if self.reply is not None else result

    def list_pending_confirmations(self, context, origin):
        """Возвращает полный пустой снимок нейтрального сценария."""
        return ConfirmationQueryResult("complete")


class ProjectionClient(FakeVkClient):
    """Подтверждает ID и сохраняет порядок доставки и очистки без сети."""

    def __init__(self):
        """Готовит последовательность cmid и управляемый сбой отправки."""
        super().__init__()
        self.ids = count(42)
        self.events = []
        self.fail_send = False
        self.fail_after = None
        self.fail_menu = False

    def send_message(self, **kwargs):
        """Подтверждает обычное сообщение после проверки тестового сбоя."""
        keyboard = json.loads(kwargs["keyboard"]) if kwargs.get("keyboard") else None
        if (self.fail_send or self.fail_after == len(self.messages)
                or self.fail_menu and keyboard is not None and not keyboard["inline"]):
            raise VkBotError("test send failure")
        super().send_message(**kwargs)
        message_id = next(self.ids)
        self.events.append(("send", message_id, keyboard))
        return message_id

    def send_editable_message(self, **kwargs):
        """Возвращает cmid сообщения в том же журнале доставки."""
        return self.send_message(**kwargs)

    def edit_message(self, *, peer_id, conversation_message_id, text, keyboard):
        """Фиксирует обновление существующего сообщения."""
        self.events.append(("edit", conversation_message_id, json.loads(keyboard) if keyboard else None))

    def clear_inline_keyboard(self, *, peer_id, conversation_message_id, managed=False):
        """Фиксирует очистку только после подтверждённой доставки."""
        self.cleared.append((peer_id, conversation_message_id))
        self.events.append(("clear", conversation_message_id, None))


class VkScreenDeliveryTest(unittest.TestCase):
    """Проводит действия через реальное ядро, меню и VK Long Poll handlers."""

    def setUp(self):
        """Создаёт изолированную БД и полный путь публикации меню."""
        self.path = Path(self.enterContext(TemporaryDirectory(prefix="vk-screen-actions-"))) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            create_user_identity_service(connection).register_with_name(
                IncomingIdentity("vk", "1001", "1001"), display_name="Тест",
            )
        self.owner = ProjectionCapability()
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(self.owner,))
        self.event = IncomingEvent("vk", "1001", "request", "/selected", "1001")
        self.processor(self.event)
        self.client = ProjectionClient()
        self.logger = logging.getLogger("test.vk.screen-actions")
        self.menu = _create_vk_menu_update_handler(self.client, logger=self.logger)

    def send(self, text="/choose", *, payload=None):
        """Передаёт команду либо помеченную подпись нижнего меню."""
        _handle_update({"type": "message_new", "object": {"message": {
            "peer_id": 1001, "from_id": 1001, "id": 1, "text": text, "payload": payload,
        }}}, incoming_message_processor=self.processor, client=self.client, logger=self.logger,
            menu_update_handler=self.menu)

    def press(self, token, cmid=42):
        """Передаёт адресный callback с подтверждённым cmid."""
        _handle_update({"type": "message_event", "object": {
            "peer_id": 1001, "user_id": 1001, "event_id": "click", "conversation_message_id": cmid,
            "payload": {"token": token},
        }}, incoming_message_processor=self.processor, client=self.client, logger=self.logger,
            menu_update_handler=self.menu)

    def deliver(self, response, *, limit=3500):
        """Доставляет готовый управляемый экран с независимой сверкой меню."""
        return _safe_send_vk_message(
            self.client, peer_id=1001, response=response, logger=self.logger, limit=limit,
            deliver_menu=lambda send: self.processor.deliver_response_menu(self.event, self.menu, send),
        )

    def choice(self, *, long=False):
        """Описывает адресный выбор и ссылку без названия канала."""
        content = "Выбери через /choose first."
        if long:
            content = "Длинная страница. " * 15 + content
        return IncomingResponse(text_content(content), suggested_actions=(
            ReplyAction("1. Объект", "/choose first", purpose=ActionPurpose.SELECTION),
            ReplyAction("Сайт", url="https://example.com"),
        ), view=ResponseView("choice", cleanup_previous=True, allow_multiple_messages=True),
            command_menu_delivery=SectionMenuDelivery.RECONCILE)

    def test_section_actions_arrive_with_content_then_unchanged_menu_is_preserved(self):
        """Основной экран не дублирует нижние команды inline-кнопками."""
        self.send()
        self.assertEqual(self.owner.layout, VK_ACTION_LAYOUT)
        self.assertEqual(len(self.client.messages), 1)
        keyboard = json.loads(self.client.keyboards[-1])
        self.assertFalse(keyboard["inline"])
        self.assertEqual(keyboard["buttons"][0][0]["action"]["label"], "Выбрать")
        self.send()
        self.assertIsNone(self.client.keyboards[-1])
        self.owner.available = ()
        self.send()
        self.assertEqual(json.loads(self.client.keyboards[-1])["buttons"], [])

    def test_menu_payload_uses_current_target_and_duplicate_label_executes_nothing(self):
        """Владелец команды проверяется при каждом выборе подписи."""
        self.send("Выбрать", payload='{"menu":true}')
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")
        self.owner.available = (*self.owner.available, ReplyAction(
            "Выбрать", url="https://example.com", scope=ActionScope.SECTION,
        ))
        calls = len(self.owner.calls)
        with self.assertLogs(self.logger, level="WARNING"):
            self.send("Выбрать", payload='{"menu":true}')
        self.assertEqual(len(self.owner.calls), calls)

    def test_changed_menu_and_screen_choice_are_both_delivered(self):
        """Одна клавиатура на сообщение не теряет ни выбор, ни меню."""
        self.owner.reply = self.choice()
        self.send()
        self.assertEqual([json.loads(value)["inline"] for value in self.client.keyboards], [True, False])
        inline = json.loads(self.client.keyboards[0])
        token = json.loads(inline["buttons"][0][0]["action"]["payload"])["token"]
        self.owner.reply = replace(self.choice(), command_menu_delivery=SectionMenuDelivery.PRESERVE)
        self.owner.reads.clear()
        # Сам ответ сохраняет нижнее меню, но проверки допуска callback остаются.
        self.press(token)
        self.assertEqual(self.owner.layout, VK_ACTION_LAYOUT)
        self.assertTrue(json.loads(self.client.keyboards[-1])["inline"])
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")

    def test_preserve_does_not_query_section_during_delivery(self):
        """PRESERVE не превращается в пустой либо заново вычисленный набор."""
        self.owner.reply = replace(self.choice(), command_menu_delivery=SectionMenuDelivery.PRESERVE)
        response = self.processor(replace(self.event, text="/choose"))
        self.owner.read_error = AssertionError("menu must not be queried")
        self.assertTrue(self.deliver(response))
        self.assertTrue(json.loads(self.client.keyboards[-1])["inline"])

    def test_managed_section_only_attaches_bottom_menu_to_original_content(self):
        """При отсутствии inline-действий не требуется отдельная подсказка."""
        self.owner.reply = IncomingResponse(text_content("Действия раздела. /choose first."),
                                           suggested_actions=self.owner.available, view=ResponseView("section"))
        self.send()
        self.assertEqual(len(self.client.messages), 1)
        self.assertFalse(json.loads(self.client.keyboards[-1])["inline"])
        self.assertIn("Действия раздела", self.client.messages[-1][1])

    def test_menu_send_failure_keeps_screen_and_retries_with_next_content(self):
        """Ошибка нижнего меню не отменяет адресный экран и остаётся для повтора."""
        self.owner.reply = self.choice()
        self.client.fail_menu = True
        with self.assertLogs(self.logger, level="ERROR"):
            self.send()
        self.assertTrue(json.loads(self.client.keyboards[0])["inline"])
        self.assertIn(self.event.reply_address, self.processor.menu_delivery_failures)
        self.client.fail_menu = False
        self.owner.reply = IncomingResponse(text_content("Следующий результат."))
        self.send()
        self.assertFalse(json.loads(self.client.keyboards[-1])["inline"])
        self.assertNotIn(self.event.reply_address, self.processor.menu_delivery_failures)

    def test_multipart_replacement_confirms_last_cmid_and_clears_old_after_send(self):
        """Нижнее меню идёт с частью текста, inline — с последней частью."""
        self.owner.reply = self.choice()
        self.send()
        self.owner.available = (ReplyAction("Новое действие", "/choose next", scope=ActionScope.SECTION),)
        self.owner.reply = replace(self.choice(long=True), view=ResponseView(
            "choice", update=True, unavailable=text_content("Повтори /choose."),
            cleanup_previous=True, allow_multiple_messages=True,
        ))
        response = self.processor(replace(self.event, text="/choose"))
        self.assertTrue(self.deliver(response, limit=45))
        self.assertEqual(self.client.events[-1], ("clear", 42, None))
        sent = [event for event in self.client.events if event[0] == "send"]
        self.assertFalse(sent[-2][2]["inline"])
        self.assertTrue(sent[-1][2]["inline"])
        self.processor.select_action(replace(self.event, callback_message_id=str(sent[-1][1])),
                                     response.bound_actions[0].token)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")

    def test_failed_new_generation_keeps_old_selection_and_keyboard(self):
        """Неудачная новая отправка не активирует ещё не показанную цель."""
        self.owner.reply = self.choice()
        old = self.processor(replace(self.event, text="/choose"))
        self.assertTrue(self.deliver(old))
        self.owner.reply = self.choice(long=True)
        new = self.processor(replace(self.event, text="/choose"))
        self.client.fail_after = len(self.client.messages) + 1
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertFalse(self.deliver(new, limit=45))
        self.assertEqual(self.client.cleared, [])
        calls = len(self.owner.calls)
        self.processor.select_action(replace(self.event, callback_message_id="42"), new.bound_actions[0].token)
        self.assertEqual(len(self.owner.calls), calls)
        self.processor.select_action(replace(self.event, callback_message_id="42"), old.bound_actions[0].token)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")

    def test_failed_ordinary_callback_reply_preserves_source_keyboard(self):
        """Обычная исходная кнопка не снимается после неотправленного результата."""
        self.owner.reply = IncomingResponse(text_content("Открыть /choose first."), suggested_actions=(
            ReplyAction("Открыть", "/choose first"),
        ))
        self.send()
        source_id = self.client.events[-1][1]
        token = json.loads(json.loads(self.client.keyboards[-1])["buttons"][0][0]["action"]["payload"])["token"]
        self.client.fail_send = True
        with self.assertLogs(self.logger, level="ERROR"):
            self.press(token, source_id)
        self.assertEqual(self.client.cleared, [])

    def test_unavailable_inline_buttons_keep_manual_text_and_delivery(self):
        """Ошибка локальной раскладки не скрывает текст или команду."""
        self.owner.reply = replace(self.choice(), suggested_actions=(
            ReplyAction("x" * 41, "/choose first", purpose=ActionPurpose.SELECTION),
        ))
        with self.assertLogs(self.logger, level="WARNING"):
            self.send()
        self.assertIn("/choose first", self.client.messages[0][1])
        self.assertFalse(json.loads(self.client.keyboards[0])["inline"])
        self.owner.reply = None
        self.send("/choose first")
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")

    def test_stale_url_reservation_never_reaches_client(self):
        """Устаревшее поколение не отправляет старую URL-кнопку."""
        self.owner.reply = self.choice()
        old = self.processor(replace(self.event, text="/choose"))
        current = self.processor(replace(self.event, text="/choose"))
        self.assertTrue(self.deliver(current))
        count_before = len(self.client.messages)
        self.assertFalse(self.deliver(old))
        self.assertEqual(len(self.client.messages), count_before)

    def test_edit_with_changed_menu_names_available_actions(self):
        """Сопровождение нижнего меню объясняет следующий шаг после edit."""
        self.owner.reply = self.choice()
        self.send()
        self.owner.available = (ReplyAction("Следующая операция", "/choose next", scope=ActionScope.SECTION),)
        self.owner.reply = replace(self.choice(), view=ResponseView(
            "choice", update=True, unavailable=text_content("Повтори /choose."), cleanup_previous=True,
        ))
        self.send()
        self.assertEqual(self.client.events[-2][0], "edit")
        self.assertIn("Следующая операция", self.client.messages[-1][1])
        self.assertNotIn("/help", self.client.messages[-1][1])
        self.assertFalse(json.loads(self.client.keyboards[-1])["inline"])

    def test_single_message_replaces_multipart_and_delivers_both_keyboards(self):
        """Сократившийся экран получает новый cmid и содержательное меню."""
        self.owner.reply = self.choice(long=True)
        old = self.processor(replace(self.event, text="/choose"))
        self.assertTrue(self.deliver(old, limit=45))
        previous = [event for event in self.client.events if event[0] == "send"][-1][1]
        self.owner.available = (ReplyAction("Новая операция", "/choose next", scope=ActionScope.SECTION),)
        self.owner.reply = replace(self.choice(), view=ResponseView(
            "choice", update=True, unavailable=text_content("Повтори /choose."),
            cleanup_previous=True, allow_multiple_messages=True,
        ))
        updated = self.processor(replace(self.event, text="/choose"))
        self.assertTrue(updated.view_delivery.target_has_multiple_messages)
        self.assertTrue(self.deliver(updated))
        self.assertTrue(json.loads(self.client.keyboards[-2])["inline"])
        self.assertFalse(json.loads(self.client.keyboards[-1])["inline"])
        self.assertIn("Новая операция", self.client.messages[-1][1])
        self.assertIn((1001, previous), self.client.cleared)
        self.assertNotIn(self.event.reply_address, self.processor.menu_delivery_failures)

    def test_single_message_replacement_keeps_failed_bottom_publication_pending(self):
        """Успех нового inline-экрана не подтверждает неотправленное меню."""
        self.owner.reply = self.choice(long=True)
        self.assertTrue(self.deliver(self.processor(replace(self.event, text="/choose")), limit=45))
        self.owner.available = (ReplyAction("Новая операция", "/choose next", scope=ActionScope.SECTION),)
        self.owner.reply = replace(self.choice(), view=ResponseView(
            "choice", update=True, unavailable=text_content("Повтори /choose."),
            cleanup_previous=True, allow_multiple_messages=True,
        ))
        updated = self.processor(replace(self.event, text="/choose"))
        self.client.fail_menu = True
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertTrue(self.deliver(updated))
        self.assertTrue(json.loads(self.client.keyboards[-1])["inline"])
        self.assertIn(self.event.reply_address, self.processor.menu_delivery_failures)


if __name__ == "__main__":
    unittest.main()
