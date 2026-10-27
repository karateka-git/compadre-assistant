"""Проверяет пошаговый ввод общих команд без предметных возможностей."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.domain.users.identity import InvalidDisplayNameError, InvalidLinkCodeError, IdentityAlreadyBoundError
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from tests.capability_preference_helpers import MemoryCapabilityPreferences
from tests.test_incoming_boundary import FakeCapability, FakeIdentity, MemoryChatModes, _event
from tests.response_assertions import response_text


class CorePendingInputTest(unittest.TestCase):
    """Проверяет жизненный цикл и повторный допуск общего ввода."""

    def setUp(self):
        """Создаёт изолированный чат с сохранённым режимом."""
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.store = PendingInputStore(lambda: self.now)
        self.identity = FakeIdentity()
        self.identity.update_display_name = Mock(return_value=AppUser(23, "Новое"))
        self.identity.link = Mock(return_value=AppUser(42, "Связанный"))
        self.identity.request_rebind_confirmation = Mock(return_value=SimpleNamespace(id=4, version=2))
        self.capability = FakeCapability()
        self.preferences = MemoryCapabilityPreferences()
        self.modes = MemoryChatModes()
        self.key = ChatModeKey(23, "telegram", "chat-1")
        self.modes.save(ChatMode(self.key, "selected"))
        self.dispatcher = IncomingMessageDispatcher(
            registry=CapabilityRegistry((self.capability,)), user_identity_service=self.identity,
            chat_mode_repository=self.modes, capability_preference_repository=self.preferences,
            pending_inputs=self.store,
        )

    def send(self, text, chat="chat-1"):
        """Отправляет событие и возвращает текст ответа."""
        return response_text(self.dispatcher.execute(_event(text, chat)))

    def pending(self, chat="chat-1"):
        """Возвращает текущее ожидание выбранного чата."""
        return self.store.get(_event("x", chat).reply_address)

    def test_name_retry_deadline_and_success_preserve_mode(self):
        """Неверное имя сохраняет срок, успешное завершает ввод поверх режима."""
        self.send("/name")
        original = self.pending()
        self.now += timedelta(minutes=9)
        self.identity.update_display_name.side_effect = InvalidDisplayNameError()
        self.assertIn("/cancel", self.send("https://invalid.test"))
        self.assertEqual(self.pending(), original)
        self.identity.update_display_name.side_effect = None
        self.send("Новое")
        self.assertIsNone(self.pending())
        self.identity.update_display_name.assert_called_with(app_user_id=23, display_name="Новое")
        self.assertEqual(self.modes.get(self.key).capability_id, "selected")
        self.assertFalse(self.capability.calls)

    def test_cancel_expiry_and_other_chat_do_not_apply_name(self):
        """Отмена и TTL не применяют устаревшее значение, соседний чат изолирован."""
        self.send("/name")
        self.send("обычный текст", "other")
        self.identity.update_display_name.assert_not_called()
        self.send("/name", "other")
        self.send("/cancel")
        self.assertIsNone(self.pending())
        self.assertIsNotNone(self.pending("other"))
        self.now += timedelta(minutes=10)
        self.assertIn("истёк", self.send("Старое", "other"))
        self.identity.update_display_name.assert_not_called()

    def test_direct_commands_replace_pending(self):
        """Прямые формы сохраняют действие и закрывают прежнее ожидание."""
        for command in ("/name Имя", "/link code", "/disable selected", "/enable selected"):
            self.send("/name")
            self.send(command)
            self.assertIsNone(self.pending())
        self.identity.update_display_name.assert_called_once()
        self.identity.link.assert_called_once()
        self.assertTrue(self.preferences.rows[(23, "selected")])

    def test_link_unknown_known_retry_and_conflict(self):
        """Код принимается до и после регистрации, конфликт создаёт адресное действие."""
        for user in (None, AppUser(23, "Тест")):
            self.identity.user = user
            self.send("/link")
            original = self.pending()
            self.identity.link.side_effect = InvalidLinkCodeError()
            self.assertIn("/cancel", self.send("wrong"))
            self.assertEqual(self.pending(), original)
            self.identity.link.side_effect = None
            self.send("code")
            self.assertIsNone(self.pending())
        self.send("/link")
        self.identity.link.side_effect = IdentityAlreadyBoundError()
        response = self.dispatcher.execute(_event("code"))
        self.assertIsNone(self.pending())
        self.assertEqual(response.actions[0].command, "/confirm core identity 4 2 yes")
        self.assertEqual(response.actions[1].command, "/confirm core identity 4 2 no")

    def test_changed_identity_never_receives_old_value(self):
        """Смена профиля между запросом и ответом отменяет старое действие."""
        for command in ("/name", "/link", "/disable"):
            self.identity.user = AppUser(23, "Первый")
            self.send(command)
            self.identity.user = AppUser(42, "Другой")
            self.assertIn("Профиль канала изменился", self.send("selected"))
            self.assertIsNone(self.pending())
        self.identity.link.assert_not_called()
        self.identity.update_display_name.assert_not_called()
        self.assertFalse(self.preferences.rows)

    def test_capability_catalog_retry_and_personal_recheck(self):
        """Пустой выбор не создаёт ввод, неверный ID повторяется, личный выбор перечитывается."""
        self.assertIn("Нет возможностей", self.send("/enable"))
        self.assertIsNone(self.pending())
        self.send("/disable")
        original = self.pending()
        for value in ("unknown", "selected extra"):
            self.assertIn("/cancel", self.send(value))
            self.assertEqual(self.pending(), original)
        self.preferences.rows[(23, "selected")] = False
        self.assertIn("уже выключена", self.send("selected"))
        self.assertIsNone(self.pending())
        self.assertIn("Нет включённых", self.send("/disable"))
        self.assertIsNone(self.pending())
        self.send("/enable")
        self.send("selected")
        self.assertTrue(self.preferences.rows[(23, "selected")])
        self.assertEqual(self.modes.get(self.key).capability_id, "selected")

    def test_external_failures_clear_input(self):
        """Неизвестный исход записи не допускает автоматического повтора."""
        for command, method in (("/name", self.identity.update_display_name), ("/link", self.identity.link)):
            self.send(command)
            method.side_effect = OSError("unknown result")
            with self.assertLogs(level="ERROR"):
                self.send("value")
            self.assertIsNone(self.pending())
        self.send("/disable")
        self.preferences.upsert = Mock(side_effect=OSError("unknown result"))
        with self.assertLogs(level="ERROR"):
            self.send("selected")
        self.assertIsNone(self.pending())

    def test_pending_creation_and_cancel_are_serialized(self):
        """Отмена не обгоняет незавершённое создание ожидания в том же чате."""
        started, release = Event(), Event()
        original = self.identity.resolve
        def delayed(identity):
            started.set()
            self.assertTrue(release.wait(3))
            return original(identity)
        self.identity.resolve = delayed
        with ThreadPoolExecutor(max_workers=2) as pool:
            create = pool.submit(self.send, "/name")
            self.assertTrue(started.wait(3))
            cancel = pool.submit(self.send, "/cancel")
            self.assertFalse(cancel.done())
            release.set()
            create.result(timeout=3)
            self.assertIn("отменён", cancel.result(timeout=3))
        self.assertIsNone(self.pending())


    def test_direct_malformed_arguments_do_not_start_input(self):
        """Несколько ID отклоняются и не создают пошаговый ввод."""
        for command in ("/enable selected extra", "/disable selected extra"):
            self.assertIn("Укажи один ID", self.send(command))
            self.assertIsNone(self.pending())
            self.assertFalse(self.preferences.rows)

    def test_server_access_is_rechecked_after_prompt(self):
        """Отключённый сервером вход не включается из ранее показанного выбора."""
        self.preferences.rows[(23, "selected")] = False
        self.send("/enable")
        original = self.pending()
        self.dispatcher._registry = CapabilityRegistry((), (self.capability.descriptor,))
        self.assertIn("недоступна на сервере", self.send("selected"))
        self.assertFalse(self.preferences.rows[(23, "selected")])
        self.assertEqual(self.pending(), original)

    def test_information_commands_preserve_core_input(self):
        """Информационные команды не заменяют ожидание имени."""
        self.send("/name")
        original = self.pending()
        for command in ("/help", "/status", "/capabilities", "/start"):
            self.send(command)
            self.assertEqual(self.pending(), original)
        self.send("/unknown")
        self.assertIsNone(self.pending())
        self.identity.update_display_name.assert_not_called()
