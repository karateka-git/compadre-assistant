"""Проверки безопасного выбора кнопок без SDK Telegram/VK."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_selection import ResponseActionStore
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    ConfirmationAction, ConfirmationQueryResult, IncomingResponse, PendingConfirmation, ReplyAction,
)
from compadre_assistant.core.presentation.message_content import text_content
from tests.test_incoming_boundary import FakeCapability
from tests.response_assertions import response_text


class ResponseActionSelectionTest(unittest.TestCase):
    """Проверяет реальную SQLite identity, режим и входящий маршрут."""

    def setUp(self):
        directory = TemporaryDirectory(prefix="response-actions-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            self.user = service.register_with_name(IncomingIdentity("telegram", "1001", "1001"), display_name="Первый")
        self.capability = FakeCapability()
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability,))
        self.event = IncomingEvent("telegram", "1001", "callback", "не команда", "1001")

    def issue(self, command="/name Выбрано", label="Подпись", response=None):
        """Эмулирует явно заданные владельцем действия конкретного ответа."""
        response = response or IncomingResponse(text_content(command), suggested_actions=(ReplyAction(label, command),))
        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            return self.processor(self.event)

    def name(self):
        """Читает имя без обновления identity."""
        with connect_database(self.path) as connection:
            return create_user_identity_service(connection).find_known_private_chat("telegram", "1001")[0].display_name

    def test_exact_command_and_opaque_token_ignore_label_and_callback_text(self):
        response = self.issue(label="/name Взлом")
        button = response.bound_actions[0]
        self.assertEqual(len(button.token), 32)
        self.assertNotIn("name", button.token)
        self.processor.select_action(replace(self.event, text="/name Подмена"), button.token)
        self.assertEqual(self.name(), "Выбрано")

    def test_foreign_chat_user_channel_and_missing_sender_cannot_consume_token(self):
        button = self.issue().bound_actions[0]
        for event in (replace(self.event, chat_id="2002"), replace(self.event, external_user_id="2002"),
                      replace(self.event, channel="vk"), replace(self.event, external_user_id=None)):
            self.assertIn("больше недоступна", response_text(self.processor.select_action(event, button.token)))
            self.assertEqual(self.name(), "Первый")
        self.processor.select_action(self.event, button.token)
        self.assertEqual(self.name(), "Выбрано")

    def test_invalid_tokens_never_execute_event_text(self):
        for token in (None, "", "/name Взлом", "x" * 32, "x" * 10000):
            self.processor.select_action(replace(self.event, text="/name Взлом"), token)
        self.assertEqual(self.name(), "Первый")

    def test_repeat_concurrent_clicks_execute_once(self):
        button = self.issue().bound_actions[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.processor.select_action(self.event, button.token), range(16)))
        self.assertEqual(sum("больше недоступна" not in response_text(item) for item in responses), 1)
        self.assertEqual(self.name(), "Выбрано")

    def test_enable_disable_choices_are_bound_to_the_current_pending_input(self):
        """Кнопка выбора выполняет точную команду и не действует повторно."""
        disable = self.processor(replace(self.event, text="/disable"))
        self.assertEqual(tuple(action.command for action in disable.suggested_actions),
                         ("/disable selected",))
        self.assertEqual(len(disable.bound_actions), 1)
        button = disable.bound_actions[0]
        self.processor.select_action(self.event, button.token)
        self.assertIn("больше недоступна", response_text(self.processor.select_action(self.event, button.token)))

        enable = self.processor(replace(self.event, text="/enable"))
        self.assertEqual(tuple(action.command for action in enable.suggested_actions),
                         ("/enable selected",))
        self.assertEqual(len(enable.bound_actions), 1)
        self.processor.select_action(self.event, enable.bound_actions[0].token)
        self.assertIn("/selected", response_text(self.processor(replace(self.event, text="/help"))))

    def test_sibling_action_is_consumed_but_other_response_survives(self):
        response = IncomingResponse(text_content("Выбор"), suggested_actions=(
            ReplyAction("Один", "/name Один"), ReplyAction("Два", "/name Два")))
        first = self.issue(response=response)
        second = self.issue()
        self.assertEqual(first.bound_actions[0].response_id, first.bound_actions[1].response_id)
        self.assertNotEqual(first.bound_actions[0].response_id, second.bound_actions[0].response_id)
        self.processor.select_action(self.event, first.bound_actions[0].token)
        self.processor.select_action(self.event, first.bound_actions[1].token)
        self.assertEqual(self.name(), "Один")
        self.processor.select_action(self.event, second.bound_actions[0].token)
        self.assertEqual(self.name(), "Выбрано")

    def test_restart_expiry_and_eviction_fail_closed(self):
        now = [0.0]
        self.processor._response_actions = ResponseActionStore(ttl_seconds=10, max_entries=1, clock=lambda: now[0])
        evicted = self.issue().bound_actions[0]
        current = self.issue().bound_actions[0]
        self.processor.select_action(self.event, evicted.token)
        now[0] = 10
        self.processor.select_action(self.event, current.token)
        fresh = self.issue().bound_actions[0]
        restarted = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability,))
        restarted.select_action(self.event, fresh.token)
        self.assertEqual(self.name(), "Первый")

    def test_mode_round_trip_and_access_change_reject_old_button(self):
        button = self.issue().bound_actions[0]
        self.processor(replace(self.event, text="/selected"))
        self.processor(replace(self.event, text="/exit"))
        self.processor.select_action(self.event, button.token)
        self.assertEqual(self.name(), "Первый")
        button = self.issue().bound_actions[0]
        self.processor(replace(self.event, text="/disable selected"))
        self.processor.select_action(self.event, button.token)
        self.assertEqual(self.name(), "Первый")

    def test_owner_change_rejects_old_button(self):
        button = self.issue().bound_actions[0]
        with connect_database(self.path) as connection:
            service = create_user_identity_service(connection)
            other = service.register_with_name(IncomingIdentity("vk", "2002", "2002"), display_name="Другой")
            connection.execute("UPDATE external_identities SET app_user_id = ? WHERE channel = 'telegram'", (other.id,))
            connection.commit()
        self.processor.select_action(self.event, button.token)
        self.assertEqual(self.name(), "Другой")

    def test_cancel_cannot_cancel_replaced_pending_input(self):
        self.processor(replace(self.event, text="/name"))
        button = self.issue(command="/cancel").bound_actions[0]
        self.processor(replace(self.event, text="/link"))
        pending = self.processor._pending_inputs.get(self.event.reply_address)
        self.processor.select_action(self.event, button.token)
        self.assertEqual(self.processor._pending_inputs.get(self.event.reply_address), pending)

    def confirmation(self):
        """Создаёт публичный снимок адресного ожидания."""
        action = ConfirmationAction("core:identity:1:1:yes", "Перенести", "/confirm core identity 1 1 yes")
        pending = PendingConfirmation("core", "identity", 1, 1, self.event.reply_address, (action,))
        return action, pending

    def test_confirmation_requires_exact_live_owner_operation_id_version_state(self):
        action, pending = self.confirmation()
        for changed in (None, replace(pending, owner_id="other"), replace(pending, operation="other"),
                        replace(pending, confirmation_id=2), replace(pending, version=2),
                        replace(pending, actions=())):
            with self.subTest(changed=changed):
                with patch.object(IncomingMessageDispatcher, "_action_confirmations",
                                  return_value=ConfirmationQueryResult("complete", (pending,))):
                    button = self.issue(response=IncomingResponse(text_content("Ожидание"), actions=(action,))).bound_actions[0]
                with patch.object(IncomingMessageDispatcher, "_action_confirmations",
                                  return_value=ConfirmationQueryResult("complete", (changed,) if changed else ())):
                    with patch.object(IncomingMessageDispatcher, "execute") as execute:
                        self.processor.select_action(self.event, button.token)
                        execute.assert_not_called()

    def test_confirmation_routes_exact_command_and_invalidates_other_response(self):
        action, pending = self.confirmation()
        with patch.object(IncomingMessageDispatcher, "_action_confirmations",
                          return_value=ConfirmationQueryResult("complete", (pending,))):
            response = IncomingResponse(text_content("Выбор"), actions=(action,))
            first, second = self.issue(response=response), self.issue(response=response)
            with patch.object(IncomingMessageDispatcher, "execute", return_value=IncomingResponse(text_content("OK"))) as execute:
                self.processor.select_action(self.event, first.bound_actions[0].token)
                self.processor.select_action(self.event, second.bound_actions[0].token)
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[0].text, action.command)

    def test_partial_snapshot_cannot_issue_or_execute_confirmation(self):
        action, pending = self.confirmation()
        response = IncomingResponse(text_content("Ожидание"), actions=(action,))
        with patch.object(IncomingMessageDispatcher, "_action_confirmations",
                          return_value=ConfirmationQueryResult("complete", (pending,))):
            button = self.issue(response=response).bound_actions[0]
        with patch.object(IncomingMessageDispatcher, "_action_confirmations",
                          return_value=ConfirmationQueryResult("unavailable", (pending,))):
            self.assertFalse(self.issue(response=response).bound_actions)
            with patch.object(IncomingMessageDispatcher, "execute") as execute:
                self.processor.select_action(self.event, button.token)
                execute.assert_not_called()

    def test_confirmation_label_may_differ_but_id_and_command_must_match(self):
        """Уточнение подписи допустимо, подмена ID или команды не выдаёт token."""
        action, pending = self.confirmation()
        with patch.object(IncomingMessageDispatcher, "_action_confirmations",
                          return_value=ConfirmationQueryResult("complete", (pending,))):
            for changed in (replace(action, action_id="other"),
                            replace(action, command="/confirm core identity 2 1 yes")):
                self.assertFalse(self.issue(response=IncomingResponse(
                    text_content("Выбор"), actions=(changed,))).bound_actions)
            response = self.issue(response=IncomingResponse(text_content("Выбор"),
                actions=(replace(action, label="Короткая подпись"),)))
            with patch.object(IncomingMessageDispatcher, "execute",
                              return_value=IncomingResponse(text_content("OK"))) as execute:
                self.processor.select_action(self.event, response.bound_actions[0].token)
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[0].text, action.command)

    def test_no_confirmation_bypass_or_unknown_command_as_suggestion(self):
        action, _ = self.confirmation()
        self.assertFalse(self.issue(command=action.command).bound_actions)
        self.assertFalse(self.issue(command="/unknown").bound_actions)
        self.assertFalse(self.issue(response=IncomingResponse(text_content("Нет"), actions=(action,))).bound_actions)

    def test_background_actions_keep_original_identity_and_reject_changed_context(self):
        self.processor(replace(self.event, text="/selected"))
        delivered = []
        self.processor(self.event, delivered.append)
        response = IncomingResponse(text_content("Имя"), suggested_actions=(ReplyAction("Имя", "/name Фон"),))
        self.capability.delivery.send(self.event.reply_address, response)
        self.assertTrue(delivered[-1].response.bound_actions)
        button = delivered[-1].response.bound_actions[0]
        self.processor.select_action(self.event, button.token)
        self.assertEqual(self.name(), "Фон")
        self.processor(replace(self.event, text="/exit"))
        self.capability.delivery.send(self.event.reply_address, response)
        self.assertFalse(delivered[-1].response.bound_actions)


if __name__ == "__main__":
    unittest.main()
