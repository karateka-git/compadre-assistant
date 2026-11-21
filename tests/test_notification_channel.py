"""Проверяет выбор мессенджера на временной SQLite с действующими миграциями."""

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compadre_assistant.bootstrap import create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations
from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository
from compadre_assistant.core.domain.users.entities import IncomingIdentity, NotificationChannel


class NotificationChannelTest(unittest.TestCase):
    """Проверяет перенос существующих данных и все операции хранилища профилей."""

    def setUp(self) -> None:
        """Открывает изолированную БД до применения выбранной тестом схемы."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.db = self.enterContext(connect_database(self.root / "app.db"))
        self.users = SQLiteAppUserRepository(self.db)

    def test_upgrade_uses_only_unambiguous_channels(self) -> None:
        """Сохраняет привязки и исключает догадки по времени и порядку регистрации."""
        previous = self.root / "previous"
        previous.mkdir()
        for path in MIGRATIONS_DIR.glob("000[1-7]_*.sql"):
            (previous / path.name).write_bytes(path.read_bytes())
        apply_migrations(self.db, previous)
        with self.db:
            self.db.execute("INSERT INTO app_users (id) VALUES (1),(2),(3),(4),(5)")
            self.db.executemany(
                """INSERT INTO external_identities (app_user_id, channel, external_user_id,
                   external_chat_id, created_at) VALUES (?, ?, ?, '1', ?)""",
                [(1, "vk", "1", "2000"), (2, "vk", "2", "2001"),
                 (2, "telegram", "3", "2001"), (3, "cli", "4", "2000"),
                 (5, "telegram", "5", "1999"), (5, "vk", "5", "2002")],
            )
        before = self.db.execute("SELECT * FROM external_identities ORDER BY id").fetchall()
        apply_migrations(self.db)
        self.assertEqual([self.users.get_by_id(i).notification_channel for i in range(1, 6)],
                         [NotificationChannel.VK, None, None, None, None])
        self.assertEqual(self.db.execute("SELECT * FROM external_identities ORDER BY id").fetchall(), before)
        self.assertEqual(apply_migrations(self.db), [])

    def test_future_channel_does_not_resolve_to_known_messenger(self) -> None:
        """Прочитанное значение будущей схемы сохраняет отдельное состояние UNKNOWN."""
        apply_migrations(self.db)
        user = self.users.create()
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("UPDATE app_users SET notification_channel = 'future' WHERE id = ?", (user.id,))
        self.assertIsNone(self.users.get_by_id(user.id).notification_channel)
        self.db.execute("PRAGMA ignore_check_constraints = ON")
        with self.db:
            self.db.execute("UPDATE app_users SET notification_channel = 'future' WHERE id = ?", (user.id,))
        self.assertEqual(self.users.get_by_id(user.id).notification_channel, NotificationChannel.UNKNOWN)

    def test_profile_operations_and_selection_ownership(self) -> None:
        """Проверяет создание, выбор, повтор, переименование, удаление и владельца."""
        apply_migrations(self.db)
        service = create_user_identity_service(self.db)
        tg = IncomingIdentity("telegram", "1", "1")
        vk = IncomingIdentity("vk", "2", "2")
        owner = service.register_with_name(tg, display_name="Аня")
        other = self.users.create(display_name="Борис")
        self.assertEqual(owner.notification_channel, NotificationChannel.TELEGRAM)
        self.assertIsNone(other.notification_channel)
        self.assertFalse(service.select_notification_channel(owner.id, "vk"))
        self.assertFalse(service.select_notification_channel(other.id, "telegram"))
        self.assertFalse(service.select_notification_channel(owner.id, "future"))
        code = service.create_link_code(owner.id, target_channel="vk")
        self.assertEqual(service.link(code=code.code, identity=vk).notification_channel, NotificationChannel.TELEGRAM)
        self.assertTrue(service.select_notification_channel(owner.id, "vk"))
        self.assertTrue(service.select_notification_channel(owner.id, "vk"))
        self.assertEqual(service.register_with_name(tg, display_name="Борис").notification_channel,
                         NotificationChannel.VK)
        renamed = self.users.update_display_name(app_user_id=owner.id, display_name="Анна")
        self.assertEqual((renamed.display_name, renamed.notification_channel), ("Анна", NotificationChannel.VK))
        self.assertIsNone(self.users.get_by_id(other.id).notification_channel)
        self.users.delete(owner.id)
        self.assertIsNone(self.users.get_by_id(owner.id))
        self.assertFalse(self.users.select_notification_channel(owner.id, "vk"))

    def test_transfer_reassigns_selected_channel_atomically(self) -> None:
        """Откат переноса сохраняет оба выбора, успех переводит отправку в оставшийся канал."""
        apply_migrations(self.db)
        service = create_user_identity_service(self.db)
        first = IncomingIdentity("telegram", "1", "1")
        second = IncomingIdentity("telegram", "2", "2")
        mobile = IncomingIdentity("vk", "3", "3")
        source = service.register_with_name(first, display_name="А")
        target = service.register_with_name(second, display_name="Б")
        service.link(code=service.create_link_code(source.id, target_channel="vk").code, identity=mobile)
        self.assertTrue(service.select_notification_channel(source.id, "vk"))
        code = service.create_link_code(target.id, target_channel="vk")
        pending = service.request_rebind_confirmation(code=code.code, identity=mobile)
        self.db.execute("""CREATE TRIGGER reject_source_change BEFORE UPDATE OF notification_channel ON app_users
                           WHEN OLD.notification_channel = 'vk' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            service.confirm_rebind(mobile, confirmation_id=pending.id, version=pending.version)
        self.assertEqual(service.resolve_without_touch(mobile), self.users.get_by_id(source.id))
        self.assertEqual(self.users.get_by_id(source.id).notification_channel, NotificationChannel.VK)
        self.assertEqual(self.users.get_by_id(target.id).notification_channel, NotificationChannel.TELEGRAM)
        self.db.execute("DROP TRIGGER reject_source_change")
        moved = service.confirm_rebind(mobile, confirmation_id=pending.id, version=pending.version)
        self.assertEqual(moved.notification_channel, NotificationChannel.TELEGRAM)
        self.assertEqual(self.users.get_by_id(source.id).notification_channel, NotificationChannel.TELEGRAM)
        self.assertFalse(service.select_notification_channel(source.id, "vk"))
