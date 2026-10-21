"""Проверки сохранённого маршрута между чатами и процессными соединениями."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from channel_helpers import process_channel_incoming_message
from compadre_assistant.capabilities.obsidian.data.configuration.config import ObsidianConfig
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository, SQLiteExternalIdentityRepository
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import CapabilityMode
from test_incoming_boundary import FakeCapability, FakeIdentity
from sqlite_helpers import ensure_app_user


class DelayedCapability(FakeCapability):
    """Останавливает выбранный вызов до конкурирующей смены режима."""

    def __init__(self, *, delay_enter: bool = False, delay_handle: bool = False) -> None:
        super().__init__()
        self.delay_enter = delay_enter
        self.delay_handle = delay_handle
        self.started = threading.Event()
        self.release = threading.Event()

    def enter(self, context, event, arguments):
        """Ожидает разрешения перед завершением входа."""
        if self.delay_enter:
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("Delayed entry was not released")
        return super().enter(context, event, arguments)

    def handle(self, context, event):
        """Ожидает разрешения перед завершением текущего режима."""
        if self.delay_handle:
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("Delayed handling was not released")
        return super().handle(context, event)


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

    def test_only_one_connection_can_replace_the_same_revision(self) -> None:
        """Две независимые записи с одной ревизией дают ровно одного победителя."""
        key = ChatModeKey(1, "telegram", "cas-chat")
        start = threading.Event()
        ready = threading.Barrier(3)

        def replace(capability_id: str) -> bool:
            with connect_database(self.database_path) as connection:
                repository = SQLiteChatModeRepository(connection)
                self.assertEqual(repository.get_state(key).revision, 0)
                ready.wait(timeout=5)
                self.assertTrue(start.wait(5))
                return repository.replace_if_revision(key, 0, capability_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(replace, name) for name in ("first", "second")]
            ready.wait(timeout=5)
            start.set()
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(results), [False, True])
        with connect_database(self.database_path) as connection:
            state = SQLiteChatModeRepository(connection).get_state(key)
            self.assertIn(state.capability_id, {"first", "second"})
            self.assertEqual(state.revision, 1)

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

    def test_late_enter_cannot_restore_mode_after_exit_on_other_connection(self) -> None:
        """Поздний вход не отменяет /exit, а tombstone переживает новое соединение."""
        capability = DelayedCapability(delay_enter=True)
        key = ChatModeKey(23, "telegram", "chat")
        registry = CapabilityRegistry((capability,))
        event = IncomingEvent(channel="telegram", chat_id="chat", message_id="1", text="/selected")

        def enter() -> None:
            with connect_database(self.database_path) as connection:
                dispatcher = IncomingMessageDispatcher(
                    registry=registry, user_identity_service=FakeIdentity(),
                    chat_mode_repository=SQLiteChatModeRepository(connection),
                    capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
                )
                dispatcher.execute(event)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(enter)
            try:
                self.assertTrue(capability.started.wait(5))
                with connect_database(self.database_path) as connection:
                    dispatcher = IncomingMessageDispatcher(
                        registry=registry, user_identity_service=FakeIdentity(),
                        chat_mode_repository=SQLiteChatModeRepository(connection),
                        capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
                    )
                    dispatcher.execute(IncomingEvent(
                        channel="telegram", chat_id="chat", message_id="2", text="/exit"
                    ))
                    self.assertEqual(SQLiteChatModeRepository(connection).get_state(key).revision, 1)
            finally:
                capability.release.set()
            future.result(timeout=5)
        with connect_database(self.database_path) as connection:
            repository = SQLiteChatModeRepository(connection)
            self.assertIsNone(repository.get(key))
            self.assertEqual(repository.get_state(key).revision, 1)

    def test_late_complete_cannot_clear_new_mode_on_other_connection(self) -> None:
        """Поздний COMPLETE не удаляет более свежий выбор другой возможности."""
        capability = DelayedCapability(delay_handle=True)
        capability.handle_mode = CapabilityMode.COMPLETE
        other = FakeCapability("other", "/other")
        registry = CapabilityRegistry((capability, other))
        key = ChatModeKey(23, "vk", "chat")
        with connect_database(self.database_path) as connection:
            SQLiteChatModeRepository(connection).save(ChatMode(key, "selected"))

        def handle() -> None:
            with connect_database(self.database_path) as connection:
                dispatcher = IncomingMessageDispatcher(
                    registry=registry, user_identity_service=FakeIdentity(),
                    chat_mode_repository=SQLiteChatModeRepository(connection),
                    capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
                )
                dispatcher.execute(IncomingEvent(
                    channel="vk", chat_id="chat", message_id="1", text="завершить"
                ))

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(handle)
            try:
                self.assertTrue(capability.started.wait(5))
                with connect_database(self.database_path) as connection:
                    dispatcher = IncomingMessageDispatcher(
                        registry=registry, user_identity_service=FakeIdentity(),
                        chat_mode_repository=SQLiteChatModeRepository(connection),
                        capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
                    )
                    dispatcher.execute(IncomingEvent(
                        channel="vk", chat_id="chat", message_id="2", text="/other"
                    ))
                    self.assertEqual(SQLiteChatModeRepository(connection).get(key).capability_id, "other")
            finally:
                capability.release.set()
            future.result(timeout=5)
        with connect_database(self.database_path) as connection:
            repository = SQLiteChatModeRepository(connection)
            self.assertEqual(repository.get(key).capability_id, "other")
            self.assertEqual(repository.get_state(key).revision, 2)

    def test_dispatcher_restores_mode_without_parsing_subject_text(self) -> None:
        """Новый диспетчер передаёт исходное событие в восстановленный вход."""
        identity = FakeIdentity()
        event = IncomingEvent(channel="telegram", chat_id="chat", message_id="1", text="https://example.com/article")
        for _ in range(2):
            capability = FakeCapability()
            with connect_database(self.database_path) as connection:
                repository = SQLiteChatModeRepository(connection)
                dispatcher = IncomingMessageDispatcher(registry=CapabilityRegistry((capability,)), user_identity_service=identity, chat_mode_repository=repository, capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection))
                if repository.get(ChatModeKey(23, "telegram", "chat")) is None:
                    dispatcher.execute(IncomingEvent(channel="telegram", chat_id="chat", message_id="0", text="/selected"))
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
            dispatcher = IncomingMessageDispatcher(registry=CapabilityRegistry((capability,)), user_identity_service=FakeIdentity(), chat_mode_repository=repository, capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection))
            result = dispatcher.execute(IncomingEvent(channel="telegram", chat_id="chat", message_id="1", text="да"))
            self.assertIn("отключена", result.text)
            self.assertEqual(capability.calls, [])
            self.assertEqual(repository.get(mode.key), mode)
            help_result = dispatcher.execute(IncomingEvent(channel="telegram", chat_id="chat", message_id="2", text="/help"))
            self.assertIn("/selected", help_result.text)

    def test_composition_root_requires_explicit_entry_without_general_message_journal(self) -> None:
        """Реальный общий вход подключает SQLite-режим и не журналирует обычный текст."""
        with connect_database(self.database_path) as connection:
            SQLiteExternalIdentityRepository(connection).create(app_user_id=1, identity=IncomingIdentity("telegram", "external", "chat"))
        event = IncomingEvent(channel="telegram", external_user_id="external", chat_id="chat", message_id="1", text="обычный текст")
        response = process_channel_incoming_message(
            database_path=self.database_path, incoming_message=event,
            obsidian_config=ObsidianConfig(
                openai_base_url="https://example.com/v1",
                openai_api_key="test", openai_model="test",
            ),
        )
        self.assertTrue(response.text)
        with connect_database(self.database_path) as connection:
            mode = SQLiteChatModeRepository(connection).get(ChatModeKey(1, "telegram", "chat"))
            self.assertIsNone(mode)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM incoming_messages").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
