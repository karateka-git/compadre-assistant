"""Проверки меню связанных чатов на настоящих identity и SQLite."""

from dataclasses import replace
from hashlib import sha256
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
        self.service.link(code=self.service.create_link_code(self.user.id, target_channel="vk").code, identity=self.vk)
        self.fixture = help_tests.ContextualHelpTest()
        self.fixture.setUp()
        self.fixture.identity = self.service
        self.dispatcher = self.fixture.build()
        self.updates = []

    def previous_code(self, owner):
        """Восстанавливает прежний общий код для нескольких identities одного канала."""
        code = "old-code"
        with self.connection as db:
            db.execute("INSERT INTO identity_link_tokens (app_user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                       (owner, sha256(code.encode()).hexdigest(), "2999-01-01"))
        return code

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
        self.send("/ai", self.vk)
        self.send("/ask", self.vk)
        self.send("/settings", self.foreign)
        self.send("/name", self.foreign)
        foreign_pending = self.fixture.store.get(self.event("x", self.foreign).reply_address)
        self.send("/settings")
        self.updates.clear()
        response = self.send("/disable ai")
        self.assertIn("выключена", response_text(response))
        menus = self.menus()
        self.assertEqual(set(menus), {"101", "202"})
        self.assertIn("/enable", menus["101"])
        self.assertIn("/exit", menus["202"])
        self.assertNotIn("/ask", menus["202"])
        self.assertNotIn("/cancel", menus["202"])
        self.assertIsNone(self.fixture.store.get(self.event("x", self.vk).reply_address))
        self.assertEqual(self.fixture.store.get(foreign_pending.chat_key), foreign_pending)
        self.assertEqual(self.fixture.modes.get(key).capability_id, "ai")
        self.updates.clear()
        self.send("/enable")
        self.send("ai")
        self.assertIn("/ask", self.menus()["202"])
        self.assertNotIn("/cancel", self.menus()["202"])
        self.assertEqual(self.fixture.store.get(foreign_pending.chat_key), foreign_pending)

    def test_link_and_rebind_refresh_both_owners_without_leaking_preferences(self):
        """Перенесённый чат получает новый профиль, оставшиеся — своего владельца."""
        fresh = IncomingIdentity("vk", "404", "404")
        self.send("/link " + self.previous_code(self.user.id), fresh)
        self.assertEqual(set(self.menus()), {"101", "202", "404"})
        self.send("/settings", self.foreign)
        self.send("/disable ai", self.foreign)
        self.send("/exit", self.foreign)
        confirmation = self.service.request_rebind_confirmation(
            code=self.service.create_link_code(self.other.id, target_channel="vk").code, identity=self.vk)
        self.updates.clear()
        self.send(f"/confirm core identity {confirmation.id} {confirmation.version} yes", self.vk)
        menus = self.menus()
        self.assertEqual(set(menus), {"101", "202", "404", "303"})
        self.assertIn("/ai", menus["101"])
        self.assertIn("/ai", menus["404"])
        self.assertNotIn("/ai", menus["202"])
        self.assertNotIn("/ai", menus["303"])
        self.assertNotIn(self.vk, self.service.list_known_private_chats(self.user))

    def test_stale_enumeration_does_not_restore_or_publish_old_address(self):
        """Явная перепривязка адреса после перечисления отменяет доставку в старый чат."""
        original = self.service.list_known_private_chats
        moved = replace(self.vk, external_chat_id="505")
        repository = SQLiteExternalIdentityRepository(self.connection)
        calls = [0]
        def enumerate_chats(user):
            calls[0] += 1
            result = original(user)
            if calls[0] == 1:
                repository.reassign(app_user_id=self.user.id, identity=moved)
            return result
        with patch.object(self.service, "list_known_private_chats", side_effect=enumerate_chats):
            self.send("/settings")
            self.send("/disable ai")
        self.assertEqual(set(self.menus()), {"101"})
        self.assertNotIn(moved, original(self.user))
        self.assertNotIn(self.vk, original(self.user))

    def test_blocked_rebind_reports_unchanged_profile_and_keeps_confirmation(self) -> None:
        """Общий вход объясняет отказ и сохраняет адресное ожидание в исходном чате."""
        with self.connection as db:
            family = db.execute(
                "INSERT INTO families (name, owner_app_user_id, created_at) VALUES ('Дом', ?, 'fixture')",
                (self.other.id,),
            ).lastrowid
            db.execute("INSERT INTO family_members VALUES (?, ?, 'fixture')", (family, self.other.id))
        pending = self.service.request_rebind_confirmation(
            code=self.previous_code(self.user.id), identity=self.foreign,
        )
        approve = f"/confirm core identity {pending.id} {pending.version} yes"

        response = self.send(approve, self.foreign)

        self.assertIsNone(response.error)
        self.assertEqual(
            response_text(response),
            "Канал остался в исходном профиле, потому что этот профиль ещё используется. "
            "Привяжи к нему другой канал, затем открой «Подтверждения», чтобы повторить перенос.",
        )
        self.assertIn(approve, [action.command for action in self.send("/confirm", self.foreign).actions])
        self.assertEqual(self.service.resolve_without_touch(self.foreign), self.other)

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
            self.send("/settings")
            self.send("/disable ai")
        self.assertEqual(set(self.menus()), {"101"})
        self.assertTrue(self.service.owns_known_private_chat(self.other, self.vk))

    def test_delivery_and_enumeration_errors_preserve_text_and_other_updates(self):
        """Сбой одного адреса не отменяет ответ, /help или следующий адрес."""
        def fail_current(update):
            if update.address.chat_id == "101":
                raise RuntimeError("transport")
            self.updates.append(update)
        self.send("/settings")
        self.updates.clear()
        self.send("/disable")
        self.updates.clear()
        with self.assertLogs("compadre_assistant.core.presentation.incoming.dispatcher", level="ERROR"):
            response = self.send("/disable ai", handler=fail_current)
        self.assertIn("выключена", response_text(response))
        self.assertEqual(set(self.menus()), {"202"})
        with patch.object(self.service, "list_known_private_chats", side_effect=RuntimeError("read")):
            with self.assertLogs("compadre_assistant.core.presentation.incoming.dispatcher", level="ERROR"):
                response = self.send("/enable ai")
        self.assertIn("«Режим ИИ» включён", response_text(response))
        self.send("/exit")
        self.assertIn("/ai", response_text(self.send("/help")))
