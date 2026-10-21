"""Проверяет допуск в независимых processor на общей временной SQLite."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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
from compadre_assistant.presentation.telegram.bot import _register_handlers
from compadre_assistant.presentation.vk.bot import _handle_update
from test_chat_modes import DelayedCapability
from test_incoming_boundary import FakeCapability
from test_telegram_bot import FakeAiogram, TelegramMessage
from test_vk_bot import FakeVkClient


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

    def send_adapter(self, channel: str, processor: ChannelMessageProcessor,
                     text: str, user_id: str, chat_id: str) -> str:
        """Передаёт команду через настоящий адаптер с fake-клиентом канала."""
        logger = logging.getLogger("test.personal.adapters")
        if channel == "telegram":
            aiogram = FakeAiogram()
            _register_handlers(
                aiogram.dispatcher, aiogram,
                incoming_message_processor=processor, logger=logger,
            )
            message = TelegramMessage()
            message.chat = SimpleNamespace(id=chat_id)
            message.from_user = SimpleNamespace(id=user_id, username=None, full_name="Тест")
            message.message_id = 1
            message.text = text
            asyncio.run(aiogram.dispatcher.included_router.handler(message))
            return "\n".join(message.answers)
        client = FakeVkClient()
        _handle_update(
            {"type": "message_new", "object": {"message": {
                "id": 1, "peer_id": int(chat_id), "from_id": int(user_id), "text": text,
            }}},
            incoming_message_processor=processor, client=client, logger=logger,
        )
        return "\n".join(reply for _, reply in client.messages)

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

    def test_adapters_share_selection_and_rebind_uses_destination_profile(self) -> None:
        """TG/VK видят commit и после rebind применяют настройки нового AppUser."""
        with connect_database(self.path) as connection:
            connection.executemany(
                "UPDATE external_identities SET external_user_id = ?, external_chat_id = ? "
                "WHERE channel = ? AND external_user_id = ?",
                (("7", "42", "telegram", "tg-user"),
                 ("8", "43", "telegram", "other-user"),
                 ("9", "44", "vk", "vk-user")),
            )
            connection.commit()
        telegram = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        vk = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))

        def tg(text: str, user: str = "7", chat: str = "42") -> str:
            return self.send_adapter("telegram", telegram, text, user, chat)

        def send_vk(text: str) -> str:
            return self.send_adapter("vk", vk, text, "9", "44")

        self.assertIn("/enable example", send_vk("/example"))
        self.assertIn("включена", tg("/enable example"))
        self.assertIn("Актуальное состояние", send_vk("/example"))
        self.assertIn("выключена", send_vk("/disable example"))
        self.assertIn("/enable example", tg("/example"))
        self.assertIn("/enable example", tg("/example", "8", "43"))
        self.assertIn("включена", tg("/enable example", "8", "43"))
        code_reply = tg("/link_code", "8", "43")
        code = re.search(r"Код привязки: `([^`]+)`", code_reply)
        self.assertIsNotNone(code, code_reply)
        rebind = send_vk(f"/link {code.group(1)}")
        confirmation = re.search(r"/confirm core identity \d+ \d+ yes", rebind)
        self.assertIsNotNone(confirmation, rebind)
        self.assertIn("перепривязал", send_vk(confirmation.group()))
        self.assertIn("Актуальное состояние", send_vk("/example"))
        self.assertIn("/enable example", tg("/example"))
        with connect_database(self.path) as connection:
            owner = connection.execute(
                "SELECT app_user_id FROM external_identities WHERE channel = 'vk' "
                "AND external_user_id = '9'"
            ).fetchone()[0]
            self.assertEqual(owner, 2)
            choices = connection.execute(
                "SELECT app_user_id, enabled FROM user_capability_preferences "
                "WHERE capability_id = 'example' ORDER BY app_user_id"
            ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in choices], [(1, 0), (2, 1)])

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

    def test_disable_command_is_shared_and_reenable_restores_saved_mode(self) -> None:
        """TG выключает VK-вход; второй AppUser и маршрут VK остаются прежними."""
        first = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        second = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        first(self.event("/enable example", "telegram", "tg-user", "tg-chat"))
        self.save(2, True)
        second(self.event("/example", "vk", "vk-user", "vk-chat"))
        reply = first(self.event("/disable example", "telegram", "tg-user", "tg-chat"))
        self.assertIn("выключена", reply.text)
        self.assertIn("/enable example", second(self.event(
            "текст", "vk", "vk-user", "vk-chat"
        )).text)
        self.assertEqual(first(self.event(
            "/example", "telegram", "other-user", "other-chat"
        )).text, "Актуальное состояние")
        with connect_database(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT capability_id FROM active_chat_modes "
                "WHERE app_user_id = 1 AND channel = 'vk' AND chat_id = 'vk-chat'"
            ).fetchone()[0], "example")
        first(self.event("/enable example", "telegram", "tg-user", "tg-chat"))
        self.assertEqual(second(self.event("текст", "vk", "vk-user", "vk-chat")).text, "Тестовый ответ")


if __name__ == "__main__":
    unittest.main()
