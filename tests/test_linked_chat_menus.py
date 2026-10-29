"""Проверки меню связанных чатов на настоящих identity и SQLite."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from compadre_assistant.bootstrap import create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteExternalIdentityRepository
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from tests import test_contextual_help as help_tests
from tests.response_assertions import response_text


class LinkedChatMenusTest(unittest.TestCase):
    """Проверяет общие настройки при независимых режимах и адресах."""

    def setUp(self):
        """Создаёт два связанных канала и отдельный чужой профиль."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.connection = self.enterContext(connect_database(Path(directory.name) / "test.db"))
        apply_migrations(self.connection)
        self.service = create_user_identity_service(self.connection)
        self.tg = IncomingIdentity("telegram", "101", "101")
        self.vk = IncomingIdentity("vk", "202", "202")
        self.foreign = IncomingIdentity("telegram", "303", "303")
        self.user = self.service.register_with_name(self.tg, display_name="Первый")
        self.other = self.service.register_with_name(self.foreign, display_name="Второй")
        self.service.link(code=self.service.create_link_code(self.user.id).code, identity=self.vk)
        self.fixture = help_tests.ContextualHelpTest()
        self.fixture.setUp()
        self.fixture.identity = self.service
        self.dispatcher = self.fixture.build()
        self.updates = []

    def event(self, text, identity=None):
        """Создаёт событие конкретного сохранённого адреса."""
        identity = identity or self.tg
        return IncomingEvent(identity.channel, identity.external_chat_id, "1", text, identity.external_user_id)

    def send(self, text, identity=None, handler=None):
        """Исполняет команду через общий вход и собирает меню по адресам."""
        return self.dispatcher.execute(self.event(text, identity), menu_update_handler=handler or self.updates.append)

    def menus(self):
        """Возвращает команды последнего обновления каждого чата."""
        return {update.address.chat_id: tuple(item.command for item in update.menu.items) for update in self.updates}

    def test_preference_updates_other_mode_and_clears_only_invalid_pending(self):
        """Общий выбор пересчитывает разные режимы, сохраняя допустимый ввод."""
        key = ChatModeKey(self.user.id, "vk", "202")
        self.fixture.modes.save(ChatMode(key, "obsidian"))
        self.send("/ask", self.vk)
        self.send("/name", self.foreign)
        foreign_pending = self.fixture.store.get(self.event("x", self.foreign).reply_address)
        self.updates.clear()
        response = self.send("/disable ask")
        self.assertIn("выключена", response_text(response))
        menus = self.menus()
        self.assertEqual(set(menus), {"101", "202"})
        self.assertIn("/obsidian", menus["101"])
        self.assertIn("/reanalyze", menus["202"])
        self.assertNotIn("/ask", menus["202"])
        self.assertNotIn("/cancel", menus["202"])
        self.assertIsNone(self.fixture.store.get(self.event("x", self.vk).reply_address))
        self.assertEqual(self.fixture.store.get(foreign_pending.chat_key), foreign_pending)
        self.assertEqual(self.fixture.modes.get(key).capability_id, "obsidian")
        self.send("/name", self.vk)
        pending = self.fixture.store.get(self.event("x", self.vk).reply_address)
        self.updates.clear()
        self.send("/enable")
        self.send("ask")
        self.assertIn("/ask", self.menus()["202"])
        self.assertIn("/cancel", self.menus()["202"])
        self.assertEqual(self.fixture.store.get(pending.chat_key), pending)

    def test_link_and_rebind_refresh_both_owners_without_leaking_preferences(self):
        """Перенесённый чат получает новый профиль, оставшиеся — своего владельца."""
        fresh = IncomingIdentity("vk", "404", "404")
        self.send("/link " + self.service.create_link_code(self.user.id).code, fresh)
        self.assertEqual(set(self.menus()), {"101", "202", "404"})
        self.send("/disable ask", self.foreign)
        confirmation = self.service.request_rebind_confirmation(
            code=self.service.create_link_code(self.other.id).code, identity=self.vk)
        self.updates.clear()
        self.send(f"/confirm core identity {confirmation.id} {confirmation.version} yes", self.vk)
        menus = self.menus()
        self.assertEqual(set(menus), {"101", "202", "404", "303"})
        self.assertIn("/ask", menus["101"])
        self.assertIn("/ask", menus["404"])
        self.assertNotIn("/ask", menus["202"])
        self.assertNotIn("/ask", menus["303"])
        self.assertNotIn(self.vk, self.service.list_known_private_chats(self.user))

    def test_stale_enumeration_does_not_restore_or_publish_old_address(self):
        """Смена адреса между перечислением и проверкой не оживляет прежний чат."""
        original = self.service.list_known_private_chats
        moved = replace(self.vk, external_chat_id="505")
        calls = [0]
        def enumerate_chats(user):
            calls[0] += 1
            result = original(user)
            if calls[0] == 1:
                self.service.resolve(moved)
            return result
        with patch.object(self.service, "list_known_private_chats", side_effect=enumerate_chats):
            self.send("/disable ask")
        self.assertEqual(set(self.menus()), {"101"})
        self.assertNotIn(moved, original(self.user))
        self.assertNotIn(self.vk, original(self.user))

    def test_enumeration_rejects_blank_ambiguous_and_deleted_addresses(self):
        """Некорректные записи и чужие адреса не проходят публичный контракт."""
        repository = SQLiteExternalIdentityRepository(self.connection)
        with self.connection:
            self.connection.execute("UPDATE external_identities SET external_chat_id = '  ' WHERE channel = 'vk'")
        self.assertEqual(self.service.list_known_private_chats(self.user), (self.tg,))
        repository.create(app_user_id=self.other.id, identity=replace(self.tg, external_user_id="606"))
        self.assertEqual(self.service.list_known_private_chats(self.user), ())
        self.assertFalse(self.service.owns_known_private_chat(self.user, self.tg))
        self.assertFalse(self.service.owns_known_private_chat(self.other, self.vk))
        with self.connection:
            self.connection.execute("DELETE FROM app_users WHERE id = ?", (self.user.id,))
        self.assertEqual(self.service.list_known_private_chats(self.user), ())

    def test_rebind_during_menu_read_discards_previous_owner_menu(self):
        """Повторная проверка после чтения предотвращает выдачу чужого меню."""
        original = self.dispatcher._command_context
        repository = SQLiteExternalIdentityRepository(self.connection)
        def read_context(event, **kwargs):
            result = original(event, **kwargs)
            if event.chat_id == "202":
                repository.reassign(app_user_id=self.other.id, identity=self.vk)
            return result
        with patch.object(self.dispatcher, "_command_context", side_effect=read_context):
            self.send("/disable ask")
        self.assertEqual(set(self.menus()), {"101"})
        self.assertTrue(self.service.owns_known_private_chat(self.other, self.vk))

    def test_delivery_and_enumeration_errors_preserve_text_and_other_updates(self):
        """Сбой одного адреса не отменяет ответ, /help или следующий адрес."""
        def fail_current(update):
            if update.address.chat_id == "101":
                raise RuntimeError("transport")
            self.updates.append(update)
        with self.assertLogs("compadre_assistant.core.presentation.incoming.dispatcher", level="ERROR"):
            response = self.send("/disable ask", handler=fail_current)
        self.assertIn("выключена", response_text(response))
        self.assertEqual(set(self.menus()), {"202"})
        with patch.object(self.service, "list_known_private_chats", side_effect=RuntimeError("read")):
            with self.assertLogs("compadre_assistant.core.presentation.incoming.dispatcher", level="ERROR"):
                response = self.send("/enable ask")
        self.assertIn("включена", response_text(response))
        self.assertIn("/ask", response_text(self.send("/help")))
