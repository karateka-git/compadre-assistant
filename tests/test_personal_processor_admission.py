from tests.response_assertions import content_text, response_text
"""Проверяет допуск в независимых processor на общей временной SQLite."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import multiprocessing
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


class ProcessCapability(FakeCapability):
    """Удерживает допущенный вход до сигнала из другого процесса."""

    def __init__(self, started, release) -> None:
        """Принимает межпроцессные сигналы для управляемой гонки."""
        super().__init__("example", "/example")
        self._descriptor = CapabilityDescriptor("example", "/example", "пример")
        self.started = started
        self.release = release

    def enter(self, context, event, arguments):
        """Ожидает второго процесса после проверки личного допуска."""
        if self.started is not None:
            self.started.set()
            if not self.release.wait(10):
                raise TimeoutError("Допущенный вход не освобождён")
        return super().enter(context, event, arguments)


def _process_event(path: Path, text: str, started, release, results) -> None:
    """Обрабатывает одно сообщение с собственным processor и SQLite-соединением."""
    capability = ProcessCapability(started, release)
    processor = ChannelMessageProcessor(database_path=path, capabilities=(capability,))
    event = IncomingEvent("telegram", "tg-chat", "1", text, "tg-user")
    reply = processor(event)
    results.put((response_text(reply), len(capability.entries)))


def _process_preference(path: Path, app_user_id: int, capability_id: str,
                        enabled: bool, ready, release, results) -> None:
    """Записывает один ключ после общего сигнала старта и возвращает commit."""
    with connect_database(path) as connection:
        ready.set()
        if not release.wait(10):
            raise TimeoutError("Запись не освобождена")
        old = SQLiteUserCapabilityPreferenceRepository(connection).upsert(
            UserCapabilityPreference(app_user_id, capability_id, enabled)
        )
    results.put(old)


def _process_adapter_event(path: Path, channel: str, text: str, results) -> None:
    """Запускает отдельный TG или VK adapter с новым processor."""
    capability = ProcessCapability(None, None)
    processor = ChannelMessageProcessor(database_path=path, capabilities=(capability,))
    logger = logging.getLogger("test.personal.process-adapters")
    if channel == "telegram":
        aiogram = FakeAiogram()
        _register_handlers(
            aiogram.dispatcher, aiogram,
            incoming_message_processor=processor, logger=logger,
        )
        message = TelegramMessage()
        message.chat = SimpleNamespace(id=42)
        message.from_user = SimpleNamespace(id=7, username=None, full_name="Тест")
        message.message_id = 1
        message.text = text
        asyncio.run(aiogram.dispatcher.included_router.handler(message))
        results.put(("\n".join(message.answers), len(capability.entries)))
        return
    client = FakeVkClient()
    _handle_update(
        {"type": "message_new", "object": {"message": {
            "id": 1, "peer_id": 44, "from_id": 9, "text": text,
        }}},
        incoming_message_processor=processor, client=client, logger=logger,
    )
    results.put(("\n".join(reply for _, reply in client.messages), len(capability.entries)))


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
        self.assertIn("/enable example", response_text(first(tg)))
        self.save(1, True)
        self.assertEqual(response_text(second(vk)), "Актуальное состояние")
        self.assertIn("/example", response_text(first(self.event("/help", "telegram", "tg-user", "tg-chat"))))
        self.assertIn("/enable example", response_text(first(other)))
        self.save(1, False)
        self.assertIn("/enable example", response_text(second(vk)))
        self.assertIn("/enable example", response_text(first(tg)))
        self.save(2, True)
        self.assertEqual(response_text(first(other)), "Актуальное состояние")
        self.assertIn("/enable example", response_text(second(vk)))

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
        code = re.search(r"Код привязки: ([^\n]+)", code_reply)
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
            self.assertEqual(response_text(future.result(timeout=5)), "Актуальное состояние")
        self.assertIn("/enable selected", response_text(first(self.event("текст", "telegram", "tg-user", "tg-chat"))))
        self.assertIn("/enable example", response_text(second(self.event("/example", "vk", "vk-user", "vk-chat"))))

    def test_enable_command_changes_both_channels_without_clearing_mode(self) -> None:
        """Выбор из TG открывает VK и сохраняет прежний маршрут этого чата."""
        first = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        second = ChannelMessageProcessor(database_path=self.path, capabilities=(self.capability(),))
        tg = self.event("/enable example", "telegram", "tg-user", "tg-chat")
        vk = self.event("/example", "vk", "vk-user", "vk-chat")
        self.assertIn("включена", response_text(first(tg)))
        self.assertEqual(response_text(second(vk)), "Актуальное состояние")
        self.assertIn("уже включена", response_text(second(self.event(
            "/enable example", "vk", "vk-user", "vk-chat"
        ))))
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
        self.assertIn("выключена", response_text(reply))
        self.assertIn("/enable example", response_text(second(self.event(
            "текст", "vk", "vk-user", "vk-chat"
        ))))
        self.assertEqual(response_text(first(self.event(
            "/example", "telegram", "other-user", "other-chat"
        ))), "Актуальное состояние")
        with connect_database(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT capability_id FROM active_chat_modes "
                "WHERE app_user_id = 1 AND channel = 'vk' AND chat_id = 'vk-chat'"
            ).fetchone()[0], "example")
        first(self.event("/enable example", "telegram", "tg-user", "tg-chat"))
        self.assertEqual(response_text(second(self.event("текст", "vk", "vk-user", "vk-chat"))), "Тестовый ответ")

    def test_process_boundary_before_and_after_disable_commit(self) -> None:
        """Допуск до commit завершается, а новый процесс после commit не входит."""
        self.save(1, True)
        context = multiprocessing.get_context("spawn")
        started = context.Event()
        release = context.Event()
        results = context.Queue()
        admitted = context.Process(
            target=_process_event,
            args=(self.path, "/example", started, release, results),
        )
        admitted.start()
        try:
            self.assertTrue(started.wait(10), "Первый процесс не достиг handler")
            disabled_results = context.Queue()
            disabled = context.Process(
                target=_process_event,
                args=(self.path, "/disable example", None, None, disabled_results),
            )
            disabled.start()
            disabled.join(10)
            self.assertEqual(disabled.exitcode, 0)
            self.assertIn("выключена", disabled_results.get(timeout=2)[0])
        finally:
            release.set()
            admitted.join(10)
        self.assertEqual(admitted.exitcode, 0)
        self.assertEqual(results.get(timeout=2), ("Актуальное состояние", 1))

        after_results = context.Queue()
        after = context.Process(
            target=_process_event,
            args=(self.path, "/example", None, None, after_results),
        )
        after.start()
        after.join(10)
        self.assertEqual(after.exitcode, 0)
        text, entered = after_results.get(timeout=2)
        self.assertIn("/enable example", text)
        self.assertEqual(entered, 0)

    def test_separate_adapter_processes_see_cross_channel_commits(self) -> None:
        """TG и VK в разных процессах читают общий AppUser после commit."""
        with connect_database(self.path) as connection:
            connection.executemany(
                "UPDATE external_identities SET external_user_id = ?, external_chat_id = ? "
                "WHERE channel = ? AND external_user_id = ?",
                (("7", "42", "telegram", "tg-user"),
                 ("9", "44", "vk", "vk-user")),
            )
            connection.commit()
        context = multiprocessing.get_context("spawn")

        def send(channel: str, text: str) -> tuple[str, int]:
            """Ожидает отдельный канал и возвращает ответ с числом входов."""
            outcome = context.Queue()
            process = context.Process(
                target=_process_adapter_event,
                args=(self.path, channel, text, outcome),
            )
            process.start()
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            return outcome.get(timeout=2)

        self.assertIn("включена", send("telegram", "/enable example")[0])
        self.assertEqual(send("vk", "/example"), ("Актуальное состояние", 1))
        self.assertIn("выключена", send("vk", "/disable example")[0])
        text, entered = send("telegram", "/example")
        self.assertIn("/enable example", text)
        self.assertEqual(entered, 0)

    def test_process_writes_keep_distinct_keys_and_last_same_key(self) -> None:
        """Независимые процессы сохраняют разные ID и порядок commit одного ID."""
        context = multiprocessing.get_context("spawn")
        release = context.Event()
        ready = (context.Event(), context.Event())
        results = (context.Queue(), context.Queue())
        processes = (
            context.Process(target=_process_preference, args=(
                self.path, 1, "ask", False, ready[0], release, results[0],
            )),
            context.Process(target=_process_preference, args=(
                self.path, 1, "example", True, ready[1], release, results[1],
            )),
        )
        for process in processes:
            process.start()
        try:
            self.assertTrue(all(event.wait(10) for event in ready))
        finally:
            release.set()
            for process in processes:
                process.join(10)
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual([result.get(timeout=2) for result in results], [None, None])
        with connect_database(self.path) as connection:
            rows = connection.execute(
                "SELECT capability_id, enabled FROM user_capability_preferences "
                "WHERE app_user_id = 1 ORDER BY capability_id"
            ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in rows], [("ask", 0), ("example", 1)])

        same_ready = (context.Event(), context.Event())
        same_go = (context.Event(), context.Event())
        same_results = (context.Queue(), context.Queue())
        same_writers = tuple(context.Process(target=_process_preference, args=(
            self.path, 1, "example", enabled, same_ready[index],
            same_go[index], same_results[index],
        )) for index, enabled in enumerate((False, True)))
        for writer in same_writers:
            writer.start()
        self.assertTrue(all(event.wait(10) for event in same_ready))
        for index, expected_old in enumerate((True, False)):
            same_go[index].set()
            same_writers[index].join(10)
            self.assertEqual(same_writers[index].exitcode, 0)
            self.assertEqual(same_results[index].get(timeout=2), expected_old)
        with connect_database(self.path) as connection:
            self.assertTrue(SQLiteUserCapabilityPreferenceRepository(connection).get_for_user(1)[1].enabled)


if __name__ == "__main__":
    unittest.main()
