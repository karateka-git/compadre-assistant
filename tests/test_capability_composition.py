"""Автономные проверки новой сборки на временной SQLite и отложенной доставке."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock
from obs_chat_bot.capabilities.obsidian.domain.pending import PendingOrigin
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from obs_chat_bot.bootstrap import create_channel_message_processor, create_user_identity_service, validate_enabled_capabilities
from channel_helpers import process_channel_incoming_message
from obs_chat_bot.capabilities.obsidian.bootstrap import ObsidianProcessRuntime, create_obsidian_process_runtime, create_vault_note_chunker
from obs_chat_bot.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import GitHubConnectionCompletion, GitHubConnectionCompletionStatus
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_selection import GitHubAccountNotConnectedError
from obs_chat_bot.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from obs_chat_bot.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.core.domain.chat_modes import ChatMode, ChatModeKey
from obs_chat_bot.core.domain.users.entities import AppUser, IncomingIdentity
from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import HandlerContext
from test_process_incoming_message import FakeArticleUrlUseCase, FakeAnalysisUseCase, FakeConfirmationService, FakeGitHubConnectionStarter, FakeVaultSelectionManager, _proposal, _test_vault


class CapabilityCompositionTest(unittest.TestCase):
    """Проверяет сборку с реальными repositories и подменными внешними сервисами."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="capability-composition-")
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "app.db"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            user = service.register(IncomingIdentity("telegram", "external", "chat"))
            self.user = service.update_display_name(app_user_id=user.id, display_name="Тест")
        self.key = ChatModeKey(self.user.id, "telegram", "chat")
        self.article = FakeArticleUrlUseCase()
        self.analysis = FakeAnalysisUseCase()

    def event(self, text: str, channel: str = "telegram", chat: str = "chat") -> IncomingEvent:
        """Создаёт событие зарегистрированного тестового пользователя."""
        return IncomingEvent(channel=channel, chat_id=chat, external_user_id="external", message_id="1", text=text)

    def process(self, text: str, **overrides):
        """Открывает новую production-сборку на каждое тестовое сообщение."""
        return process_channel_incoming_message(
            database_path=self.database_path,
            incoming_message=self.event(text),
            openai_base_url="https://example.com/v1",
            openai_api_key="test",
            openai_model="test",
            use_case_factory=lambda _connection: self.article,
            analysis_use_case_factory=lambda _connection: self.analysis,
            **overrides,
        )

    def test_composition_rejects_unknown_or_duplicate_enabled_ids(self) -> None:
        """Ошибки выбранных сборок появляются до запуска канала."""
        for identifiers in (("unknown",), ("obsidian", "obsidian")):
            with self.subTest(identifiers=identifiers), self.assertRaises(ValueError):
                validate_enabled_capabilities(identifiers)

    def test_process_registry_reuses_handler_and_scopes_only_obsidian_events(self) -> None:
        """Общие команды обходят Obsidian scope, а предметные события получают новые соединения."""
        connections = []

        def article_factory(connection):
            """Записывает соединение, принадлежащее только текущей операции."""
            connections.append(connection)
            return self.article

        with create_channel_message_processor(
            database_path=self.database_path,
            openai_base_url="https://example.com/v1",
            openai_api_key="test", openai_model="test",
            use_case_factory=article_factory,
            analysis_use_case_factory=lambda _connection: self.analysis,
        ) as processor:
            registered = processor._registry.by_id("obsidian")
            for command in ("/help", "/status", "/name Имя", "/link_code"):
                processor(self.event(command))
            self.assertEqual(connections, [])
            self.assertIn("Режим Obsidian", processor(self.event("/obsidian")).text)
            self.assertIs(processor._registry.by_id("obsidian"), registered)
            self.assertIn("Режим Obsidian", processor(self.event("/obsidian")).text)
        self.assertEqual(len(connections), 2)
        self.assertIsNot(connections[0], connections[1])
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_processor_closes_only_owned_process_clients(self) -> None:
        """Остановка процессного входа закрывает HTTP/LLM клиентов по одному разу."""
        class Client:
            def __init__(self):
                self.closes = 0

            def close(self):
                self.closes += 1

        github, llm, embedding = Client(), Client(), Client()
        runtime = ObsidianProcessRuntime(None, github, llm, embedding)
        with create_channel_message_processor(
            database_path=self.database_path,
            runtime=runtime,
        ) as processor:
            processor(self.event("/help"))
            self.assertEqual((github.closes, llm.closes, embedding.closes), (0, 0, 0))
        self.assertEqual((github.closes, llm.closes, embedding.closes), (1, 1, 1))

    def test_failed_process_registration_closes_runtime_clients(self) -> None:
        """Сбой сборки реестра после создания runtime не оставляет открытых клиентов."""
        class Client:
            def __init__(self):
                self.closes = 0

            def close(self):
                self.closes += 1

        clients = (Client(), Client(), Client())
        runtime = ObsidianProcessRuntime(None, *clients)
        with patch("obs_chat_bot.bootstrap.ChannelMessageProcessor", side_effect=ValueError("registry")):
            with self.assertRaisesRegex(ValueError, "registry"):
                with create_channel_message_processor(
                    database_path=self.database_path, runtime=runtime,
                ):
                    self.fail("Процесс не должен быть передан вызывающему коду")
        self.assertEqual(tuple(client.closes for client in clients), (1, 1, 1))

    def test_process_chunker_is_reused_for_separate_obsidian_operations(self) -> None:
        """Операции получают один неизменяемый chunker из процессного runtime."""
        with patch("obs_chat_bot.capabilities.obsidian.bootstrap.create_vault_note_chunker",
                   wraps=create_vault_note_chunker) as factory:
            runtime = create_obsidian_process_runtime(
                database_path=self.database_path, github_app_config=None,
            )
            with create_channel_message_processor(
                database_path=self.database_path,
                runtime=runtime,
                openai_base_url="https://example.com/v1",
                openai_api_key="test", openai_model="test",
                use_case_factory=lambda _connection: self.article,
                analysis_use_case_factory=lambda _connection: self.analysis,
            ) as processor:
                self.assertIn("Режим Obsidian", processor(self.event("/obsidian")).text)
                self.assertIn("Режим Obsidian", processor(self.event("/obsidian")).text)
            self.assertEqual(factory.call_count, 1)

    def test_parallel_events_do_not_share_obsidian_connection(self) -> None:
        """Два вызова одного зарегистрированного входа имеют разные SQLite scopes."""
        barrier = Barrier(2)
        guard = Lock()
        connections = []

        def article_factory(connection):
            """Удерживает два одновременных вызова внутри их собственных scopes."""
            with guard:
                connections.append(connection)
            barrier.wait(timeout=3)
            connection.execute("SELECT 1")
            return self.article

        with create_channel_message_processor(
            database_path=self.database_path,
            use_case_factory=article_factory,
            analysis_use_case_factory=lambda _connection: self.analysis,
        ) as processor, ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(processor, self.event("/obsidian")) for _ in range(2)]
            for call in calls:
                self.assertIn("Режим Obsidian", call.result(timeout=5).text)
        self.assertEqual(len(connections), 2)
        self.assertIsNot(connections[0], connections[1])

    def test_no_mode_and_exit_do_not_run_article_or_llm_after_restart(self) -> None:
        """Смена соединения сохраняет отсутствие режима и явный выход."""
        for text in ("https://example.com/article", "текст"):
            self.assertIn("/obsidian", self.process(text).text)
        self.process("/obsidian")
        with connect_database(self.database_path) as connection:
            self.assertEqual(SQLiteChatModeRepository(connection).get(self.key).capability_id, "obsidian")
        self.process("/exit")
        for text in ("/start", "/register", "https://example.com/article"):
            self.process(text)
        with connect_database(self.database_path) as connection:
            self.assertIsNone(SQLiteChatModeRepository(connection).get(self.key))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM incoming_messages").fetchone()[0], 0)
        self.assertFalse(self.article.commands)
        self.assertFalse(self.analysis.commands)

    def test_disabled_obsidian_preserves_article_and_mode_without_building_entry(self) -> None:
        """Отключённый вход не собирает внешние зависимости и не удаляет свои данные."""
        with connect_database(self.database_path) as connection:
            SQLiteChatModeRepository(connection).save(ChatMode(self.key, "obsidian"))
            saved = SQLiteArticleRepository(connection).create(Article("https://example.com/article", "https://example.com/article", self.user.id))
        with patch("obs_chat_bot.capabilities.obsidian.bootstrap.create_obsidian_entry") as factory:
            reply = self.process("https://example.com/article", enabled_capabilities=())
            self.assertIn("отключена", reply.text)
            self.assertIn("/exit", reply.text)
            self.assertIn("/help", self.process("/help", enabled_capabilities=()).text)
            factory.assert_not_called()
        with connect_database(self.database_path) as connection:
            self.assertEqual(SQLiteChatModeRepository(connection).get(self.key).capability_id, "obsidian")
            self.assertEqual(SQLiteArticleRepository(connection).find_by_normalized_url(saved.normalized_url, app_user_id=self.user.id), saved)
        self.process("/exit", enabled_capabilities=())
        with connect_database(self.database_path) as connection:
            self.assertIsNone(SQLiteChatModeRepository(connection).get(self.key))
            self.assertIsNotNone(SQLiteArticleRepository(connection).find_by_normalized_url(saved.normalized_url, app_user_id=self.user.id))

    def test_obsidian_argument_reports_missing_github_provider(self) -> None:
        """Аргумент /obsidian входит в возможность и проверяет её провайдера."""
        with connect_database(self.database_path) as connection:
            SQLiteChatModeRepository(connection).save(ChatMode(self.key, "old"))
        reply = self.process("/obsidian https://example.com/article")
        self.assertIsNone(reply.error)
        self.assertIn("Провайдер GitHub не настроен", reply.text)
        with connect_database(self.database_path) as connection:
            self.assertEqual(SQLiteChatModeRepository(connection).get(self.key).capability_id, "obsidian")
        self.assertFalse(self.article.commands)

    def test_device_flow_completion_uses_fresh_connection_after_exit(self) -> None:
        """Фоновый поток завершает исходный сценарий после закрытия соединения и /exit."""
        starter = FakeGitHubConnectionStarter()
        manager = FakeVaultSelectionManager(error=GitHubAccountNotConnectedError("not authorized"))
        runtime = ObsidianProcessRuntime(starter, object())
        connections = []
        delivered = []

        def article_factory(connection):
            """Наблюдает жизненный цикл каждого собранного SQLite-соединения."""
            connections.append(connection)
            connection.execute("SELECT 1")
            return self.article

        self.process("/obsidian")
        with patch("obs_chat_bot.capabilities.obsidian.bootstrap.create_vault_selection_manager", return_value=manager):
            started = process_channel_incoming_message(
                database_path=self.database_path,
                incoming_message=self.event("https://github.com/octocat/notes Vault"),
                openai_base_url="https://example.com/v1", openai_api_key="test", openai_model="test",
                runtime=runtime,
                use_case_factory=article_factory,
                analysis_use_case_factory=lambda _connection: self.analysis,
                completion_handler=delivered.append,
            )
            self.assertIn("ABCD-EFGH", started.text)
            original = connections[0]
            with self.assertRaises(sqlite3.ProgrammingError):
                original.execute("SELECT 1")
            self.process("/exit")
            manager.error = None
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(starter.completion_handler, GitHubConnectionCompletion(GitHubConnectionCompletionStatus.CONNECTED, installation_count=1, account_login="octocat")).result()
        self.assertEqual(len(connections), 2)
        self.assertIsNot(connections[0], connections[1])
        self.assertEqual(manager.calls[-1], (self.user.id, "https://github.com/octocat/notes", "Vault"))
        self.assertIn("octocat/notes", delivered[-1].response.text)
        self.assertTrue(all(reply.address == self.event("текст").reply_address for reply in delivered))
        with connect_database(self.database_path) as connection:
            self.assertIsNone(SQLiteChatModeRepository(connection).get(self.key))

    def test_initial_name_completes_profile_without_mode(self) -> None:
        """Первое имя завершает профиль и не выбирает Obsidian."""
        with connect_database(self.database_path) as connection:
            user = create_user_identity_service(connection).register(IncomingIdentity("vk", "new", "peer"))
        common = dict(database_path=self.database_path, openai_base_url="https://example.com/v1", openai_api_key="test", openai_model="test", use_case_factory=lambda _connection: self.article, analysis_use_case_factory=lambda _connection: self.analysis)
        reply = process_channel_incoming_message(incoming_message=IncomingEvent("vk", "peer", "1", "/name Имя", "new"), **common)
        self.assertIn("Приятно познакомиться", reply.text)
        with connect_database(self.database_path) as connection:
            self.assertIsNone(SQLiteChatModeRepository(connection).get(ChatModeKey(user.id, "vk", "peer")))


class ObsidianEntryStateTest(unittest.TestCase):
    """Проверяет актуальное состояние возможности при повторном явном входе."""

    def test_enter_reports_pending_review_without_confirmation(self) -> None:
        """Вход напоминает о proposal без его применения или отмены."""
        confirmation = FakeConfirmationService(
            replace(_proposal(), origin=PendingOrigin("telegram", "chat"))
        )
        handler = ObsidianMessageHandler(
            article_url_use_case=FakeArticleUrlUseCase(), obsidian_confirmation_service=confirmation
        )
        response = handler.enter(
            HandlerContext(AppUser(42, display_name="Тест")),
            IncomingEvent("telegram", "chat", "1", "/obsidian"),
            "",
        ).response
        self.assertIn("да", response.text)
        self.assertIn("нет", response.text)
        self.assertEqual(confirmation.confirm_calls, 0)
        self.assertEqual(confirmation.cancel_calls, 0)

    def test_enter_refreshes_vault_and_pending_actions_without_executing_them(self) -> None:
        """Повторный вход читает новое состояние vault и сохранённых действий."""
        manager = FakeVaultSelectionManager(selected=_test_vault(42))
        handler = ObsidianMessageHandler(
            article_url_use_case=FakeArticleUrlUseCase(), vault_selection_manager=manager
        )
        event = IncomingEvent("telegram", "chat", "1", "/obsidian")
        context = HandlerContext(AppUser(42, display_name="Тест"))
        self.assertIn("octocat/notes", handler.enter(context, event, "").response.text)
        manager.selected = replace(manager.selected, repository="changed")
        manager.pending_origin = PendingOrigin("telegram", "chat")
        manager.disconnect_pending = True
        response = handler.enter(context, event, "").response
        self.assertIn("octocat/changed", response.text)
        self.assertIn("отключения", response.text)
        self.assertEqual(len(response.actions), 2)
        self.assertTrue(manager.disconnect_pending)
        self.assertFalse(manager.replacement_pending)


if __name__ == "__main__":
    unittest.main()
