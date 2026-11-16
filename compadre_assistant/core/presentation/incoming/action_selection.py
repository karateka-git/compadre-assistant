"""Одноразовые действия ответа без зависимости от SDK и хранилищ возможностей."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    BoundReplyAction, ConfirmationAction, PendingConfirmation, ResponseView, ResponseViewDelivery,
)


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Фиксирует владельца, ревизию режима, доступ и ожидаемый ввод ответа."""

    app_user_id: int | None
    mode_id: str | None
    mode_revision: int
    access: tuple[tuple[str, bool, bool, bool], ...]
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
    view: PreparedResponseView | None = None


@dataclass(frozen=True, slots=True)
class _ViewScope:
    """Отделяет представления разных владельцев, чатов и ревизий."""

    origin: ReplyAddress
    external_user_id: str
    app_user_id: int
    mode_id: str
    mode_revision: int
    key: str


@dataclass(frozen=True, slots=True)
class PreparedResponseView:
    """Связывает поколение кнопок с одной попыткой доставки."""

    scope: _ViewScope
    generation: int
    delivery: ResponseViewDelivery


@dataclass(slots=True)
class _ViewSlot:
    """Хранит отдельно запланированную и подтверждённую доставку."""

    generation: int
    expires_at: float
    delivered_generation: int | None = None
    delivered_message_id: str | None = None
    started: bool = False
    sending: bool = False


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
        self._views: dict[_ViewScope, _ViewSlot] = {}
        self._generation = 0
        self._lock = RLock()

    def event_generation(self) -> int:
        """Выдаёт порядковый номер до вызова обработчика события."""
        with self._lock:
            self._generation += 1
            return self._generation

    def prepare_view(
        self, event: IncomingEvent, context: ActionContext, generation: int, view: ResponseView,
    ) -> PreparedResponseView | None:
        """Готовит одну публикацию или замену подтверждённого сообщения.

        Args:
            event: Проверенный адрес, автор и при нажатии ID сообщения с кнопкой.
            context: Контекст активной возможности после повторной проверки.
            generation: Выданное событию поколение до вызова обработчика.
            view: Запрошенное представление возможности.

        Returns:
            Резерв доставки и поколение кнопок либо None при устаревшей
            операции, недоступном исходном сообщении или занятой доставке.
        """
        if not event.external_user_id or context.app_user_id is None or context.mode_id is None:
            return None
        scope = _ViewScope(
            event.reply_address, event.external_user_id, context.app_user_id,
            context.mode_id, context.mode_revision, view.key,
        )
        with self._lock:
            self._prune()
            slot = self._views.get(scope)
            if slot is not None and (slot.sending or generation <= slot.generation):
                return None
            target = None
            if view.update:
                target = slot.delivered_message_id if slot is not None else None
                if target is None or event.callback_message_id not in (None, target):
                    return None
            if slot is None:
                if len(self._views) >= self._limit:
                    candidate = next((item for item, state in self._views.items() if not state.sending), None)
                    if candidate is None:
                        return None
                    del self._views[candidate]
                slot = self._views[scope] = _ViewSlot(generation, self._clock() + self._ttl)
            slot.generation = generation
            slot.expires_at = self._clock() + self._ttl
            slot.started = False
        delivery = ResponseViewDelivery(
            target, view.unavailable,
            lambda: self._start_view(scope, generation),
            lambda message_id: self._finish_view(scope, generation, target, message_id, view.on_delivered),
        )
        return PreparedResponseView(scope, generation, delivery)

    def _start_view(self, scope: _ViewScope, generation: int) -> bool:
        with self._lock:
            self._prune()
            slot = self._views.get(scope)
            if slot is None or slot.generation != generation or slot.started:
                return False
            slot.started = True
            slot.sending = True
            return True

    def _finish_view(
        self, scope: _ViewScope, generation: int, target: str | None, message_id: str | None,
        on_delivered: Callable[[], None] | None,
    ) -> None:
        with self._lock:
            slot = self._views.get(scope)
            if slot is None or slot.generation != generation or not slot.sending:
                return
            slot.sending = False
            slot.expires_at = self._clock() + self._ttl
            if message_id and (target is None or target == message_id):
                slot.delivered_generation = generation
                slot.delivered_message_id = message_id
            elif target is not None:
                # Неопределённый результат edit требует открыть представление снова.
                slot.delivered_generation = None
                slot.delivered_message_id = None
                return
        if message_id and on_delivered is not None:
            on_delivered()

    def protects_keyboard(self, event: IncomingEvent, token: str) -> bool:
        """Проверяет, могло ли нажатие прийти с управляемой клавиатуры."""
        if event.callback_message_id is None:
            return False
        with self._lock:
            self._prune()
            action = self._entries.get(token) if isinstance(token, str) else None
            return (
                action is not None and action.view is not None and action.origin == event.reply_address
                or any(scope.origin == event.reply_address and slot.delivered_message_id == event.callback_message_id
                       for scope, slot in self._views.items())
            )

    def register(self, event: IncomingEvent, context: ActionContext,
                 actions: tuple[tuple[str, str, PendingConfirmation | None, ConfirmationAction | None], ...],
                 *, view: PreparedResponseView | None = None,
                 ) -> tuple[BoundReplyAction, ...]:
        """Связывает действия одного ответа с непрозрачными случайными токенами.

        Args:
            event: Проверенный исходный адрес с явным отправителем.
            context: Снимок допуска именно этого ответа.
            actions: Подпись, точная команда и необязательное адресное ожидание.
            view: Поколение управляемого ответа, требующее подтверждения доставки.

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
                    command, self._clock() + self._ttl, confirmation, action, view,
                )
                result.append(BoundReplyAction(label, token, response_id))
            return tuple(result)

    def claim(
        self, event: IncomingEvent, token: str, *, context: ActionContext | None = None,
    ) -> RegisteredAction | None:
        """Атомарно принимает один выбор, обезвреживая соседние кнопки.

        Чужой автор не потребляет токен владельца. После допуска к проверке
        контекста токен не восстанавливается даже при ошибке с неизвестным исходом.
        Другие ответы с тем же адресным ожиданием также становятся неактивными.

        Args:
            event: Адрес, автор и ID фактически нажатого сообщения.
            token: Непрозрачный токен действия.
            context: Проверенный текущий контекст управляемого представления.

        Returns:
            Действие после одноразового допуска либо None.
        """
        if not isinstance(token, str) or len(token) != 32:
            return None
        with self._lock:
            self._prune()
            entry = self._entries.get(token)
            if entry is None or entry.origin != event.reply_address or entry.external_user_id != event.external_user_id:
                return None
            if entry.view is not None:
                slot = self._views.get(entry.view.scope)
                if (context != entry.context or slot is None or slot.sending
                        or slot.delivered_generation != entry.view.generation
                        or event.callback_message_id != slot.delivered_message_id):
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
        for scope, slot in tuple(self._views.items()):
            if not slot.sending and now >= slot.expires_at:
                del self._views[scope]
