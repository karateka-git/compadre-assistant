"""Одноразовые действия ответа без зависимости от SDK и хранилищ возможностей."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from compadre_assistant.core.presentation.incoming.action_contracts import ActionScope
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    BoundReplyAction, ConfirmationAction, PendingConfirmation, ReplyAction, ResponseView, ResponseViewDelivery,
)


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Фиксирует владельца, ревизию режима, доступ, ввод и фоновую работу."""

    app_user_id: int | None
    mode_id: str | None
    mode_revision: int
    access: tuple[tuple[str, bool, bool, bool], ...]
    pending_request_id: str | None
    deferred_request_id: str | None = None


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
class _ViewOwner:
    """Ограничивает замену экранов одним профилем, чатом и возможностью."""

    origin: ReplyAddress
    external_user_id: str
    app_user_id: int
    mode_id: str


@dataclass(frozen=True, slots=True)
class _ViewScope:
    """Связывает экран с владельцем и конкретной ревизией режима."""

    owner: _ViewOwner
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
    delivered_in_parts: bool = False
    delivered_action_scope: ActionScope | None = None
    action_scope: ActionScope | None = None
    started: bool = False
    sending: bool = False
    cleanup_previous: bool = False
    retired: bool = False  # Новый экран уже доставлен, но этот ещё отправляется.


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
        self._retirements: dict[_ViewOwner, tuple[int, float]] = {}
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
        owner = self._view_owner(event, context)
        if owner is None:
            return None
        scope = _ViewScope(owner, context.mode_revision, view.key)
        with self._lock:
            self._prune()
            if view.cleanup_previous and generation <= self._retirements.get(owner, (0, 0))[0]:
                return None
            slot = self._views.get(scope)
            if slot is not None and (slot.sending or slot.retired or generation <= slot.generation):
                return None
            target = None
            if view.update:
                target = slot.delivered_message_id if slot is not None and slot.delivered_generation is not None else None
                # Переход от раздела может начаться вручную до подтверждения его списка.
                opening_screen = view.action_scope == ActionScope.SCREEN and (
                    slot is None or (slot.delivered_message_id is None and slot.action_scope == ActionScope.SECTION)
                )
                if event.callback_message_id not in (None, target) or (target is None and not opening_screen):
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
            slot.cleanup_previous = view.cleanup_previous
            slot.action_scope = view.action_scope
            target_has_parts = target is not None and slot.delivered_in_parts
            section_transition = (target is not None and slot.delivered_action_scope is not None
                                  and view.action_scope is not None and slot.delivered_action_scope != view.action_scope)
        finish_replacement = (
            lambda message_id, multipart: self._finish_view(
                scope, generation, target, message_id, view.on_delivered, replacement=True, multipart=multipart,
            )
        ) if view.allow_multiple_messages or target_has_parts or section_transition else None
        delivery = ResponseViewDelivery(
            target, view.unavailable,
            lambda: self._start_view(scope, generation),
            lambda message_id: self._finish_view(scope, generation, target, message_id, view.on_delivered),
            allow_multiple_messages=view.allow_multiple_messages,
            target_has_multiple_messages=target_has_parts,
            finish_replacement=finish_replacement,
            section_transition=section_transition,
        )
        return PreparedResponseView(scope, generation, delivery)

    def _start_view(self, scope: _ViewScope, generation: int) -> bool:
        with self._lock:
            self._prune()
            slot = self._views.get(scope)
            if slot is None or slot.generation != generation or slot.started or slot.retired:
                return False
            slot.started = True
            slot.sending = True
            return True

    def _finish_view(
        self, scope: _ViewScope, generation: int, target: str | None, message_id: str | None,
        on_delivered: Callable[[], None] | None, *, replacement: bool = False, multipart: bool = False,
    ) -> tuple[str, ...]:
        with self._lock:
            slot = self._views.get(scope)
            if slot is None or slot.generation != generation or not slot.sending:
                return ()
            slot.sending = False
            slot.expires_at = self._clock() + self._ttl
            previous_id = slot.delivered_message_id
            confirmed = bool(message_id and (
                target is None or (message_id != target if replacement else message_id == target)
            ))
            if slot.retired:
                del self._views[scope]
                return tuple(dict.fromkeys(value for value in (previous_id, message_id if confirmed else None) if value))
            retired = ()
            if confirmed:
                slot.delivered_generation = generation
                slot.delivered_message_id = message_id
                slot.delivered_in_parts = multipart
                slot.delivered_action_scope = slot.action_scope
                if slot.cleanup_previous:
                    retired = self._retire_views(scope.owner, generation)
                    if previous_id is not None and previous_id != message_id:
                        retired = (*retired, previous_id)
            elif target is not None and not replacement:
                # Неопределённый результат edit требует открыть представление снова.
                slot.delivered_generation = None
        if confirmed and on_delivered is not None:
            on_delivered()
        return retired

    def retire_views(self, event: IncomingEvent, context: ActionContext, generation: int) -> tuple[str, ...]:
        """Снимает с учёта прежние отслеживаемые экраны после доставки замены.

        Args:
            event: Проверенный адрес и автор исходного события.
            context: Профиль и режим прежнего экрана.
            generation: Номер события, доставившего замену.

        Returns:
            ID сообщений, которые больше нельзя обновить как представления.
        """
        owner = self._view_owner(event, context)
        with self._lock:
            self._prune()
            return self._retire_views(owner, generation) if owner is not None else ()

    @staticmethod
    def _view_owner(event: IncomingEvent, context: ActionContext) -> _ViewOwner | None:
        if not event.external_user_id or context.app_user_id is None or context.mode_id is None:
            return None
        return _ViewOwner(event.reply_address, event.external_user_id, context.app_user_id, context.mode_id)

    def _retire_views(self, owner: _ViewOwner, generation: int) -> tuple[str, ...]:
        # Незавершённый запрос к каналу сам отдаст свой ID на очистку. Иначе
        # его edit мог бы вернуть клавиатуру уже после отдельного снятия.
        self._retirements[owner] = (max(generation, self._retirements.get(owner, (0, 0))[0]),
                                    self._clock() + self._ttl)
        if len(self._retirements) > self._limit:
            del self._retirements[next(iter(self._retirements))]
        retired = []
        for scope, slot in tuple(self._views.items()):
            if scope.owner != owner or not slot.cleanup_previous or slot.generation >= generation or slot.retired:
                continue
            if slot.sending:
                slot.retired = True
            else:
                del self._views[scope]
                if slot.delivered_message_id is not None:
                    retired.append(slot.delivered_message_id)
        return tuple(retired)

    def managed_action_owner(self, event: IncomingEvent, token: str) -> str | None:
        """Возвращает режим известной кнопки только для её исходного автора и чата."""
        with self._lock:
            self._prune()
            entry = self._entries.get(token) if isinstance(token, str) else None
            if (entry is None or entry.view is None or entry.origin != event.reply_address
                    or entry.external_user_id != event.external_user_id):
                return None
            return entry.context.mode_id

    def protects_keyboard(self, event: IncomingEvent, token: str) -> bool:
        """Проверяет, могло ли нажатие прийти с управляемой клавиатуры."""
        if event.callback_message_id is None:
            return False
        with self._lock:
            self._prune()
            action = self._entries.get(token) if isinstance(token, str) else None
            return (
                action is not None and action.view is not None and action.origin == event.reply_address
                or any(scope.owner.origin == event.reply_address and slot.delivered_message_id == event.callback_message_id
                       for scope, slot in self._views.items())
            )

    def register(self, event: IncomingEvent, context: ActionContext,
                 actions: tuple[tuple[ReplyAction, PendingConfirmation | None, ConfirmationAction | None], ...],
                 *, view: PreparedResponseView | None = None,
                 ) -> tuple[BoundReplyAction, ...]:
        """Связывает действия одного ответа с непрозрачными случайными токенами.

        Args:
            event: Проверенный исходный адрес с явным отправителем.
            context: Снимок допуска именно этого ответа.
            actions: Проверенные описания действий и необязательные адресные ожидания.
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
            for description, confirmation, action in actions:
                token = "" if description.url is not None else token_urlsafe(24)
                if token:
                    self._entries[token] = RegisteredAction(
                        response_id, event.reply_address, event.external_user_id, context,
                        description.command, self._clock() + self._ttl, confirmation, action, view,
                    )
                result.append(BoundReplyAction(
                    description.label, token, response_id, description.row,
                    description.scope, description.purpose, description.destination, description.url,
                ))
            return tuple(result)

    def claim(
        self, event: IncomingEvent, token: str, *, context: ActionContext | None = None,
        allowed_commands: frozenset[str] | None = None,
    ) -> RegisteredAction | None:
        """Атомарно принимает один выбор, обезвреживая соседние кнопки.

        Чужой автор не потребляет токен владельца. После допуска к проверке
        контекста токен не восстанавливается даже при ошибке с неизвестным исходом.
        Другие ответы с тем же адресным ожиданием также становятся неактивными.

        Args:
            event: Адрес, автор и ID фактически нажатого сообщения.
            token: Непрозрачный токен действия.
            context: Проверенный текущий контекст управляемого представления.
            allowed_commands: Команды, которые чат может принять в текущем состоянии.

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
            if allowed_commands is not None and entry.command not in allowed_commands:
                return None
            if entry.view is not None:
                slot = self._views.get(entry.view.scope)
                if (context != entry.context or slot is None or slot.sending or slot.retired
                        or slot.delivered_generation != entry.view.generation
                        or event.callback_message_id != slot.delivered_message_id):
                    return None
            # Справка читает текущий контекст. Она оставляет кнопки шага действующими.
            if entry.command == "/help" and entry.confirmation is None:
                return entry
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
        for owner, (_, expires_at) in tuple(self._retirements.items()):
            if now >= expires_at:
                del self._retirements[owner]
