"""Проверяет временные ожидания через общий presentation-контракт."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from compadre_assistant.bootstrap import ChannelMessageProcessor
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.presentation.incoming.action_selection import ActionContext, ResponseActionStore
from compadre_assistant.core.presentation.incoming.deferred_actions import DeferredActions
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
        self.work = None

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
        return PendingInputResult(IncomingResponse(text_content('Результат'), self.error), self.outcome,
                                  cancellable_action=self.work)


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
        self.assertIn('истёк', response_text(self.dispatcher.execute(_event('поздно'))))
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
        self.assertIn('нечего отменять', response_text(self.dispatcher.execute(_event('/cancel'))))

    def test_pre_registration_storage_and_cancel(self):
        self.identity.user = None
        pending = self.store.begin(_event('x').reply_address, 'core', 'register_name')
        self.dispatcher.execute(_event('/help'))
        self.assertEqual(self.store.get(pending.chat_key), pending)
        self.dispatcher.execute(_event('/cancel'))
        self.assertIsNone(self.store.get(pending.chat_key))

    def test_registration_race_reports_existing_name(self):
        """Победившая регистрация не выдаётся за сохранение другого введённого имени."""
        self.identity.register_with_name = Mock(return_value=self.identity.user)
        pending = self.store.begin(_event('x').reply_address, 'core', 'register_name')
        reply = response_text(self.dispatcher.execute(_event('Другое имя')))
        self.assertIn('уже зарегистрирован', reply)
        self.assertNotIn('Регистрация завершена', reply)
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


class DeferredInputTest(unittest.TestCase):
    """Проверяет квитанцию, допуск и отмену через общий вход ядра."""

    def setUp(self):
        self.actions = DeferredActions(capacity=2)
        self.addCleanup(self.actions.close)
        self.store = PendingInputStore()
        self.identity = FakeIdentity()
        self.identity.resolve_without_touch = Mock(side_effect=self.identity.resolve)
        self.owner = InputCapability()
        self.mode = FakeCapability('mode', '/mode')
        self.modes = MemoryChatModes()
        self.preferences = MemoryCapabilityPreferences()
        self.delivered = Mock()
        self.dispatcher = IncomingMessageDispatcher(
            registry=CapabilityRegistry((self.owner, self.mode)),
            user_identity_service=self.identity, chat_mode_repository=self.modes,
            capability_preference_repository=self.preferences, pending_inputs=self.store,
            deferred_actions=self.actions, deferred_access=self.may_finish,
        )
        self.owner.work = Mock(return_value=IncomingResponse(text_content("Текст")))

    def may_finish(self, *args):
        return self.dispatcher.may_finish_deferred(*args)

    def send(self, text, *, chat='chat-1', author='42'):
        return self.dispatcher.execute(self.event(text, chat=chat, author=author), self.delivered)

    def event(self, text, *, chat='chat-1', author='42'):
        return replace(_event(text, chat), external_user_id=author)

    def begin(self, *, chat='chat-1'):
        self.send('/selected', chat=chat)
        return self.send('значение', chat=chat)

    def test_waits_for_receipt_rejects_busy_commands_and_does_not_claim_button(self):
        origin = self.event('значение')
        callbacks = ResponseActionStore()
        button, = callbacks.register(origin, ActionContext(23, None, 0, (), None), (('X', '/mode', None, None),))
        receipt = self.begin()
        self.assertIsNone(self.store.get(origin.reply_address))
        self.owner.work.assert_not_called()
        for text in ('/exit', '/disable selected', '/help', 'ещё один ввод'):
            self.assertIn('Обработка уже идёт', response_text(self.send(text)))
        self.assertIn('Обработка уже идёт', response_text(
            self.dispatcher.select_action(origin, button.token, callbacks, self.delivered),
        ))
        self.assertIsNotNone(callbacks.claim(origin, button.token))
        self.assertIn('Обработка уже идёт', response_text(self.send('/cancel', author='43')))
        self.assertIsNotNone(self.actions.get(origin.reply_address))
        self.assertIn('без аргументов', response_text(self.send('/cancel аргумент')))
        self.assertIn('Обработка отменена', response_text(self.send('/cancel')))
        receipt.after_delivery(True)
        self.owner.work.assert_not_called()
        self.delivered.assert_not_called()
        self.assertIsNone(self.actions.get(origin.reply_address))

    def test_cancelled_work_and_late_finish_do_not_clear_new_generation(self):
        started, release, evaluated = Event(), Event(), Event()

        def work(cancelled):
            started.set()
            release.wait(5)
            if cancelled():
                evaluated.set()
            return IncomingResponse(text_content("Отменённый ответ"))

        self.owner.work = work
        first = self.begin()
        first.after_delivery(True)
        try:
            self.assertTrue(started.wait(5))
            self.assertIn('Обработка отменена', response_text(self.send('/cancel')))
            second = self.begin()
            current = self.actions.get(self.event('x').reply_address)
        finally:
            release.set()
        self.assertTrue(evaluated.wait(5))
        self.assertIs(self.actions.get(current.address), current)
        second.after_delivery(False)
        self.actions.close()
        self.delivered.assert_not_called()

    def test_capacity_retains_pending_until_receipt_can_be_delivered(self):
        first, second = self.begin(), self.begin(chat='chat-2')
        self.send('/selected', chat='chat-3')
        address = self.event('x', chat='chat-3').reply_address
        pending = self.store.get(address)
        self.assertIn('занята', response_text(self.send('значение', chat='chat-3')))
        self.assertIs(self.store.get(address), pending)
        first.after_delivery(False)
        third = self.send('значение', chat='chat-3')
        self.assertIsNotNone(third.after_delivery)
        self.assertIsNone(self.store.get(address))
        second.after_delivery(False)
        third.after_delivery(False)
        self.owner.work.assert_not_called()

    def test_finish_rechecks_access_removes_work_and_delivers_outside_chat_lock(self):
        checked, released = Event(), Event()

        def delivered(_reply):
            checked.set()
            released.wait(5)

        self.delivered.side_effect = delivered
        receipt = self.begin()
        receipt.after_delivery(True)
        try:
            self.assertTrue(checked.wait(5))
            self.assertIn('нечего отменять', response_text(self.send('/cancel')))
            self.assertIsNone(self.actions.get(self.event('x').reply_address))
        finally:
            released.set()
        self.owner.work.assert_called_once()

    def test_revoked_user_cannot_cancel_or_receive_a_result(self):
        receipt = self.begin()
        origin = self.event('x').reply_address
        self.identity.user = None
        self.assertIn('Обработка уже идёт', response_text(self.send('/cancel')))
        checked = Event()
        original = self.identity.resolve_without_touch.side_effect

        def lookup(identity):
            checked.set()
            return original(identity)

        self.identity.resolve_without_touch.side_effect = lookup
        receipt.after_delivery(True)
        self.assertTrue(checked.wait(5))
        self.actions.close()
        self.assertIsNone(self.actions.get(origin))
        self.delivered.assert_not_called()

    def test_processor_rechecks_identity_after_sqlite_scope_closes(self):
        """Снятая связь скрывает результат; новая SQLite-сессия не оживляет identity."""
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / 'app.db'
            with connect_database(database) as connection:
                apply_migrations(connection)
            processor = ChannelMessageProcessor(database_path=database, capabilities=(self.owner,))
            started, release, checked = Event(), Event(), Event()
            actual_check = processor._deferred_access

            def access(*args):
                try:
                    return actual_check(*args)
                finally:
                    checked.set()

            processor._deferred_access = access

            def work(_cancelled):
                started.set()
                release.wait(5)
                return IncomingResponse(text_content('Результат'))

            self.owner.work = work
            def send(text):
                return processor(self.event(text, chat='42'), self.delivered)

            try:
                send('/register')
                send('Имя')
                send('/selected')
                receipt = send('значение')
                receipt.after_delivery(True)
                self.assertTrue(started.wait(5))
                with connect_database(database) as connection:
                    with connection:
                        self.assertEqual(connection.execute('DELETE FROM external_identities').rowcount, 1)
                release.set()
                self.assertTrue(checked.wait(5))
            finally:
                release.set()
                processor.close_deferred_actions()
            self.delivered.assert_not_called()
            with connect_database(database) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM external_identities').fetchone()[0], 0)
