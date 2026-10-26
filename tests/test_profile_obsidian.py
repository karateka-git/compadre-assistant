from tests.response_assertions import content_text, response_text
"""Проверки независимого профиля и явного входа в Obsidian без внешних API."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import create_incoming_message_dispatcher, create_user_identity_service, create_channel_message_processor
from channel_helpers import process_channel_incoming_message
from compadre_assistant.capabilities.obsidian.bootstrap import ObsidianProcessRuntime
from compadre_assistant.capabilities.obsidian.data.configuration.config import ObsidianConfig
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from test_process_incoming_message import FakeArticleUrlUseCase, FakeAnalysisUseCase, FakeIncomingMessageRepository, FakeVaultSelectionManager, _test_vault


class ProfileObsidianTest(unittest.TestCase):
    """Проверяет шаг 2.3 с настоящими профилями и маршрутами временной SQLite."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="profile-obsidian-")
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "app.db"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.article = FakeArticleUrlUseCase()
        self.analysis = FakeAnalysisUseCase()

    def event(self, text: str, channel: str = "telegram", chat: str = "chat") -> IncomingEvent:
        """Создаёт событие с полным адресом и данными отправителя."""
        return IncomingEvent(channel, chat, "message", text, "external", "username", "Внешнее имя")

    def process(self, text: str, *, channel: str = "telegram", chat: str = "chat", **overrides):
        """Обрабатывает сообщение через сборку с новым соединением без сети."""
        options = dict(
            database_path=self.database_path,
            incoming_message=self.event(text, channel, chat),
            obsidian_config=ObsidianConfig(
                openai_base_url="https://example.com/v1",
                openai_api_key="test", openai_model="test",
            ),
            use_case_factory=lambda _connection: self.article,
            analysis_use_case_factory=lambda _connection: self.analysis,
        )
        options.update(overrides)
        return process_channel_incoming_message(**options)

    def register(self, channel: str = "telegram", **overrides) -> None:
        """Создаёт профиль и сохраняет имя, не входя в возможность."""
        options = dict(
            database_path=self.database_path,
            obsidian_config=ObsidianConfig(
                openai_base_url="https://example.com/v1",
                openai_api_key="test", openai_model="test",
            ),
            use_case_factory=lambda _connection: self.article,
            analysis_use_case_factory=lambda _connection: self.analysis,
        )
        options.update(overrides)
        with create_channel_message_processor(**options) as processor:
            processor(self.event("/register", channel))
            reply = processor(self.event("Имя", channel))
        self.assertIn("Регистрация завершена", response_text(reply))
        with connect_database(self.database_path) as connection:
            owner = connection.execute(
                "SELECT app_user_id FROM external_identities WHERE channel = ? AND external_user_id = ?",
                (channel, "external"),
            ).fetchone()[0]
            SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                UserCapabilityPreference(owner, "obsidian", True)
            )

    def test_cancelled_or_restarted_registration_leaves_no_profile(self) -> None:
        """Отмена и перезапуск удаляют только ожидание, не создавая записи БД."""
        options = dict(database_path=self.database_path, enabled_capabilities=())
        with create_channel_message_processor(**options) as processor:
            reply = processor(self.event("/register Лишнее имя"))
            self.assertIn("без аргументов", response_text(reply))
            processor(self.event("/register"))
            processor(self.event("/cancel"))
            processor(self.event("Имя после отмены"))
            processor(self.event("/register"))
        with create_channel_message_processor(**options) as processor:
            reply = processor(self.event("Имя после перезапуска"))
            self.assertIn("Я пока не знаю", response_text(reply))
        with connect_database(self.database_path) as connection:
            for table in ("app_users", "external_identities"):
                self.assertEqual(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0], 0)

    def test_registration_and_profile_commands_work_with_disabled_obsidian(self) -> None:
        """Отключённый модуль не мешает имени, справке и связи каналов."""
        for channel in ("telegram", "vk", "cli"):
            with self.subTest(channel=channel), patch("compadre_assistant.capabilities.obsidian.bootstrap.create_obsidian_entry") as factory:
                options = dict(channel=channel, enabled_capabilities=(), obsidian_config=None)
                self.register(**options)
                self.assertIn("Рад тебя видеть, Имя", response_text(self.process("/start", **options)))
                self.assertIn("уже зарегистрирован", response_text(self.process("/register", **options)))
                self.assertIn("Профиль: Имя", response_text(self.process("/status", **options)))
                self.assertIn("Теперь буду обращаться", response_text(self.process("/name Новое имя", **options)))
                self.assertIn("Код привязки", response_text(self.process("/link_code", **options)))
                help_text = response_text(self.process("/help", **options))
                self.assertIn("/register", help_text)
                self.assertNotIn("/github_status", help_text)
                self.assertNotIn("/obsidian", help_text)
                self.assertIn("недоступна на сервере", response_text(self.process("/obsidian https://example.com/article", **options)))
                factory.assert_not_called()
        with connect_database(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_chat_modes").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM obsidian_vaults").fetchone()[0], 0)
        self.assertFalse(self.article.commands)

    def test_profile_commands_never_check_vault(self) -> None:
        """Общий профиль остаётся доступен при сбое предметного хранилища."""
        manager = FakeVaultSelectionManager()
        runtime = ObsidianProcessRuntime(None, object())
        with patch("compadre_assistant.capabilities.obsidian.bootstrap.create_vault_selection_manager", return_value=manager), patch.object(manager, "get_selected", side_effect=AssertionError("Профиль не читает vault")):
            self.register(runtime=runtime)
            for command in ("/start", "/register", "/status", "/help", "/name Новое имя", "/link_code", "/exit"):
                self.assertIsNone(self.process(command, runtime=runtime).error)
        with connect_database(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_chat_modes WHERE capability_id IS NOT NULL").fetchone()[0], 0)

    def test_channels_link_to_profile_with_disabled_obsidian(self) -> None:
        """Общая привязка Telegram/VK доступна без предметной возможности и vault."""
        self.register(enabled_capabilities=())
        with connect_database(self.database_path) as connection:
            code = create_user_identity_service(connection).create_link_code(1)
        reply = self.process(f"/link {code.code}", channel="vk", enabled_capabilities=())
        self.assertIn("привязал этот канал", response_text(reply))
        self.assertIn("Профиль: Имя", response_text(self.process("/status", channel="vk", enabled_capabilities=())))
        with connect_database(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_chat_modes").fetchone()[0], 0)

    def test_absent_llm_does_not_construct_provider_during_registration(self) -> None:
        """Пустые credentials не превращают регистрацию в ошибку SDK."""
        with patch("compadre_assistant.capabilities.obsidian.bootstrap.create_analyze_article_use_case") as analyzer, patch("compadre_assistant.capabilities.obsidian.bootstrap.create_prepare_obsidian_review_use_case") as review:
            options = dict(obsidian_config=ObsidianConfig(), analysis_use_case_factory=None)
            self.register(**options)
            self.assertIn("/obsidian", response_text(self.process("/start", **options)))
            self.assertIn("провайдер LLM не настроен", response_text(self.process("/obsidian", **options)))
            analyzer.assert_not_called()
            review.assert_not_called()

    def test_provider_and_user_vault_are_distinct(self) -> None:
        """Ненастроенный GitHub отличается от отсутствующего пользовательского vault."""
        self.register()
        no_provider = self.process("/obsidian https://example.com/article")
        self.assertIn("провайдер GitHub не настроен", response_text(no_provider))
        manager = FakeVaultSelectionManager()
        runtime = ObsidianProcessRuntime(None, object())
        with patch("compadre_assistant.capabilities.obsidian.bootstrap.create_vault_selection_manager", return_value=manager):
            no_vault = self.process("/obsidian https://example.com/article", runtime=runtime)
            self.assertIn("хранилище заметок (vault) не подключено", response_text(no_vault))
            self.assertNotIn("Провайдер", response_text(no_vault))
            self.assertIn("GitHub repository", response_text(self.process("/obsidian", runtime=runtime)))
            manager.selected = _test_vault(1)
            missing_llm = self.process(
                "/obsidian https://example.com/article", runtime=runtime,
                obsidian_config=ObsidianConfig(), analysis_use_case_factory=None,
            )
            self.assertIn("провайдер LLM не настроен", response_text(missing_llm))
        self.assertFalse(self.article.commands)
        self.assertFalse(self.analysis.commands)

    def test_entry_argument_preserves_original_event_and_runs_once(self) -> None:
        """Аргумент входа обрабатывается со всеми исходными данными каждого канала."""
        for channel in ("telegram", "vk", "cli"):
            with self.subTest(channel=channel), connect_database(self.database_path) as connection:
                self.register(channel)
                journal = FakeIncomingMessageRepository()
                handler = ObsidianMessageHandler(
                    article_url_use_case=self.article,
                    incoming_message_repository=journal,
                    vault_selection_manager=FakeVaultSelectionManager(selected=_test_vault(1)),
                )
                dispatcher = create_incoming_message_dispatcher(
                    capabilities=(handler,), user_identity_service=create_user_identity_service(connection),
                    chat_mode_repository=SQLiteChatModeRepository(connection),
                    capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
                )
                event = self.event("/obsidian Материал https://example.com/article", channel)
                with patch.object(handler, "enter", wraps=handler.enter) as enter:
                    reply = dispatcher.execute(event)
                self.assertIn("статья сохранена", response_text(reply))
                self.assertIs(enter.call_args.args[1], event)
                self.assertEqual(enter.call_args.args[2], "Материал https://example.com/article")
                saved = journal.messages[0]
                for field in ("channel", "chat_id", "message_id", "external_user_id", "username", "display_name", "text"):
                    self.assertEqual(getattr(saved, field), getattr(event, field))
                user = enter.call_args.args[0].app_user
                self.assertEqual(SQLiteChatModeRepository(connection).get(ChatModeKey(user.id, channel, "chat")).capability_id, "obsidian")
                self.assertIn("/obsidian", response_text(dispatcher.execute(replace(event, chat_id="other", text="https://example.com/article"))))
                dispatcher.execute(replace(event, text="/exit"))
                self.assertIn("/obsidian", response_text(dispatcher.execute(replace(event, text="https://github.com/octocat/notes"))))
        self.assertEqual(len(self.article.commands), 3)

    def test_github_entry_argument_connects_inside_obsidian(self) -> None:
        """GitHub URL и вложенная папка запускают самостоятельное подключение vault."""
        self.register()
        manager = FakeVaultSelectionManager()
        runtime = ObsidianProcessRuntime(None, object())
        with patch("compadre_assistant.capabilities.obsidian.bootstrap.create_vault_selection_manager", return_value=manager):
            self.assertIn("/obsidian", response_text(self.process("https://github.com/octocat/notes Vault", runtime=runtime)))
            self.assertFalse(manager.calls)
            reply = self.process("/obsidian https://github.com/octocat/notes Vault", runtime=runtime)
            self.assertIn("Хранилище подключено", response_text(reply))
            self.assertEqual(manager.calls, [(1, "https://github.com/octocat/notes", "Vault")])
        self.assertFalse(self.article.commands)

    def test_invalid_entry_argument_preserves_previous_mode(self) -> None:
        """Ошибка аргумента не заменяет сохранённый выбор текущего чата."""
        self.register()
        key = ChatModeKey(1, "telegram", "chat")
        with connect_database(self.database_path) as connection:
            SQLiteChatModeRepository(connection).save(ChatMode(key, "previous"))
        manager = FakeVaultSelectionManager(selected=_test_vault(1))
        runtime = ObsidianProcessRuntime(None, object())
        with patch("compadre_assistant.capabilities.obsidian.bootstrap.create_vault_selection_manager", return_value=manager):
            for text in ("/obsidian текст без URL", "/obsidian /reanalyze invalid"):
                self.assertIsNotNone(self.process(text, runtime=runtime).error)
                with connect_database(self.database_path) as connection:
                    self.assertEqual(SQLiteChatModeRepository(connection).get(key).capability_id, "previous")
        self.assertFalse(self.article.commands)


if __name__ == "__main__":
    unittest.main()
