"""Фоновый расчёт и запись наблюдений в пределах одного процесса."""

from collections import deque
import logging
from threading import Condition, Thread

from compadre_assistant.capabilities.ask.domain.usage import UsageObservation, UsageRecorder
from compadre_assistant.capabilities.ask.domain.usage_pricing import UsageEstimateWriter, UsagePricing


_logger = logging.getLogger(__name__)
# Ограничивает память при замедлении хранилища, не включая текущую запись.
_DEFAULT_MAX_PENDING = 1024


class BackgroundUsageRecorder(UsageRecorder):
    """Обрабатывает принятую очередь в одном фоновом потоке.

    Владелец вызывает close после завершения всех отправителей наблюдений.

    Args:
        pricing: Расчёт стоимости по каталогу тарифов.
        writer: Хранилище результата с дедупликацией по ключу вызова.
        max_pending: Максимум ожидающих наблюдений помимо обрабатываемого.

    Raises:
        ValueError: Размер очереди не является положительным целым числом.
    """

    def __init__(
        self,
        *,
        pricing: UsagePricing,
        writer: UsageEstimateWriter,
        max_pending: int = _DEFAULT_MAX_PENDING,
    ) -> None:
        """Создаёт пустую очередь и запускает поток записи."""
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("Размер очереди наблюдений должен быть положительным целым числом.")
        self._pricing = pricing
        self._writer = writer
        self._max_pending = max_pending
        self._pending: deque[UsageObservation] = deque()
        self._ready = Condition()
        self._closed = False
        self._thread = Thread(target=self._run, name="ask-usage-recorder", daemon=True)
        self._thread.start()

    def submit(self, observation: UsageObservation) -> None:
        """Ставит наблюдение в очередь без ожидания записи или свободного места.

        Args:
            observation: Метаданные обращения. При переполнении очереди или
                после закрытия новое наблюдение отбрасывается с диагностикой.
        """
        with self._ready:
            if self._closed:
                reason = "closed"
            elif len(self._pending) >= self._max_pending:
                reason = "queue_full"
            else:
                self._pending.append(observation)
                self._ready.notify()
                return
        _logger.warning("AI usage observation dropped: reason=%s", reason)

    def close(self) -> None:
        """Прекращает приём и дожидается попытки записи всех принятых наблюдений.

        Повторный вызов безопасен. Ошибки отдельных записей видны в журнале.
        """
        with self._ready:
            self._closed = True
            self._ready.notify()
        self._thread.join()

    def _run(self) -> None:
        while True:
            with self._ready:
                while not self._pending:
                    if self._closed:
                        return
                    self._ready.wait()
                observation = self._pending.popleft()
            try:
                self._writer.store(self._pricing.estimate(observation))
            except Exception as error:
                # Текст исключения может содержать исходные данные или путь к БД.
                _logger.error("AI usage recording failed: error_type=%s", type(error).__name__)
