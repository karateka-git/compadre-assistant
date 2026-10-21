"""Проверяет допуск в независимых processor на общей временной SQLite."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compadre_assistant.bootstrap import ChannelMessageProcessor
from compadre_assistant.core.data.sqlite.capability_preference_repository import (
    SQLiteUserCapabilityPreferenceRepository,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import CapabilityDescriptor
from test_chat_modes import DelayedCapability
from test_incoming_boundary import FakeCapability


class PersonalProcessorAdmissionTest(unittest.TestCase):
    """Проверяет границу чтения личного выбора и отсутствие process cache."""

    def setUp(self) -> None:
        """Создаёт два профиля и два канала первого профиля."""
        directory = TemporaryDirectory(prefix="personal-admission-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            connection.execute("INSERT INTO app_users (id, display_name) VALUES (1, 'Один'), (2, 'Два')")
            connection.executemany(
                "INSERT INTO external_identities "
                "(app_user_id, channel, external_user_id, external_chat_id) VALUES (?, ?, ?, ?)",
                ((1, "telegram", "tg-user", "tg-chat"),
                 (1, "vk", "vk-user", "vk-chat"),
                 (2, "telegram", "other-user", "other-chat")),
            )
            connection.commit()

    def event(self, text: str, channel: str, external_user_id: str, chat_id: str) -> IncomingEvent:
        """Создаёт событие указанного привязанного канала."""
        return IncomingEvent(channel, chat_id, "1", text, external_user_id)

    def save(self, app_user_id: int, enabled: bool) -> None:
        """Фиксирует настройку через независимое SQLite-соединение."""
        with connect_database(self.path) as connection:
            SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                UserCapabilityPreference(app_user_id, "example", enabled)
            )

    @staticmethod
    def capability() -> FakeCapability:
        """Создаёт вход с default False, как у нового встроенного ID."""
        capability = FakeCapability("example", "/example")
        capability._descriptor = CapabilityDescriptor("example", "/example", "пример")
        return capability

    def test_two_processors_see_commits_and_isolate_users(self) -> None:
        """TG и VK читают новый выбор AppUser; второй профиль не меняется."""
        first = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        second = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        tg = self.event("/example", "telegram", "tg-user", "tg-chat")
        vk = self.event("/example", "vk", "vk-user", "vk-chat")
        other = self.event("/example", "telegram", "other-user", "other-chat")
        self.assertIn("/enable example", first(tg).text)
        self.save(1, True)
        self.assertEqual(second(vk).text, "Актуальное состояние")
        self.assertIn("/example", first(self.event("/help", "telegram", "tg-user", "tg-chat")).text)
        self.assertIn("/enable example", first(other).text)
        self.save(1, False)
        self.assertIn("/enable example", second(vk).text)
        self.assertIn("/enable example", first(tg).text)
        self.save(2, True)
        self.assertEqual(first(other).text, "Актуальное состояние")
        self.assertIn("/enable example", second(vk).text)

    def test_admitted_entry_finishes_after_disable_commit(self) -> None:
        """Уже допущенный вызов завершается, следующий вход видит disable."""
        capability = DelayedCapability(delay_enter=True)
        capability._descriptor = CapabilityDescriptor("selected", "/selected", "пример")
        first = ChannelMessageProcessor(database_path=self.path, capabilities=(capability,))
        second = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        with connect_database(self.path) as connection:
            SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                UserCapabilityPreference(1, "selected", True)
            )
        event = self.event("/selected", "telegram", "tg-user", "tg-chat")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first, event)
            try:
                self.assertTrue(capability.started.wait(5))
                with connect_database(self.path) as connection:
                    SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                        UserCapabilityPreference(1, "selected", False)
                    )
            finally:
                capability.release.set()
            self.assertEqual(future.result(timeout=5).text, "Актуальное состояние")
        self.assertIn("/enable selected", first(self.event("текст", "telegram", "tg-user", "tg-chat")).text)
        self.assertIn("/enable example", second(self.event("/example", "vk", "vk-user", "vk-chat")).text)

    def test_enable_command_changes_both_channels_without_clearing_mode(self) -> None:
        """Выбор из TG открывает VK и сохраняет прежний маршрут этого чата."""
        first = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        second = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        tg = self.event("/enable example", "telegram", "tg-user", "tg-chat")
        vk = self.event("/example", "vk", "vk-user", "vk-chat")
        self.assertIn("включена", first(tg).text)
        self.assertEqual(second(vk).text, "Актуальное состояние")
        self.assertIn("уже включена", second(self.event(
            "/enable example", "vk", "vk-user", "vk-chat"
        )).text)
        with connect_database(self.path) as connection:
            mode = connection.execute(
                "SELECT capability_id FROM active_chat_modes "
                "WHERE app_user_id = 1 AND channel = 'vk' AND chat_id = 'vk-chat'"
            ).fetchone()[0]
            self.assertEqual(mode, "example")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM user_capability_preferences WHERE app_user_id = 2"
                ).fetchone()[0], 0,
            )


if __name__ == "__main__":
    unittest.main()
