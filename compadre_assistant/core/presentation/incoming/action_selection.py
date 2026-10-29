"""Одноразовые действия ответа без зависимости от SDK и хранилищ возможностей."""

from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import BoundReplyAction, ConfirmationAction, PendingConfirmation


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Фиксирует владельца, ревизию режима, доступ и ожидаемый ввод ответа."""

    app_user_id: int | None
    mode_id: str | None
    mode_revision: int
    access: tuple[tuple[str, bool, bool], ...]
    pending_request_id: str | None


@dataclass(frozen=True, slots=True)
class RegisteredAction:
    """Хранит точную команду и проверяемый серверный контекст токена."""

    response_id: str
    origin: ReplyAddress
    external_user_id: str
    context: ActionContext
    command: str
    expires_at: float
    confirmation: PendingConfirmation | None = None
    action: ConfirmationAction | None = None


class ResponseActionStore:
    """Ограничивает срок и память токенов; перезапуск безопасно забывает их все."""

    def __init__(self, *, ttl_seconds: float = 600, max_entries: int = 4096,
                 clock: Callable[[], float] = monotonic) -> None:
        """Задаёт положительный TTL, предел записей и монотонные часы."""
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("TTL и предел записей должны быть положительными")
        self._ttl = ttl_seconds
        self._limit = max_entries
        self._clock = clock
        self._entries: dict[str, RegisteredAction] = {}
        self._lock = RLock()

    def register(self, event: IncomingEvent, context: ActionContext,
                 actions: tuple[tuple[str, str, PendingConfirmation | None, ConfirmationAction | None], ...],
                 ) -> tuple[BoundReplyAction, ...]:
        """Связывает действия одного ответа с непрозрачными случайными токенами.

        Args:
            event: Проверенный исходный адрес с явным отправителем.
            context: Снимок допуска именно этого ответа.
            actions: Подпись, точная команда и необязательное адресное ожидание.

        Returns:
            Контракт сериализации адаптером; команда в callback не передаётся.
        """
        if not event.external_user_id or len(actions) > self._limit:
            return ()
        with self._lock:
            self._prune()
            while len(self._entries) + len(actions) > self._limit:
                del self._entries[next(iter(self._entries))]
            response_id = token_urlsafe(16)
            result = []
            for label, command, confirmation, action in actions:
                token = token_urlsafe(24)
                self._entries[token] = RegisteredAction(
                    response_id, event.reply_address, event.external_user_id, context,
                    command, self._clock() + self._ttl, confirmation, action,
                )
                result.append(BoundReplyAction(label, token, response_id))
            return tuple(result)

    def claim(self, event: IncomingEvent, token: str) -> RegisteredAction | None:
        """Атомарно принимает один выбор, обезвреживая соседние кнопки.

        Чужой автор не потребляет токен владельца. После допуска к проверке
        контекста токен не восстанавливается даже при ошибке с неизвестным исходом.
        Другие ответы с тем же адресным ожиданием также становятся неактивными.
        """
        if not isinstance(token, str) or len(token) != 32:
            return None
        with self._lock:
            self._prune()
            entry = self._entries.get(token)
            if entry is None or entry.origin != event.reply_address or entry.external_user_id != event.external_user_id:
                return None
            for key, other in tuple(self._entries.items()):
                same_confirmation = (entry.confirmation is not None
                                     and other.origin == entry.origin
                                     and other.context.app_user_id == entry.context.app_user_id
                                     and other.confirmation == entry.confirmation)
                if other.response_id == entry.response_id or same_confirmation:
                    del self._entries[key]
            return entry

    def _prune(self) -> None:
        now = self._clock()
        for token, entry in tuple(self._entries.items()):
            if now >= entry.expires_at:
                del self._entries[token]
