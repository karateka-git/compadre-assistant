"""Сквозная проверка меню двух личных каналов и текстового маршрута."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    ConfirmationAction, ConfirmationQueryResult, PendingConfirmation,
)
from tests.response_assertions import response_text
from tests.test_incoming_boundary import FakeCapability


class MenuLifecycleTest(unittest.TestCase):
    """Проверяет последовательные переходы поверх SQLite и процессного входа."""

    def setUp(self):
        """Создаёт пустую БД и независимые адреса Telegram и VK."""
        directory = TemporaryDirectory(prefix="menu-lifecycle-")
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "app.db"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.capability = FakeCapability()
        self.processor = ChannelMessageProcessor(
            database_path=self.database_path, capabilities=(self.capability,))
        self.updates = []
        self.telegram = IncomingIdentity("telegram", "101", "101")
        self.vk = IncomingIdentity("vk", "202", "202")

    def event(self, identity, text):
        """Создаёт сообщение из конкретного личного диалога."""
        return IncomingEvent(identity.channel, identity.external_chat_id, "message", text,
                             identity.external_user_id)

    def send(self, identity, text, handler=None):
        """Выполняет команду с наблюдаемой публикацией меню."""
        return self.processor(self.event(identity, text),
                              menu_update_handler=handler or self.updates.append)

    def menu(self, identity):
        """Возвращает последнее опубликованное меню нужного канала."""
        address = ReplyAddress(identity.channel, identity.external_chat_id)
        return next(update.menu for update in reversed(self.updates)
                    if update.address == address)

    def assert_menu_matches_help(self, identity):
        """Сверяет выполнимые пункты меню с текущей текстовой справкой."""
        help_text = response_text(self.send(identity, "/help"))
        for item in self.menu(identity).items:
            with self.subTest(channel=identity.channel, command=item.command):
                self.assertIn(item.command, help_text)

    def test_registration_link_modes_access_input_failure_and_restart(self):
        """Один цикл проходит оба канала, общий доступ и восстановление меню."""
        self.send(self.telegram, "/help")
        self.assertEqual(tuple(item.command for item in self.menu(self.telegram).items),
                         ("/register", "/link"))
        self.send(self.telegram, "/register")
        self.assertEqual(tuple(item.command for item in self.menu(self.telegram).items),
                         ("/cancel",))
        self.assert_menu_matches_help(self.telegram)
        self.send(self.telegram, "Первый")
        self.assertIn("/selected", tuple(item.command for item in self.menu(self.telegram).items))
        self.assert_menu_matches_help(self.telegram)

        with connect_database(self.database_path) as connection:
            service = create_user_identity_service(connection)
            user = service.resolve(self.telegram)
            code = service.create_link_code(user.id).code
        self.send(self.vk, "/link " + code)
        self.assertIn("/selected", tuple(item.command for item in self.menu(self.vk).items))
        self.assert_menu_matches_help(self.vk)

        self.send(self.telegram, "/selected")
        self.assertEqual(tuple(item.command for item in self.menu(self.telegram).items),
                         ("/exit",))
        self.assertIn("/selected", tuple(item.command for item in self.menu(self.vk).items))
        self.assert_menu_matches_help(self.telegram)

        def fail(_update):
            raise RuntimeError("menu API unavailable")

        with self.assertLogs("compadre_assistant.bootstrap", level="ERROR"):
            response = self.send(self.vk, "/disable selected", handler=fail)
        self.assertIn("выключена", response_text(response))
        self.assertIn(ReplyAddress("vk", "202"), self.processor.menu_delivery_failures)
        self.assertIn("/exit", response_text(self.send(self.telegram, "/help", handler=fail)))
        self.processor.reconcile_menus(self.updates.append)
        self.assertNotIn("/selected", tuple(item.command for item in self.menu(self.vk).items))
        self.assertEqual(tuple(item.command for item in self.menu(self.telegram).items),
                         ("/exit",))
        self.assert_menu_matches_help(self.telegram)
        self.assert_menu_matches_help(self.vk)

        self.send(self.vk, "/name")
        self.assertIn("/cancel", tuple(item.command for item in self.menu(self.vk).items))
        self.assert_menu_matches_help(self.vk)
        restarted = ChannelMessageProcessor(database_path=self.database_path,
                                            capabilities=(self.capability,))
        restarted.reconcile_menus(self.updates.append)
        self.assertNotIn("/cancel", tuple(item.command for item in self.menu(self.vk).items))
        self.assertEqual(tuple(item.command for item in self.menu(self.telegram).items),
                         ("/exit",))

    def test_confirmation_menu_tracks_complete_snapshot_and_text_path(self):
        """Неполное чтение скрывает пункт, адресная команда остаётся текстовой."""
        self.send(self.telegram, "/register")
        self.send(self.telegram, "Первый")
        confirmation = PendingConfirmation(
            "core", "identity", 4, 1, ReplyAddress("telegram", "101"),
            (ConfirmationAction("yes", "Подтвердить", "/confirm core identity 4 1 yes"),),
        )
        with patch.object(IncomingMessageDispatcher, "_confirmation_snapshot",
                          return_value=ConfirmationQueryResult("complete", (confirmation,))):
            self.processor.reconcile_menus(self.updates.append)
            self.assertIn("/confirm", tuple(item.command for item in self.menu(self.telegram).items))
            self.assert_menu_matches_help(self.telegram)
            response = self.send(self.telegram, "/confirm")
            self.assertIn("/confirm core identity 4 1 yes", response_text(response))
        with patch.object(IncomingMessageDispatcher, "_confirmation_snapshot",
                          return_value=ConfirmationQueryResult("unavailable", ())):
            self.processor.reconcile_menus(self.updates.append)
            self.assertNotIn("/confirm", tuple(item.command for item in self.menu(self.telegram).items))
        self.assertIn(ReplyAddress("telegram", "101"), self.processor.menu_delivery_failures)


if __name__ == "__main__":
    unittest.main()
