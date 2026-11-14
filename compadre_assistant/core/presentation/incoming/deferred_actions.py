"""Ограничивает процессные действия, которые можно отменить после приёма ввода."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
from threading import Event, Lock

from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse
from compadre_assistant.core.presentation.message_content import text_content


_LOGGER = logging.getLogger(__name__)


@dataclass(eq=False, slots=True)
class DeferredAction:
    """Хранит одно поколение работы и исходного владельца в памяти процесса."""

    address: ReplyAddress
    app_user_id: int
    external_user_id: str | None
    cancelled: Event = field(default_factory=Event, repr=False)
    submitted: bool = False  # Исполнитель занял слот до выхода сетевого вызова.


class DeferredActions:
    """Хранит принятые действия и исполняет до четырёх работ без очереди."""

    def __init__(self, *, capacity: int = 4) -> None:
        """Создаёт пул с общим пределом для зарезервированных и начатых работ."""
        self._executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="incoming-action")
        self._capacity = capacity
        self._actions: dict[ReplyAddress, DeferredAction] = {}
        self._slots: set[DeferredAction] = set()
        self._lock = Lock()
        self._closed = False

    def get(self, address: ReplyAddress) -> DeferredAction | None:
        """Возвращает действующее поколение по адресу чата."""
        with self._lock:
            return self._actions.get(address)

    def reserve(self, address: ReplyAddress, app_user_id: int, external_user_id: str | None) -> DeferredAction | None:
        """Занимает место до отправки квитанции; None означает отсутствие свободного слота."""
        with self._lock:
            if self._closed or len(self._slots) >= self._capacity or address in self._actions:
                return None
            action = DeferredAction(address, app_user_id, external_user_id)
            self._slots.add(action)
            self._actions[address] = action
            return action

    def cancel(self, action: DeferredAction) -> bool:
        """Снимает только переданное поколение и помечает его результат недействительным."""
        with self._lock:
            return self._cancel_locked(action)

    def _cancel_locked(self, action: DeferredAction) -> bool:
        if self._actions.get(action.address) is not action:
            return False
        del self._actions[action.address]
        action.cancelled.set()
        if not action.submitted:
            self._slots.discard(action)
        return True

    def after_delivery(
        self, action: DeferredAction, delivered: bool,
        work: Callable[[Callable[[], bool]], IncomingResponse],
        finish: Callable[[IncomingResponse], None],
    ) -> None:
        """Запускает работу после успешной квитанции либо освобождает её место.

        Args:
            action: Текущее поколение чата из reserve.
            delivered: Адаптер подтвердил успешную отправку квитанции.
            work: Обработка вне блокировки с предикатом отмены.
            finish: Проверка допуска и передача результата в канал.
        """
        with self._lock:
            if self._actions.get(action.address) is not action or action.submitted:
                return
            if not delivered:
                self._cancel_locked(action)
                return
            action.submitted = True
            try:
                self._executor.submit(self._run, action, work, finish)
            except RuntimeError:
                self._cancel_locked(action)
                self._slots.discard(action)
                _LOGGER.error("Deferred input executor could not start work")

    def claim_result(self, action: DeferredAction) -> bool:
        """Освобождает чат в момент передачи результата, если отмена не успела первой."""
        with self._lock:
            if self._actions.get(action.address) is not action:
                return False
            del self._actions[action.address]
            return True

    def _run(self, action: DeferredAction, work: Callable[[Callable[[], bool]], IncomingResponse],
             finish: Callable[[IncomingResponse], None]) -> None:
        try:
            if action.cancelled.is_set():
                return
            try:
                response = work(action.cancelled.is_set)
            except Exception as error:
                _LOGGER.error("Deferred input action failed: error_type=%s", type(error).__name__)
                response = IncomingResponse(text_content("Не удалось обработать ввод. Попробуй ещё раз."))
            if not action.cancelled.is_set():
                finish(response)
        except Exception as error:
            _LOGGER.error("Deferred input reply failed: error_type=%s", type(error).__name__)
        finally:
            with self._lock:
                self._cancel_locked(action)
                self._slots.discard(action)

    def close(self) -> None:
        """Останавливает приём, отменяет текущие поколения и ждёт выхода исполнителей."""
        with self._lock:
            self._closed = True
            for action in tuple(self._actions.values()):
                self._cancel_locked(action)
        self._executor.shutdown(wait=True)
