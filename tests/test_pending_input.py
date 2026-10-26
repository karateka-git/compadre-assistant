"""Проверяет временные ожидания через общий presentation-контракт."""

from datetime import datetime, timedelta, timezone
import unittest
from concurrent.futures import ThreadPoolExecutor

from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityResult, CapabilityMode, IncomingResponse, PendingInputContinuationPort,
    PendingInputOutcome, PendingInputResult,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.message_content import text_content
from tests.test_incoming_boundary import FakeCapability, FakeIdentity, MemoryChatModes, _event
from tests.capability_preference_helpers import MemoryCapabilityPreferences
from tests.response_assertions import response_text


class InputCapability(FakeCapability, PendingInputContinuationPort):
    """Создаёт ожидание тем же публичным путём, что и реальные producers."""

    def __init__(self):
        super().__init__()
        self.outcome = PendingInputOutcome.COMPLETE
        self.error = None
        self.raise_error = False
        self.replace = False
        self.values = []

    def enter(self, context, event, arguments):
        """Начинает одноразовый ввод без смены сохранённого режима."""
        context.pending_inputs.begin(event.reply_address, self.descriptor.capability_id, 'value')
        return CapabilityResult(IncomingResponse(text_content('Введи значение')), CapabilityMode.COMPLETE)

    def continue_input(self, context, pending, event):
        """Сохраняет сырой event и моделирует решение предметной валидации."""
        self.values.append(event)
        if self.replace:
            context.pending_inputs.begin(event.reply_address, self.descriptor.capability_id, 'next')
        if self.raise_error:
            raise RuntimeError('Ошибка внешнего действия')
        return PendingInputResult(IncomingResponse(text_content('Результат'), self.error), self.outcome)


class PendingInputTest(unittest.TestCase):
    """Покрывает сроки, переходы, изоляцию и повторный допуск."""

    def setUp(self):
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.store = PendingInputStore(lambda: self.now)
        self.identity = FakeIdentity()
        self.owner = InputCapability()
        self.mode = FakeCapability('mode', '/mode')
        self.modes = MemoryChatModes()
        self.preferences = MemoryCapabilityPreferences()
        self.key = ChatModeKey(23, 'telegram', 'chat-1')
        self.modes.save(ChatMode(self.key, 'mode'))
        self.dispatcher = self.make_dispatcher(self.store)

    def make_dispatcher(self, store=None):
        """Собирает новый процесс с тем же постоянным хранилищем режима."""
        return IncomingMessageDispatcher(
            registry=CapabilityRegistry((self.owner, self.mode)),
            user_identity_service=self.identity, chat_mode_repository=self.modes,
            capability_preference_repository=self.preferences, pending_inputs=store,
        )

    def start(self, chat='chat-1', channel='telegram'):
        """Создаёт ожидание командой тестовой возможности."""
        self.dispatcher.execute(_event('/selected', chat, channel))
        return self.store.get(_event('x', chat, channel).reply_address)

    def test_raw_event_complete_and_mode(self):
        self.start()
        event = _event('  исходное значение\n')
        self.dispatcher.execute(event)
        self.assertIs(self.owner.values[0], event)
        self.assertIsNone(self.store.get(event.reply_address))
        self.assertEqual(self.modes.get(self.key).capability_id, 'mode')
        self.assertFalse(self.mode.calls)

    def test_retry_keeps_original_deadline(self):
        pending = self.start()
        self.owner.outcome = PendingInputOutcome.RETRY
        self.now += timedelta(minutes=9)
        self.dispatcher.execute(_event('неверно'))
        self.assertEqual(self.store.get(pending.chat_key), pending)
        self.now += timedelta(minutes=1)
        self.assertIn('истекло', response_text(self.dispatcher.execute(_event('поздно'))))
        self.assertEqual(len(self.owner.values), 1)
        self.assertFalse(self.mode.calls)
        self.dispatcher.execute(_event('обычный маршрут'))
        self.assertEqual(len(self.mode.calls), 1)

    def test_preserving_slash_commands(self):
        for command in ('/help', '/status', '/capabilities', '/start'):
            with self.subTest(command=command):
                pending = self.start()
                self.dispatcher.execute(_event(command))
                self.assertEqual(self.store.get(pending.chat_key), pending)
        self.assertFalse(self.owner.values)

    def test_other_slashes_clear_and_follow_route(self):
        for command in ('/unknown', '/confirm', '/exit', '/mode'):
            with self.subTest(command=command):
                pending = self.start()
                self.dispatcher.execute(_event(command))
                self.assertIsNone(self.store.get(pending.chat_key))
        self.assertFalse(self.owner.values)

    def test_expired_slash_still_runs(self):
        pending = self.start()
        self.now += timedelta(minutes=10)
        self.assertIn('Профиль', response_text(self.dispatcher.execute(_event('/status'))))
        self.assertIsNone(self.store.get(pending.chat_key))

    def test_repeated_entry_replaces_deadline_and_id(self):
        first = self.start()
        self.now += timedelta(minutes=1)
        second = self.start()
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertEqual(second.expires_at - first.expires_at, timedelta(minutes=1))
        self.assertFalse(self.store.clear(first.chat_key, first.request_id))

    def test_reentrant_replacement_survives_old_completion(self):
        first = self.start()
        self.owner.replace = True
        self.dispatcher.execute(_event('следующий шаг'))
        self.assertNotEqual(self.store.get(first.chat_key).request_id, first.request_id)

    def test_cancel_isolated_by_chat_and_channel(self):
        first = self.start()
        other_chat = self.start('chat-2')
        other_channel = self.start(channel='vk')
        self.dispatcher.execute(_event('/cancel'))
        self.assertIsNone(self.store.get(first.chat_key))
        self.assertEqual(self.store.get(other_chat.chat_key), other_chat)
        self.assertEqual(self.store.get(other_channel.chat_key), other_channel)
        self.assertEqual(self.modes.get(self.key).capability_id, 'mode')
        self.assertIn('Нет активного', response_text(self.dispatcher.execute(_event('/cancel'))))

    def test_pre_registration_storage_and_cancel(self):
        self.identity.user = None
        pending = self.store.begin(_event('x').reply_address, 'core', 'register_name')
        self.dispatcher.execute(_event('/help'))
        self.assertEqual(self.store.get(pending.chat_key), pending)
        self.dispatcher.execute(_event('/cancel'))
        self.assertIsNone(self.store.get(pending.chat_key))

    def test_restart_preserves_mode_without_input(self):
        self.start()
        restarted = self.make_dispatcher()
        restarted.execute(_event('обычное сообщение'))
        self.assertEqual(len(self.mode.calls), 1)
        self.assertFalse(self.owner.values)
        self.assertEqual(self.modes.get(self.key).capability_id, 'mode')

    def test_access_rechecked_without_fallback(self):
        pending = self.start()
        self.preferences.rows[(23, 'selected')] = False
        self.assertIn('выключена', response_text(self.dispatcher.execute(_event('значение'))))
        self.assertIsNone(self.store.get(pending.chat_key))
        self.assertFalse(self.owner.values)
        self.assertFalse(self.mode.calls)

    def test_missing_identity_or_owner_clears_without_fallback(self):
        for owner in ('missing', 'mode'):
            pending = self.store.begin(_event('x').reply_address, owner, 'value')
            self.dispatcher.execute(_event('значение'))
            self.assertIsNone(self.store.get(pending.chat_key))
        self.start()
        self.identity.user = None
        self.dispatcher.execute(_event('значение'))
        self.assertFalse(self.owner.values)
        self.assertFalse(self.mode.calls)

    def test_accepted_error_and_unexpected_error_clear(self):
        for raises in (False, True):
            pending = self.start()
            self.owner.error = RuntimeError('Ошибка')
            self.owner.raise_error = raises
            self.assertIsNotNone(self.dispatcher.execute(_event('значение')).error)
            self.assertIsNone(self.store.get(pending.chat_key))

    def test_parallel_values_only_continue_once(self):
        self.start()
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(self.dispatcher.execute, [_event('a'), _event('b')]))
        self.assertEqual(len(self.owner.values), 1)
        self.assertEqual(len(self.mode.calls), 1)

    def test_clock_requires_aware_time_and_normalizes_utc(self):
        store = PendingInputStore(lambda: self.now.astimezone(timezone(timedelta(hours=6))))
        pending = store.begin(_event('x').reply_address, 'core', 'value')
        self.assertIs(pending.created_at.tzinfo, timezone.utc)
        with self.assertRaises(ValueError):
            PendingInputStore(lambda: datetime(2026, 1, 1)).begin(pending.chat_key, 'core', 'value')


    def test_help_does_not_advertise_cancel_without_pending(self):
        self.assertNotIn('/cancel', response_text(self.dispatcher.execute(_event('/help'))))

    def test_cancel_arguments_preserve_pending(self):
        pending = self.start()
        response = self.dispatcher.execute(_event('/cancel лишнее'))
        self.assertIn('без аргументов', response_text(response))
        self.assertEqual(self.store.get(pending.chat_key), pending)
