"""Проверяет управляемый ответ через реальный процессор и временную SQLite."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_selection import ResponseActionStore
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityHelp, CapabilityResult, HelpSection, IncomingResponse,
    ReplyAction, ResponseView,
)
from compadre_assistant.core.presentation.message_content import text_content
from tests.response_assertions import response_text
from tests.test_incoming_boundary import FakeCapability


RECOVERY = "Открой представление заново через /show_view."


class EditableCapability(FakeCapability):
    """Нейтральный обработчик публикует и меняет одно представление."""

    def __init__(self):
        """Объявляет ручные команды и наблюдаемые выборы."""
        super().__init__()
        commands = ("/show_view", "/adjust_view", "/choose_view", "/clear_view")
        self._descriptor = replace(self.descriptor, help=CapabilityHelp((
            HelpSection("Представление", tuple(CapabilityCommand(command, (), command) for command in commands)),
        )))
        self.choices = []
        self.before_reply = None

    def handle(self, context, event):
        """Выполняет одно действие и возвращает новый вид с оставшимися кнопками."""
        command, _, value = event.text.partition(" ")
        if command == "/choose_view":
            self.choices.append(value)
        if self.before_reply is not None:
            self.before_reply(event.text)
        remaining = () if command == "/clear_view" else tuple(
            ReplyAction(value, f"/choose_view {value}") for value in ("A", "B", "C")
            if value not in self.choices
        )
        return CapabilityResult(IncomingResponse(
            text_content(f"Состояние: {', '.join(self.choices)}. Открыть: /show_view."),
            suggested_actions=remaining,
            view=ResponseView("panel", update=command != "/show_view", unavailable=text_content(RECOVERY)),
        ))


class ResponseViewsTest(unittest.TestCase):
    """Проверяет подтверждение доставки, контекст и поколения редактирования."""

    def setUp(self):
        """Выбирает нейтральный режим в двух известных личных чатах."""
        root = Path(self.enterContext(TemporaryDirectory(prefix="response-views-")))
        self.path = root / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            for chat_id in ("1001", "1002"):
                service.register_with_name(IncomingIdentity("telegram", chat_id, chat_id), display_name="Тест")
        self.capability = EditableCapability()
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability,))
        self.send("/selected")
        self.send("/selected", "1002")

    def send(self, command, chat="1001"):
        """Передаёт ручную команду без ID исходной кнопки."""
        return self.processor(IncomingEvent("telegram", chat, "incoming", command, chat))

    def select(self, action, source="42", chat="1001"):
        """Передаёт ID фактического кнопочного сообщения отдельно от события."""
        event = IncomingEvent("telegram", chat, "callback-id", "/choose_view Подмена", chat,
                              callback_message_id=source)
        return self.processor.select_action(event, action.token)

    def deliver(self, response, message_id="42"):
        """Подтверждает один сетевой успех от имени нейтрального адаптера."""
        operation = response.view_delivery
        self.assertIsNotNone(operation)
        self.assertTrue(operation.start())
        operation.finish(message_id)
        self.assertFalse(operation.start())

    def test_two_choices_update_one_confirmed_message_and_replace_tokens(self):
        """Старое поколение и другой адрес оставляют свежую клавиатуру."""
        opening = self.send("/show_view")
        first, old_sibling, _ = opening.bound_actions
        self.assertIsNone(opening.view_delivery.target_message_id)
        opening.view_delivery.finish("42")  # Без допуска это не подтверждение.
        self.assertIn("больше недоступна", response_text(self.select(first)))
        self.assertTrue(self.select(first).preserve_source_actions)
        self.deliver(opening)
        self.assertIn("больше недоступна", response_text(self.select(first, source="callback-id")))
        self.assertIn("больше недоступна", response_text(self.select(first, chat="1002")))
        self.assertEqual(self.capability.choices, [])

        changed = self.select(first)
        self.assertEqual(changed.view_delivery.target_message_id, "42")
        self.assertTrue(changed.preserve_source_actions)
        self.assertEqual(self.capability.choices, ["A"])
        self.assertEqual([button.label for button in changed.bound_actions], ["B", "C"])
        self.deliver(changed)
        replay = self.select(old_sibling)
        self.assertIn("больше недоступна", response_text(replay))
        self.assertTrue(replay.preserve_source_actions)

        changed = self.select(changed.bound_actions[0])
        self.assertEqual(changed.view_delivery.target_message_id, "42")
        self.deliver(changed)
        self.assertEqual(self.capability.choices, ["A", "B"])
        self.assertEqual([button.label for button in changed.bound_actions], ["C"])

    def test_manual_update_uses_last_delivered_view_and_respects_mode_revision(self):
        """Отсутствующая доставка или новый вход дают штатную подсказку."""
        self.assertEqual(response_text(self.send("/adjust_view")), RECOVERY)
        opening = self.send("/show_view")
        self.assertEqual(response_text(self.send("/adjust_view")), RECOVERY)
        self.deliver(opening)
        self.assertEqual(response_text(self.send("/adjust_view", "1002")), RECOVERY)
        changed = self.send("/clear_view")
        self.assertEqual(changed.view_delivery.target_message_id, "42")
        self.assertEqual(changed.bound_actions, ())
        self.deliver(changed)
        self.send("/selected")
        self.assertEqual(response_text(self.send("/adjust_view")), RECOVERY)
        stale = self.select(opening.bound_actions[0])
        self.assertTrue(stale.preserve_source_actions)
        self.assertIn("больше недоступна", response_text(stale))

    def test_superseded_attempt_and_failed_edit_do_not_activate_tokens(self):
        """Поздняя доставка не перезаписывает новую, а сбой закрывает цель."""
        opening = self.send("/show_view")
        self.deliver(opening)
        older = self.send("/adjust_view").view_delivery
        latest = self.send("/adjust_view")
        operation = latest.view_delivery
        self.assertFalse(older.start())
        self.assertTrue(operation.start())
        self.assertEqual(response_text(self.send("/adjust_view")), RECOVERY)
        operation.finish(None)
        older.finish("42")
        self.assertFalse(operation.start())
        self.assertIn("больше недоступна", response_text(self.select(latest.bound_actions[0])))
        self.assertEqual(response_text(self.send("/adjust_view")), RECOVERY)
        self.assertEqual(self.capability.choices, [])

    def test_undelivered_reopen_does_not_replace_last_message(self):
        """Публикация становится целью только после успешной доставки."""
        self.deliver(self.send("/show_view"))
        pending = self.send("/show_view")
        adjusted = self.send("/adjust_view")
        self.assertEqual(adjusted.view_delivery.target_message_id, "42")
        self.assertFalse(pending.view_delivery.start())
        self.deliver(adjusted)
        self.deliver(self.send("/show_view"), "43")
        self.assertEqual(self.send("/adjust_view").view_delivery.target_message_id, "43")

    def test_slow_earlier_command_cannot_replace_a_newer_delivered_view(self):
        """Поколение выбирается до обработки и учитывает порядок событий."""
        self.deliver(self.send("/show_view"))
        entered, release = Event(), Event()

        def delay(command):
            if command == "/adjust_view slow":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("Не завершилась проверка параллельного ответа")

        self.capability.before_reply = delay
        with ThreadPoolExecutor(max_workers=1) as executor:
            earlier = executor.submit(self.send, "/adjust_view slow")
            try:
                self.assertTrue(entered.wait(3))
                latest = self.send("/adjust_view")
                self.deliver(latest)
            finally:
                release.set()
            self.assertEqual(response_text(earlier.result(timeout=3)), RECOVERY)
        self.assertIsNotNone(self.select(latest.bound_actions[0]).view_delivery)
        self.assertEqual(self.capability.choices, ["A"])

    def test_restart_and_expired_view_require_reopen(self):
        """Процессный реестр имеет ограниченный срок и не восстанавливает ID из SQLite."""
        now = [0.0]
        self.processor._response_actions = ResponseActionStore(ttl_seconds=10, clock=lambda: now[0])
        opening = self.send("/show_view")
        self.deliver(opening)
        now[0] = 10
        self.assertIn("больше недоступна", response_text(self.select(opening.bound_actions[0])))
        self.assertEqual(response_text(self.send("/adjust_view")), RECOVERY)
        self.deliver(self.send("/show_view"), "43")
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability,))
        self.assertEqual(response_text(self.send("/adjust_view")), RECOVERY)
