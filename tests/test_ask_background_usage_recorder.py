"""Проверки приёма наблюдений и завершения фоновой записи расходов."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from threading import Event, get_ident
import unittest
from unittest.mock import Mock
from uuid import uuid4

from compadre_assistant.capabilities.ask.data.background_usage_recorder import BackgroundUsageRecorder
from compadre_assistant.capabilities.ask.domain.usage import UsageObservation
from compadre_assistant.capabilities.ask.domain.usage_pricing import UsageEstimateWriter, UsagePricing


_LOGGER_NAME = "compadre_assistant.capabilities.ask.data.background_usage_recorder"
_WAIT_SECONDS = 5


def _observation() -> UsageObservation:
    return UsageObservation(uuid4(), datetime.now(timezone.utc), "test-provider", "test-model", None)


class BackgroundUsageRecorderTest(unittest.TestCase):
    """Проверяет расчёт и запись вне потока отправителя без SQLite и сети."""

    def test_submit_finishes_while_writer_is_blocked_and_close_drains(self) -> None:
        """Занятая запись не задерживает отправку, а close ждёт обе записи."""
        writer_started = Event()
        release_writer = Event()
        close_started = Event()
        observations = (_observation(), _observation())
        pricing_threads = []
        writes = []
        pricing = UsagePricing()

        def estimate(observation):
            pricing_threads.append(get_ident())
            return pricing.estimate(observation)

        def store(result):
            if result.observation == observations[0]:
                writer_started.set()
                release_writer.wait(_WAIT_SECONDS)
            writes.append((result.observation, get_ident()))
            return True

        recorder = BackgroundUsageRecorder(
            pricing=Mock(spec=UsagePricing, estimate=Mock(side_effect=estimate)),
            writer=Mock(spec=UsageEstimateWriter, store=Mock(side_effect=store)),
        )
        self.addCleanup(recorder.close)

        def close():
            close_started.set()
            recorder.close()

        with ThreadPoolExecutor(max_workers=1) as caller:
            caller_thread = caller.submit(get_ident).result(timeout=_WAIT_SECONDS)
            caller.submit(recorder.submit, observations[0]).result(timeout=_WAIT_SECONDS)
            self.assertTrue(writer_started.wait(_WAIT_SECONDS))
            try:
                caller.submit(recorder.submit, observations[1]).result(timeout=_WAIT_SECONDS)
                closing = caller.submit(close)
                self.assertTrue(close_started.wait(_WAIT_SECONDS))
                with self.assertRaises(TimeoutError):
                    closing.result(timeout=0.05)
            finally:
                release_writer.set()
            closing.result(timeout=_WAIT_SECONDS)
        self.assertEqual([observation for observation, _ in writes], list(observations))
        writer_threads = [thread for _, thread in writes]
        self.assertEqual(pricing_threads, writer_threads)
        self.assertNotIn(caller_thread, writer_threads)

    def test_full_and_closed_queue_drop_only_new_observations(self) -> None:
        """Переполнение и закрытие диагностируются, принятые записи сохраняются."""
        writer_started = Event()
        release_writer = Event()
        first, second, overflow, after_close = (_observation() for _ in range(4))
        written = []

        def store(result):
            if result.observation == first:
                writer_started.set()
                release_writer.wait(_WAIT_SECONDS)
            written.append(result.observation)
            return True

        recorder = BackgroundUsageRecorder(
            pricing=UsagePricing(),
            writer=Mock(spec=UsageEstimateWriter, store=Mock(side_effect=store)),
            max_pending=1,
        )
        self.addCleanup(recorder.close)
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as logged:
            recorder.submit(first)
            try:
                self.assertTrue(writer_started.wait(_WAIT_SECONDS))
                recorder.submit(second)
                recorder.submit(overflow)
            finally:
                release_writer.set()
            recorder.close()
            recorder.submit(after_close)
        self.assertEqual(written, [first, second])
        self.assertEqual([entry.getMessage() for entry in logged.records], [
            "AI usage observation dropped: reason=queue_full",
            "AI usage observation dropped: reason=closed",
        ])

    def test_pricing_and_write_errors_leave_worker_running(self) -> None:
        """Ошибка одного обращения не раскрывает данные и не теряет следующее."""
        first, second, third = (_observation() for _ in range(3))
        pricing = UsagePricing()
        writer = Mock(spec=UsageEstimateWriter)
        writer.store.side_effect = [OSError("private storage details"), True]
        estimate = Mock(side_effect=[ValueError("private pricing details"),
                                     pricing.estimate(second), pricing.estimate(third)])
        recorder = BackgroundUsageRecorder(pricing=Mock(spec=UsagePricing, estimate=estimate), writer=writer)
        self.addCleanup(recorder.close)
        with self.assertLogs(_LOGGER_NAME, level="ERROR") as logged:
            for observation in (first, second, third):
                recorder.submit(observation)
            recorder.close()
        self.assertEqual([call.args[0] for call in writer.store.call_args_list], [
            pricing.estimate(second), pricing.estimate(third),
        ])
        self.assertEqual([entry.getMessage() for entry in logged.records], [
            "AI usage recording failed: error_type=ValueError",
            "AI usage recording failed: error_type=OSError",
        ])
        self.assertTrue(all(entry.exc_info is None for entry in logged.records))

    def test_duplicate_write_keeps_call_id_and_does_not_interrupt_queue(self) -> None:
        """Повтор доходит до хранилища с исходным ключом и не прерывает очередь."""
        first, second = _observation(), _observation()
        writer = Mock(spec=UsageEstimateWriter)
        writer.store.side_effect = [True, False, True]
        recorder = BackgroundUsageRecorder(pricing=UsagePricing(), writer=writer)
        self.addCleanup(recorder.close)
        with self.assertNoLogs(_LOGGER_NAME, level="WARNING"):
            for observation in (first, first, second):
                recorder.submit(observation)
            recorder.close()
        self.assertEqual([call.args[0].observation.call_id for call in writer.store.call_args_list], [
            first.call_id, first.call_id, second.call_id,
        ])

    def test_requires_positive_integer_queue_size(self) -> None:
        """Некорректный лимит отклоняется до запуска фонового потока."""
        for max_pending in (0, -1, 1.5, True):
            with self.subTest(max_pending=max_pending), self.assertRaises(ValueError):
                BackgroundUsageRecorder(pricing=UsagePricing(), writer=Mock(spec=UsageEstimateWriter),
                                        max_pending=max_pending)
