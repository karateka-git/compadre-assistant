"""Восстановление и повторная публикация меню без входящей команды."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch
import unittest

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.domain.users.identity import UserIdentityService
from compadre_assistant.core.presentation.incoming.ports import (
    ConfirmationAction, ConfirmationQueryResult, PendingConfirmation,
)
from tests.test_incoming_boundary import FakeCapability


class MenuReconciliationTest(unittest.TestCase):
    """Проверяет процессную сверку на сохранённых личных адресах."""

    def setUp(self):
        directory = TemporaryDirectory(prefix="menu-reconcile-")
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "app.db"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            self.identity = IncomingIdentity("telegram", "1001", "1001")
            self.user = service.register_with_name(self.identity, display_name="Первый")
        self.processor = ChannelMessageProcessor(database_path=self.database_path, capabilities=())
        self.updates = []

    def event(self, text):
        """Создаёт событие сохранённого личного чата."""
        return IncomingEvent("telegram", "1001", "msg", text, "1001")

    def commands(self):
        """Возвращает команды последнего доставленного меню."""
        return tuple(item.command for item in self.updates[-1].menu.items)

    def test_startup_recovery_and_unchanged_dedup(self):
        """Новый процесс восстанавливает меню и не повторяет тот же снимок."""
        self.processor.reconcile_menus(self.updates.append)
        self.assertEqual(len(self.updates), 1)
        self.assertIn("/status", self.commands())
        self.processor.reconcile_menus(self.updates.append)
        self.assertEqual(len(self.updates), 1)
        restarted = ChannelMessageProcessor(database_path=self.database_path, capabilities=())
        restarted.reconcile_menus(self.updates.append)
        self.assertEqual(len(self.updates), 2)

    def test_periodic_pass_enumerates_globally_once(self):
        """Проверки отдельных адресов не повторяют полное перечисление БД."""
        original = UserIdentityService.list_all_known_private_chats
        with patch.object(UserIdentityService, "list_all_known_private_chats",
                          side_effect=original, autospec=True) as listing:
            self.processor.reconcile_menus(self.updates.append)
        self.assertEqual(listing.call_count, 1)

    def test_reconciliation_does_not_touch_saved_identity(self):
        """Фоновое чтение адреса не стирает имя и не обновляет last_seen."""
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE external_identities SET username = 'saved', last_seen_at = '2020-01-01' "
                "WHERE channel = 'telegram' AND external_user_id = '1001'"
            )
            connection.commit()
        self.processor.reconcile_menus(self.updates.append)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT username, last_seen_at FROM external_identities "
                "WHERE channel = 'telegram' AND external_user_id = '1001'"
            ).fetchone()
        self.assertEqual(tuple(row), ("saved", "2020-01-01"))

    def test_server_availability_changes_on_reconfigured_startup(self):
        """Новый каталог сервера восстанавливает доступный вход без команды."""
        self.processor.reconcile_menus(self.updates.append)
        self.assertNotIn("/selected", self.commands())
        restarted = ChannelMessageProcessor(
            database_path=self.database_path, capabilities=(FakeCapability(),))
        restarted.reconcile_menus(self.updates.append)
        self.assertIn("/selected", self.commands())

    def test_confirmation_change_reconciles_without_message(self):
        """Появление и исчезновение полного подтверждения меняют меню."""
        self.processor.reconcile_menus(self.updates.append)
        confirmation = PendingConfirmation(
            "core", "identity", 1, 1, ReplyAddress("telegram", "1001"),
            (ConfirmationAction("yes", "Да", "/confirm core identity 1 1 yes"),),
        )
        with patch.object(IncomingMessageDispatcher, "_confirmation_snapshot",
                          return_value=ConfirmationQueryResult("complete", (confirmation,))):
            self.processor.reconcile_menus(self.updates.append)
        self.assertIn("/confirm", self.commands())
        self.processor.reconcile_menus(self.updates.append)
        self.assertNotIn("/confirm", self.commands())

    def test_ttl_expires_without_message(self):
        """Периодический проход снимает ввод по TTL без пользовательского события."""
        now = datetime(2026, 10, 28, tzinfo=timezone.utc)
        self.processor._pending_inputs._clock = lambda: now
        self.processor(self.event("/name"), menu_update_handler=self.updates.append)
        self.assertIn("/cancel", self.commands())
        self.processor._pending_inputs._clock = lambda: now + timedelta(minutes=11)
        self.processor.reconcile_menus(self.updates.append)
        self.assertNotIn("/cancel", self.commands())
        self.assertIsNone(self.processor._pending_inputs.get(ReplyAddress("telegram", "1001")))

    def test_retry_uses_latest_menu_after_delivery_failure(self):
        """Неудачный снимок повторяется с актуальным состоянием после смены ввода."""
        def fail(update):
            raise RuntimeError("transport down")

        with self.assertLogs("compadre_assistant.bootstrap", level="ERROR"):
            self.processor(self.event("/name"), menu_update_handler=fail)
        self.assertIn(ReplyAddress("telegram", "1001"), self.processor.menu_delivery_failures)
        self.processor._pending_inputs.clear(ReplyAddress("telegram", "1001"))
        self.processor.reconcile_menus(self.updates.append)
        self.assertNotIn("/cancel", self.commands())
        self.assertFalse(self.processor.menu_delivery_failures)

    def test_incomplete_confirmation_snapshot_defers_delivery(self):
        """Неполный снимок убирает прежнюю /confirm и не дублирует восстановление."""
        confirmation = PendingConfirmation(
            "core", "identity", 1, 1, ReplyAddress("telegram", "1001"),
            (ConfirmationAction("yes", "Да", "/confirm core identity 1 1 yes"),),
        )
        with patch.object(IncomingMessageDispatcher, "_confirmation_snapshot",
                          return_value=ConfirmationQueryResult("complete", (confirmation,))):
            self.processor.reconcile_menus(self.updates.append)
        self.assertIn("/confirm", self.commands())
        with patch.object(IncomingMessageDispatcher, "_confirmation_snapshot",
                          return_value=ConfirmationQueryResult("unavailable", ())):
            self.processor.reconcile_menus(self.updates.append)
        self.assertEqual(len(self.updates), 2)
        self.assertNotIn("/confirm", self.commands())
        self.assertIn(ReplyAddress("telegram", "1001"), self.processor.menu_delivery_failures)
        self.processor.reconcile_menus(self.updates.append)
        self.assertEqual(len(self.updates), 2)
        self.assertFalse(self.processor.menu_delivery_failures)

    def test_parallel_reconciliation_does_not_duplicate_delivery(self):
        """Одновременные проходы сериализуют проверку и публикацию адреса."""
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: self.processor.reconcile_menus(self.updates.append), range(8)))
        self.assertEqual(len(self.updates), 1)

    def test_pending_address_snapshot_is_safe_during_concurrent_updates(self):
        """Перебор адресов не сталкивается с изменением словаря из другого чата."""
        store = PendingInputStore()
        addresses = tuple(ReplyAddress("telegram", str(index)) for index in range(40))

        def mutate(address):
            for _ in range(20):
                store.begin(address, "core", "name")
                store.addresses()
                store.clear(address)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(mutate, addresses))
        self.assertEqual(store.addresses(), ())

    def test_startup_keeps_profiles_separate(self):
        """Глобальное перечисление не смешивает адреса разных владельцев."""
        with connect_database(self.database_path) as connection:
            service = create_user_identity_service(connection)
            service.register_with_name(IncomingIdentity("vk", "2002", "2002"),
                                       display_name="Второй")
        self.processor.reconcile_menus(self.updates.append)
        self.assertEqual({update.address.chat_id for update in self.updates},
                         {"1001", "2002"})

    def test_stored_group_addresses_are_excluded_from_both_identity_lists(self):
        """Исторические групповые Telegram/VK записи не становятся меню."""
        with connect_database(self.database_path) as connection:
            service = create_user_identity_service(connection)
            tg_group = IncomingIdentity("telegram", "3003", "-100555")
            vk_group = IncomingIdentity("vk", "4004", "2000000001")
            tg_user = service.register_with_name(tg_group, display_name="Группа TG")
            vk_user = service.register_with_name(vk_group, display_name="Группа VK")
            self.assertEqual(service.list_known_private_chats(tg_user), ())
            self.assertEqual(service.list_known_private_chats(vk_user), ())
            self.assertEqual({identity.external_chat_id for _, identity
                              in service.list_all_known_private_chats()}, {"1001"})
        self.processor.reconcile_menus(self.updates.append)
        self.assertEqual([update.address.chat_id for update in self.updates], ["1001"])

    def test_ambiguous_address_blocks_point_lookup_and_origin_fallback(self):
        """Коллизия адреса не открывает меню через точечный поиск или новый вход."""
        with connect_database(self.database_path) as connection:
            service = create_user_identity_service(connection)
            other = service.register_with_name(
                IncomingIdentity("telegram", "9999", "1001"), display_name="Второй")
            self.assertIsNotNone(other)
            self.assertIsNone(service.find_known_private_chat("telegram", "1001"))
            self.assertTrue(service.has_known_chat_address("telegram", "1001"))
        self.processor.reconcile_menus(self.updates.append)
        self.processor(self.event("/name"), menu_update_handler=self.updates.append)
        self.assertEqual(self.updates, [])

    def test_group_origin_does_not_publish_menu(self):
        """Новая групповая команда не использует путь неизвестного личного чата."""
        for channel, sender, chat in (
            ("telegram", "3003", "-100555"),
            ("vk", "4004", "2000000001"),
        ):
            with self.subTest(channel=channel):
                response = self.processor(
                    IncomingEvent(channel, chat, "group-msg", "/register", sender),
                    menu_update_handler=self.updates.append,
                )
                self.assertIsNotNone(response)
        self.assertEqual(self.updates, [])

    def test_foreign_origin_in_known_chat_cannot_publish_owner_menu(self):
        """Чужой автор с тем же адресом не получает меню сохранённого владельца."""
        response = self.processor(
            IncomingEvent("telegram", "1001", "foreign-msg", "/register", "foreign-user"),
            menu_update_handler=self.updates.append,
        )
        self.assertIsNotNone(response)
        self.assertEqual(self.updates, [])

    def test_synthetic_event_uses_current_identity_for_known_chat(self):
        """Внутренний проход без автора подставляет проверенную identity адреса."""
        self.processor._reconcile_menu_event(
            IncomingEvent("telegram", "1001", "synthetic", "/help"),
            self.updates.append, allow_unknown=False,
        )
        self.assertEqual(len(self.updates), 1)
        self.assertIn("/status", self.commands())

    def test_removed_or_ambiguous_address_is_not_republished(self):
        """Сверка не публикует адрес, который больше не принадлежит одному профилю."""
        with connect_database(self.database_path) as connection:
            connection.execute("DELETE FROM external_identities WHERE channel = ? AND external_user_id = ?",
                               ("telegram", "1001"))
            connection.commit()
        self.processor.reconcile_menus(self.updates.append)
        self.assertEqual(self.updates, [])

    def test_removed_between_enumeration_and_read_is_not_published(self):
        """Устаревший адрес не превращается в меню регистрации при гонке."""
        original = UserIdentityService.list_all_known_private_chats
        first = True

        def remove_after_listing(service):
            nonlocal first
            result = original(service)
            if first:
                first = False
                with connect_database(self.database_path) as connection:
                    connection.execute(
                        "DELETE FROM external_identities WHERE channel = ? AND external_user_id = ?",
                        ("telegram", "1001"),
                    )
                    connection.commit()
            return result

        with patch.object(UserIdentityService, "list_all_known_private_chats",
                          remove_after_listing):
            self.processor.reconcile_menus(self.updates.append)
        self.assertEqual(self.updates, [])

    def test_background_runner_checks_without_message(self):
        """Цикл запускает восстановление и завершается по сигналу владельца."""
        stop = Event()
        def receive(update):
            self.updates.append(update)
            stop.set()
        worker = Thread(target=self.processor.run_menu_reconciliation,
                        args=(receive, stop), kwargs={"interval_seconds": 0.01})
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.updates), 1)


if __name__ == "__main__":
    unittest.main()
