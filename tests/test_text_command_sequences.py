"""Проверяет текстовые последовательности через процессор и настоящую SQLite."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from compadre_assistant.bootstrap import ChannelMessageProcessor
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import ObsidianProposalConfirmationService
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import SQLiteObsidianProposalRepository
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability
from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import SQLiteArticleAnalysisResultRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.processing_error_repository import SQLiteProcessingErrorRecorder
from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleUseCase
from compadre_assistant.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlUseCase
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from tests.response_assertions import response_text
from tests.test_process_incoming_message import FakeVaultSelectionManager, _test_vault
from tests.test_telegram_flow_smoke import FakeArticleHtmlFetcher, FakeArticleTextExtractor, FakeArticleAnalyzer


class TextCommandSequencesTest(unittest.TestCase):
    """Связывает общие команды, реальные use cases и изоляцию сообщений."""

    def setUp(self):
        """Создаёт БД без профилей, подменяя только сеть и выбранное хранилище."""
        directory = TemporaryDirectory(prefix="text-sequences-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "app.db"
        self.connection = self.enterContext(connect_database(self.path))
        apply_migrations(self.connection)
        self.fetcher = FakeArticleHtmlFetcher()
        self.analyzer = FakeArticleAnalyzer()
        repository = SQLiteArticleRepository(self.connection)
        errors = SQLiteProcessingErrorRecorder(self.connection)
        self.manager = FakeVaultSelectionManager(selected=_test_vault(1))
        self.confirmations = ObsidianProposalConfirmationService(
            proposal_repository=SQLiteObsidianProposalRepository(self.connection),
            vault_repository=Mock(), lease_repository=Mock(), github_gateway=Mock())
        self.obsidian = ObsidianMessageHandler(
            article_url_use_case=ProcessArticleUrlUseCase(
                article_repository=repository, html_fetcher=self.fetcher,
                text_extractor=FakeArticleTextExtractor(), error_recorder=errors),
            article_analysis_use_case=AnalyzeArticleUseCase(
                article_repository=repository, analyzer=self.analyzer,
                analysis_result_repository=SQLiteArticleAnalysisResultRepository(self.connection),
                error_recorder=errors),
            vault_selection_manager=self.manager,
            obsidian_confirmation_service=self.confirmations,
        )
        self.model = Mock()
        self.model.answer.return_value = "Ответ модели"
        self.ask = AskCapability(AskQuestion(model=self.model, max_question_chars=80))
        self.now = datetime.now(timezone.utc)
        self.sequence = 0
        self.restart()

    def restart(self, *, ask_enabled=True):
        """Пересоздаёт процессный ввод, оставляя профиль и режим в SQLite."""
        self.processor = ChannelMessageProcessor(
            database_path=self.path,
            capabilities=(self.obsidian, self.ask) if ask_enabled else (self.obsidian,),
            disabled_capabilities=() if ask_enabled else (self.ask.descriptor,),
        )
        # Управляем только временем; ввод создаётся штатными командами процессора.
        self.processor._pending_inputs._clock = lambda: self.now

    def send(self, text, channel="telegram", chat="chat", user="user"):
        """Отправляет очередное сообщение с новым ID через штатный core scope."""
        self.sequence += 1
        return self.processor(IncomingEvent(channel, chat, str(self.sequence), text, user))

    def say(self, text, **address):
        """Возвращает текст ответа на очередное сообщение."""
        return response_text(self.send(text, **address))

    def register(self, name="Первый", **address):
        """Создаёт профиль только публичной последовательностью из двух сообщений."""
        self.assertIn("Как к тебе обращаться", self.say("/register", **address))
        self.assertIn("Регистрация завершена", self.say(name, **address))

    def code(self, **address):
        """Извлекает из ответа точный код для ручной привязки другого канала."""
        match = re.search(r"/link (\S+)", self.say("/link_code", **address))
        self.assertIsNotNone(match)
        return match.group(1)

    def profile_names(self):
        """Читает сохранённый результат регистрации и переименования."""
        return [row[0] for row in self.connection.execute("SELECT display_name FROM app_users ORDER BY id")]

    def test_registration_retry_cancel_restart_and_no_implicit_name(self):
        """До принятия имени нет записи, старый обход обычным текстом отсутствует."""
        self.assertNotIn("/ask", self.say("/help"))
        self.say("/register Имя")
        self.assertEqual(self.profile_names(), [])
        self.say("/register")
        self.assertIn("/cancel", self.say("https://example.com"))
        self.assertEqual(self.profile_names(), [])
        self.assertIn("Регистрация не завершена", self.say("/help"))
        self.assertEqual(self.say("/cancel"), "Ввод отменён. Активный режим этого чата не изменён.")
        self.say("Имя без ожидания")
        self.assertEqual(self.profile_names(), [])
        self.say("/register")
        self.restart()
        self.say("Имя после перезапуска")
        self.assertEqual(self.profile_names(), [])
        self.register()
        self.assertIn("уже зарегистрирован", self.say("/register"))
        self.say("Другое имя без команды")
        self.assertEqual(self.profile_names(), ["Первый"])

    def test_core_direct_and_prompted_commands_across_linked_channels(self):
        """Коды, имена и выбор возможностей сохраняются между независимыми scopes."""
        self.register()
        code = self.code()
        self.assertIn("следующим сообщением", self.say("/link", channel="vk"))
        self.assertIn("/cancel", self.say("wrong", channel="vk"))
        self.assertIn("привязал", self.say(code, channel="vk"))
        self.assertIn("уже привязан", self.say("/link " + self.code(), channel="vk"))
        self.say("/name")
        for info in ("/help", "/status", "/capabilities", "/start"):
            self.say(info)
        self.assertIn("/cancel", self.say("x" * 81))
        self.say("Новое имя")
        self.assertIn("Новое имя", self.say("/status", channel="vk"))
        self.say("/name Прямое имя", channel="vk")
        self.assertEqual(self.profile_names(), ["Прямое имя"])
        self.say("/enable")
        self.assertIn("/cancel", self.say("missing"))
        self.say("obsidian")
        self.say("/obsidian")
        self.say("/disable", channel="vk")
        self.say("obsidian", channel="vk")
        self.assertIn("недоступен", self.say("/help"))
        self.say("/enable obsidian")
        self.assertIn("Команды режима obsidian", self.say("/help"))
        self.say("/disable obsidian")
        self.assertIn("выключена", self.say("https://example.com/article"))
        self.assertFalse(self.fetcher.calls)

    def test_ask_and_reanalyze_keep_subject_pipeline_and_mode(self):
        """Оба способа ввода используют сохранённую статью, Ask не перехватывает режим."""
        self.register()
        self.say("/enable obsidian")
        self.say("/obsidian")
        self.assertIn("Анализ готов", self.say("https://example.com/article"))
        self.assertIn("Использую сохранённый анализ", self.say("https://example.com/article"))
        self.assertEqual(len(self.fetcher.calls), 1)
        self.say("/ask")
        self.assertEqual(self.say("  "), "Вопрос пуст. Отправь непустой вопрос ещё раз или введи /cancel.")
        self.assertIn("/cancel", self.say("x" * 81))
        self.assertEqual(self.say("Вопрос"), "Ответ модели")
        self.assertEqual(self.say("/ask Второй"), "Ответ модели")
        self.assertEqual(self.model.answer.call_count, 2)
        self.assertIn("числовой ID", self.say("/reanalyze"))
        self.assertIn("Ожидание ID продолжается", self.say("abc"))
        self.assertIn("недоступна", self.say("999"))
        self.assertIn("Анализ обновлён", self.say("1"))
        self.assertIn("Анализ обновлён", self.say("/reanalyze 1"))
        self.assertEqual(self.analyzer.article_ids, [1, 1, 1])
        self.assertEqual(len(self.fetcher.calls), 3)
        self.assertIn("Команды режима obsidian", self.say("/help"))
        self.say("/exit")
        self.say("/reanalyze")
        self.say("1")
        self.assertEqual(self.analyzer.article_ids, [1, 1, 1])

    def test_ttl_retry_cancel_and_restart_preserve_saved_mode(self):
        """TTL всех владельцев даёт выполнимую команду, перезапуск теряет только ввод."""
        self.register()
        self.say("/enable obsidian")
        self.say("/obsidian")
        for command in ("/name", "/link", "/disable", "/ask", "/reanalyze"):
            with self.subTest(command=command):
                self.say(command)
                self.now += timedelta(minutes=10)
                self.assertEqual(self.say("позднее значение"),
                    f"Срок ожидания ввода истёк. Повтори {command}, чтобы начать заново.")
                self.assertNotIn("/cancel", self.say("/help"))
        self.say("/ask")
        self.say("/cancel")
        self.assertEqual(self.say("/cancel"),
            "В этом чате нечего отменять. Продолжай текущий режим или вызови нужную команду.")
        self.say("/name")
        self.restart()
        self.assertIn("Команды режима obsidian", self.say("/help"))
        self.say("Не новое имя")
        self.assertEqual(self.profile_names(), ["Первый"])
        self.model.answer.assert_not_called()
        self.assertFalse(self.analyzer.article_ids)

    def test_isolation_replacement_and_disabled_access(self):
        """Один чат отменяется отдельно; отключение во втором канале запрещает ввод."""
        self.register()
        self.say("/link " + self.code(), channel="vk")
        self.say("/ask")
        self.say("/ask", channel="vk")
        self.say("/cancel")
        self.assertEqual(self.say("VK вопрос", channel="vk"), "Ответ модели")
        self.say("/ask")
        self.say("/disable ask", channel="vk")
        self.assertIn("выключена", self.say("Отложенный вопрос"))
        self.say("/enable ask")
        self.say("/ask")
        self.say("/unknown")
        self.say("Не вопрос")
        self.assertEqual(self.model.answer.call_count, 1)
        self.restart(ask_enabled=False)
        self.assertNotIn("/ask", self.say("/help"))
        self.assertIn("недоступна на сервере", self.say("/ask"))
        self.assertEqual(self.model.answer.call_count, 1)

    def test_identity_confirmation_full_partial_and_replayed_selection(self):
        """От кода до переноса проверяет просмотр, частичный снимок и повторный выбор."""
        self.register()
        self.register("Второй", channel="vk")
        self.say("/enable obsidian", channel="vk")
        self.say("/obsidian", channel="vk")
        self.say("/link", channel="vk")
        created = self.send(self.code(), channel="vk")
        self.assertEqual(len(created.actions), 2)
        selected = created.actions[0].command
        self.assertEqual([a.command for a in self.send("/confirm", channel="vk").actions],
                         [a.command for a in created.actions])
        self.assertFalse(self.send("/confirm").actions)
        self.say("да", channel="vk")
        self.assertIn("Второй", self.say("/status", channel="vk"))
        self.obsidian._obsidian_confirmation_service = None
        partial = self.send("/confirm", channel="vk")
        self.assertIn("Список действий неполный", response_text(partial))
        self.assertEqual([a.command for a in partial.actions], [a.command for a in created.actions])
        self.assertIsNotNone(self.send(selected).error)
        self.assertIn("перепривязал", self.say(selected, channel="vk"))
        self.assertIn("Первый", self.say("/status", channel="vk"))
        self.assertIn("устарело", self.say(selected, channel="vk"))
        self.assertEqual(self.say("/confirm", channel="vk"),
            "В этом чате нет действий, которые нужно подтвердить.")

    def test_obsidian_confirmation_short_and_addressed_from_command(self):
        """Запрос отключения показывает точный выбор; просмотр и /cancel не решают его."""
        self.register()
        self.say("/enable obsidian")
        self.say("/obsidian")
        self.say("/github_disconnect")
        choices = self.send("/confirm")
        self.assertEqual(len(choices.actions), 2)
        self.say("/cancel")
        self.assertTrue(self.manager.disconnect_pending)
        self.say("нет")
        self.assertFalse(self.manager.disconnect_pending)
        self.assertIsNotNone(self.manager.selected)
        self.say("/github_disconnect")
        command = self.send("/confirm").actions[0].command
        self.say("/exit")
        self.say(command)
        self.assertTrue(self.manager.disconnect_pending)
        self.say("/obsidian")
        self.say(command)
        self.assertFalse(self.manager.disconnect_pending)
        self.assertIsNone(self.manager.selected)
