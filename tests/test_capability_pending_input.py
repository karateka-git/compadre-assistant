"""Проверяет пошаговые входы реальных Ask и Obsidian через публичное ядро."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import Mock
import unittest

from compadre_assistant.capabilities.ask.domain.errors import AskModelTimeoutError
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from tests.capability_preference_helpers import MemoryCapabilityPreferences
from tests.test_incoming_boundary import FakeIdentity, FakeCapability, MemoryChatModes, _event
from tests.test_process_incoming_message import FakeArticleUrlUseCase, FakeAnalysisUseCase
from tests.response_assertions import response_text


class CapabilityPendingInputTest(unittest.TestCase):
    """Проверяет маршрутизацию, повторный допуск и атомарное принятие значения."""

    def setUp(self):
        """Собирает настоящие presentation-входы с независимыми внешними doubles."""
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.store = PendingInputStore(lambda: self.now)
        self.identity = FakeIdentity()
        self.model = Mock()
        self.model.answer.return_value = 'Ответ'
        self.ask = AskCapability(AskQuestion(model=self.model, max_question_chars=40))
        self.articles = FakeArticleUrlUseCase()
        self.analysis = FakeAnalysisUseCase()
        self.obsidian = ObsidianMessageHandler(
            article_url_use_case=self.articles, article_analysis_use_case=self.analysis,
            vault_selection_manager=Mock(),
        )
        self.other = FakeCapability('other', '/other')
        self.modes = MemoryChatModes()
        self.key = ChatModeKey(23, 'telegram', 'chat-1')
        self.modes.save(ChatMode(self.key, 'obsidian'))
        self.preferences = MemoryCapabilityPreferences()
        self.preferences.rows[(23, 'obsidian')] = True
        self.dispatcher = self.make_dispatcher()

    def make_dispatcher(self, enabled=None):
        """Собирает ядро с общим состоянием и выбранным серверным каталогом."""
        return IncomingMessageDispatcher(
            registry=CapabilityRegistry(enabled or (self.ask, self.obsidian, self.other)),
            user_identity_service=self.identity, chat_mode_repository=self.modes,
            capability_preference_repository=self.preferences, pending_inputs=self.store,
        )

    def send(self, text, chat='chat-1', channel='telegram'):
        """Передаёт текст по тому же публичному входу, что адаптер канала."""
        return self.dispatcher.execute(_event(text, chat, channel))

    def test_ask_retry_ttl_direct_and_mode(self):
        """Пустота и длина сохраняют срок, оба пути выполняют один вопрос."""
        self.assertIn('Введи один вопрос', response_text(self.send('/ask')))
        pending = self.store.get(_event('x').reply_address)
        self.now += timedelta(minutes=9)
        for invalid in ('  ', 'x' * 41):
            self.send(invalid)
            self.assertEqual(self.store.get(pending.chat_key), pending)
            self.model.answer.assert_not_called()
        self.assertEqual(response_text(self.send(' вопрос ')), 'Ответ')
        self.assertIsNone(self.store.get(pending.chat_key))
        self.assertEqual(self.model.answer.call_args.kwargs['question'], 'вопрос')
        self.send('/ask напрямую')
        self.assertEqual(self.model.answer.call_count, 2)
        self.assertEqual(self.modes.get(self.key).capability_id, 'obsidian')
        self.assertFalse(self.articles.commands)

    def test_ask_expiry_and_failure_do_not_repeat(self):
        """Истечение не доставляет вопрос в Obsidian, timeout закрывает ввод."""
        self.send('/ask')
        self.now += timedelta(minutes=10)
        self.assertIn('истёк', response_text(self.send('https://example.com/article')))
        self.model.answer.assert_not_called()
        self.assertFalse(self.articles.commands)
        self.send('/ask')
        self.model.answer.side_effect = AskModelTimeoutError('timeout')
        self.assertIsNotNone(self.send('вопрос').error)
        self.assertIsNone(self.store.get(_event('x').reply_address))

    def test_reanalyze_retry_then_direct_preserve_mode(self):
        """ID проверяет Obsidian, прямой и пошаговый пути сохраняют сценарий."""
        self.assertIn('числовой ID', response_text(self.send('/reanalyze')))
        pending = self.store.get(_event('x').reply_address)
        self.now += timedelta(minutes=9)
        for invalid in ('  ', 'abc', '7 8', '²', '9' * 5000):
            self.assertIn('Ожидание ID продолжается', response_text(self.send(invalid)))
            self.assertEqual(self.store.get(pending.chat_key), pending)
        self.send('7')
        self.assertEqual([c.article_id for c in self.analysis.commands], [7])
        self.assertIsNone(self.store.get(pending.chat_key))
        self.send('/reanalyze 8')
        self.assertEqual([c.article_id for c in self.analysis.commands], [7, 8])
        self.assertEqual(self.modes.get(self.key).capability_id, 'obsidian')

    def test_unavailable_article_keeps_original_input(self):
        """Чужая статья не выполняется и позволяет выбрать другую до исходного TTL."""
        self.send('/reanalyze')
        pending = self.store.get(_event('x').reply_address)
        self.articles.get_saved_for_user = Mock(return_value=None)
        self.assertIn('недоступна', response_text(self.send('7')))
        self.assertEqual(self.store.get(pending.chat_key), pending)
        self.assertFalse(self.analysis.commands)

    def test_reanalyze_requires_active_mode_at_start_and_delivery(self):
        """Смена режима без slash тоже не доставляет ожидаемый ID другому владельцу."""
        self.modes.clear(self.key)
        self.send('/reanalyze')
        self.assertIsNone(self.store.get(_event('x').reply_address))
        self.modes.save(ChatMode(self.key, 'obsidian'))
        self.send('/reanalyze')
        self.modes.save(ChatMode(self.key, 'other'))
        self.assertIn('Контекст ввода изменился', response_text(self.send('7')))
        self.assertFalse(self.analysis.commands)
        self.assertFalse(self.other.calls)
        self.assertIsNone(self.store.get(_event('x').reply_address))

    def test_linked_channel_disable_rechecks_owner(self):
        """Отключение в VK немедленно запрещает ожидаемое действие Telegram."""
        for command, owner, value in (('/ask', 'ask', 'https://example.com/article'),
                                      ('/reanalyze', 'obsidian', '7')):
            with self.subTest(owner=owner):
                self.preferences.rows[(23, owner)] = True
                self.send(command)
                self.send('/disable ' + owner, channel='vk')
                self.assertIn('выключена', response_text(self.send(value)))
                self.assertIsNone(self.store.get(_event('x').reply_address))
        self.model.answer.assert_not_called()
        self.assertFalse(self.articles.commands)

    def test_disabled_saved_mode_does_not_block_ask(self):
        """Ask завершает вопрос, даже если другой канал выключил активный Obsidian."""
        self.send('/ask')
        self.send('/disable obsidian', channel='vk')
        self.assertEqual(response_text(self.send('вопрос')), 'Ответ')
        self.assertEqual(self.modes.get(self.key).capability_id, 'obsidian')

    def test_chat_and_channel_isolation(self):
        """Одинаковый chat ID разных каналов имеет независимый ввод."""
        for chat, channel in (('chat-1', 'telegram'), ('chat-2', 'telegram'), ('chat-1', 'vk')):
            self.send('/ask', chat, channel)
        self.send('/cancel')
        self.send('второй', 'chat-2')
        self.send('третий', channel='vk')
        self.assertEqual([c.kwargs['question'] for c in self.model.answer.call_args_list], ['второй', 'третий'])
        self.assertEqual(self.modes.get(self.key).capability_id, 'obsidian')

    def test_profile_or_server_change_drops_value(self):
        """Смена профиля или удаление владельца не пересылает текст активной возможности."""
        self.send('/ask')
        self.identity.user = AppUser(id=99, display_name='Другой')
        self.assertIn('Контекст', response_text(self.send('вопрос')))
        self.identity.user = AppUser(id=23, display_name='Тест')
        self.send('/ask')
        self.dispatcher = self.make_dispatcher((self.obsidian, self.other))
        self.assertIn('недоступен', response_text(self.send('https://example.com/article')))
        self.model.answer.assert_not_called()
        self.assertFalse(self.articles.commands)

    def test_slow_actions_release_lock_and_only_consume_once(self):
        """Долгая модель/анализ не блокирует команды и не выполняет ввод дважды."""
        for command, value in (('/ask', 'вопрос'), ('/reanalyze', '7')):
            with self.subTest(command=command):
                started, release = Event(), Event()
                def slow(*args, **kwargs):
                    started.set()
                    if not release.wait(5):
                        raise TimeoutError('Действие не освобождено')
                    return 'Ответ' if command == '/ask' else FakeAnalysisUseCase().execute(*args, **kwargs)
                target = self.model.answer if command == '/ask' else Mock(side_effect=slow)
                if command == '/ask':
                    target.side_effect = slow
                else:
                    self.analysis.execute = target
                self.send(command)
                with ThreadPoolExecutor(max_workers=3) as executor:
                    future = executor.submit(self.send, value)
                    try:
                        self.assertTrue(started.wait(3))
                        self.assertIsNone(self.store.get(_event('x').reply_address))
                        self.assertIn('нечего отменять', response_text(executor.submit(self.send, '/cancel').result(1)))
                        executor.submit(self.send, value).result(1)
                        self.assertEqual(target.call_count, 1)
                    finally:
                        release.set()
                    future.result(3)
