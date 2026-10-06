"""Проверки сохранённого маршрута между чатами и процессными соединениями."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from obs_chat_bot.bootstrap import process_channel_incoming_message
from obs_chat_bot.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository, SQLiteExternalIdentityRepository
from obs_chat_bot.core.domain.chat_modes import ChatMode, ChatModeKey
from obs_chat_bot.core.domain.users.entities import IncomingIdentity
from obs_chat_bot.core.presentation.incoming.dispatcher import IncomingMessageDispatcher, IncomingMessageResultType
from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from test_incoming_boundary import FakeCapability, FakeIdentity
from sqlite_helpers import ensure_app_user


class ChatModesTest(unittest.TestCase):
    """Проверяет полную адресацию, восстановление и атомарный первый выбор."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="chat-modes-")
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "app.db"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            for app_user_id in (1, 2, 23):
                ensure_app_user(connection, app_user_id=app_user_id)

    def test_modes_are_isolated_by_user_channel_and_chat(self) -> None:
        """Связанные каналы, разные чаты и пользователи имеют отдельные выборы."""
        keys = [ChatModeKey(1, "telegram", "chat"), ChatModeKey(1, "vk", "chat"), ChatModeKey(1, "telegram", "other"), ChatModeKey(2, "telegram", "chat")]
        with connect_database(self.database_path) as connection:
            repository = SQLiteChatModeRepository(connection)
            for index, key in enumerate(keys):
                repository.save(ChatMode(key, f"mode-{index}"))
            repository.save(ChatMode(keys[0], "replacement"))
            self.assertEqual(repository.get(keys[0]).capability_id, "replacement")
            for index, key in enumerate(keys[1:], 1):
                self.assertEqual(repository.get(key).capability_id, f"mode-{index}")

    def test_mode_survives_new_connection(self) -> None:
        """Новый процессный repository читает прежний выбор из SQLite."""
        mode = ChatMode(ChatModeKey(1, "vk", "peer"), "selected")
        with connect_database(self.database_path) as connection:
            SQLiteChatModeRepository(connection).save(mode)
        with connect_database(self.database_path) as connection:
            self.assertEqual(SQLiteChatModeRepository(connection).get(mode.key), mode)

    def test_concurrent_first_selection_does_not_replace_winner(self) -> None:
        """Два соединения получают одинаковый сохранённый маршрут при гонке."""
        key = ChatModeKey(1, "telegram", "chat")

        def activate(capability_id: str) -> ChatMode:
            with connect_database(self.database_path) as connection:
                return SQLiteChatModeRepository(connection).activate_if_absent(ChatMode(key, capability_id))

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(activate, ("first", "second")))
        self.assertEqual(results[0], results[1])

    def test_clear_leaves_profile_and_other_chat(self) -> None:
        """Удаление выбора не удаляет профиль и маршрут соседнего чата."""
        key = ChatModeKey(1, "telegram", "chat")
        other = ChatMode(ChatModeKey(1, "vk", "peer"), "selected")
        with connect_database(self.database_path) as connection:
            repository = SQLiteChatModeRepository(connection)
            repository.save(ChatMode(key, "selected"))
            repository.save(other)
            repository.clear(key)
            self.assertIsNone(repository.get(key))
            self.assertEqual(repository.get(other.key), other)
            self.assertIsNotNone(SQLiteAppUserRepository(connection).get_by_id(1))

    def test_dispatcher_restores_mode_without_parsing_subject_text(self) -> None:
        """Новый диспетчер передаёт исходное событие в восстановленный вход."""
        identity = FakeIdentity()
        event = IncomingEvent(channel="telegram", chat_id="chat", message_id="1", text="https://example.com/article")
        for _ in range(2):
            capability = FakeCapability()
            with connect_database(self.database_path) as connection:
                repository = SQLiteChatModeRepository(connection)
                dispatcher = IncomingMessageDispatcher(incoming_capability=capability, incoming_capability_id="selected", user_identity_service=identity, chat_mode_repository=repository)
                dispatcher.execute(event)
                self.assertEqual(repository.get(ChatModeKey(23, "telegram", "chat")).capability_id, "selected")
            self.assertEqual(capability.calls, [(identity.user, event)])

    def test_unavailable_mode_is_preserved_and_common_help_still_works(self) -> None:
        """Неизвестный ID не запускает другой вход и не теряется при новом событии."""
        with connect_database(self.database_path) as connection:
            repository = SQLiteChatModeRepository(connection)
            mode = ChatMode(ChatModeKey(23, "telegram", "chat"), "unavailable")
            repository.save(mode)
            capability = FakeCapability()
            dispatcher = IncomingMessageDispatcher(incoming_capability=capability, incoming_capability_id="selected", user_identity_service=FakeIdentity(), chat_mode_repository=repository)
            result = dispatcher.execute(IncomingEvent(channel="telegram", chat_id="chat", message_id="1", text="да"))
            self.assertEqual(result.type, IncomingMessageResultType.MODE_UNAVAILABLE)
            self.assertEqual(capability.calls, [])
            self.assertEqual(repository.get(mode.key), mode)
            help_result = dispatcher.execute(IncomingEvent(channel="telegram", chat_id="chat", message_id="2", text="/help"))
            self.assertEqual(help_result.type, IncomingMessageResultType.HELP)

    def test_composition_root_saves_default_without_general_message_journal(self) -> None:
        """Реальный общий вход подключает SQLite-режим и не журналирует обычный текст."""
        with connect_database(self.database_path) as connection:
            SQLiteExternalIdentityRepository(connection).create(app_user_id=1, identity=IncomingIdentity("telegram", "external", "chat"))
        event = IncomingEvent(channel="telegram", external_user_id="external", chat_id="chat", message_id="1", text="обычный текст")
        response = process_channel_incoming_message(database_path=self.database_path, incoming_message=event, openai_base_url="https://example.com/v1", openai_api_key="test", openai_model="test")
        self.assertTrue(response.text)
        with connect_database(self.database_path) as connection:
            mode = SQLiteChatModeRepository(connection).get(ChatModeKey(1, "telegram", "chat"))
            self.assertEqual(mode.capability_id, "obsidian")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM incoming_messages").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
