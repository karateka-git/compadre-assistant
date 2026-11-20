from __future__ import annotations


from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
import logging

from compadre_assistant.core.presentation.message_content import (
    MessageContent, code_span, compose_content, confirmation_action_paragraph, text_content,
)
from compadre_assistant.core.presentation.incoming.commands import PROFILE_COMMANDS, ChatCommand, ParsedChatCommand
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, SectionMenuDelivery
from compadre_assistant.core.presentation.incoming.deferred_actions import DeferredActions
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.menu import (
    BUSY_COMMAND_MENU, HELP_SECTION_ACTION, HOME_SECTION_ACTION, build_command_menu, capability_entry_action,
    main_actions,
)
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, CancellableAction, ConfirmationAction, BackgroundReply,
    CapabilityDescriptor, CapabilityMode, ChannelCompletionHandler,
    HandlerContext, IncomingResponse, ReplyDeliveryPort, IncomingCapabilityPort,
    PendingInputContinuationPort, PendingInputOutcome, PendingInputResult,
    PendingConfirmation, ConfirmationQueryResult, PendingConfirmationQueryPort, CommandMenu,
    CommandMenuUpdate, CommandMenuUpdateHandler, ReplyAction,
    CapabilityHelp, CommandSuggestion, ContextualCommandQueryPort, HelpSection,
    ReplyActionRecoveryPort, ResponseView, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput, PendingInputStore
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.incoming.registry import PersonalCapabilityRegistry, CapabilityAccess
from compadre_assistant.core.domain.capability_preferences import (
    CapabilityDefault, GetUserCapabilitySelections, SetUserCapabilityPreference,
    UserCapabilityPreferenceRepository,
)
from compadre_assistant.core.domain.users.identity import (
    CreatedLinkCode,
    IdentityAlreadyBoundError,
    InvalidDisplayNameError,
    InvalidLinkCodeError,
    SourceProfileInUseError,
    UserIdentityService,
)
from compadre_assistant.core.domain.users.entities import AppUser, IncomingIdentity
from compadre_assistant.core.domain.users.entities import IdentityRebindConfirmation
from compadre_assistant.core.domain.chat_modes import ChatModeKey, ChatModeRepository
from compadre_assistant.core.domain.users.entities import is_private_chat_address
from compadre_assistant.core.presentation.incoming.action_selection import ActionContext, ResponseActionStore


LOGGER = logging.getLogger(__name__)
_TEXT_INPUT_HINT = "Пришли текстовое сообщение или выбери действие в меню."
_PROFILE_LINK_PRIVATE_HINT = "Для привязки профиля открой личный чат с ботом."
_HELP_SCREEN_ACTION = replace(HELP_SECTION_ACTION, scope=ActionScope.SCREEN)
_LINK_INPUT_SCREEN_ACTION = ReplyAction("Ввести код", "/link")
_CONFIRM_SCREEN_ACTION = ReplyAction("Подтверждения", "/confirm")
_BUSY_COMMANDS = frozenset((ChatCommand.CANCEL.value, ChatCommand.HELP.value))
# Только успешная отмена требует пересчитать меню из проекции занятого чата.
_DEFERRED_CANCELLED_REPLY = IncomingResponse(text_content("Обработка отменена. Активный режим этого чата не изменён."))


def _rebind_actions(pending: IdentityRebindConfirmation) -> tuple[ConfirmationAction, ...]:
    """Формирует одни адресные решения для запроса переноса, справки и /confirm."""
    return tuple(ConfirmationAction(
        f"core:identity:{pending.id}:{pending.version}:{decision}", label,
        f"/confirm core identity {pending.id} {pending.version} {decision}",
    ) for decision, label in (("yes", "Перенести аккаунт"), ("no", "Отменить перенос")))


def _capability_label(descriptor: CapabilityDescriptor) -> str:
    """Возвращает публичное название или идентификатор возможности без названия."""
    name = descriptor.display_name
    return f"«{name}»" if name is not None and not name.startswith("«") else name or descriptor.capability_id


def pending_expired_reply(pending: PendingInput) -> IncomingResponse:
    """Предлагает заново открыть ввод, когда сценарий объявил способ повтора."""
    retry = (ReplyAction("Начать заново", pending.retry_command),) if pending.retry_command else ()
    return IncomingResponse(
        text_content("Срок ожидания ввода истёк. Начни ввод заново."),
        suggested_actions=(*retry, _HELP_SCREEN_ACTION), close_managed_views=True,
    )


class IncomingMessageResultType(StrEnum):
    """Тип результата обработки входящего сообщения."""

    UNKNOWN_IDENTITY = "unknown_identity"
    START_UNREGISTERED = "start_unregistered"
    START_REGISTERED = "start_registered"
    ALREADY_REGISTERED = "already_registered"
    REGISTRATION_NAME_REQUIRED = "registration_name_required"  # Профиль ещё не создан; ожидается имя.
    REGISTRATION_NAME_SAVED = "registration_name_saved"
    NAME_COMMAND_INVALID = "name_command_invalid"
    NAME_UPDATED = "name_updated"
    CONFIRMATION_MISSING = "confirmation_missing"
    LINK_CODE_CREATED = "link_code_created"
    LINK_COMMAND_INVALID = "link_command_invalid"
    LINKED = "linked"
    LINK_ALREADY_BOUND = "link_already_bound"
    LINK_CODE_INVALID = "link_code_invalid"
    LINK_REBOUND = "link_rebound"
    LINK_REBIND_CANCELLED = "link_rebind_cancelled"
    MODE_HINT = "mode_hint"  # В этом чате возможность ещё не выбрана.
    MODE_EXITED = "mode_exited"  # Выбор очищен только для указанного чата.
    PROFILE_STATUS = "profile_status"  # Общие сведения о пользователе и выбранном режиме.
    MODE_UNAVAILABLE = "mode_unavailable"  # Сохранённый маршрут не подключён к текущему входу.
    CAPABILITY_DISABLED = "capability_disabled"  # Явная команда относится к отключённому входу.


@dataclass(frozen=True, slots=True)
class ProcessIncomingMessageResult:
    """Хранит ответ общего входа без моделей подключённых возможностей."""

    type: IncomingMessageResultType
    app_user: AppUser | None = None
    link_code: CreatedLinkCode | None = None
    error: Exception | None = None
    capability_reply: str | None = None
    personal: PersonalCapabilityRegistry | None = None


class CallbackReplyDelivery(ReplyDeliveryPort):
    """Связывает нейтральную доставку с callback канала без SQLite-зависимостей."""

    def __init__(self, handler: ChannelCompletionHandler) -> None:
        """Сохраняет только callback канала для последующей доставки."""
        self._handler = handler

    def send(self, address: ReplyAddress, response: IncomingResponse) -> None:
        """Передаёт готовый фоновый ответ и сохранённый адрес адаптеру."""
        self._handler(BackgroundReply(address, response))


class IncomingMessageDispatcher:
    """Разрешает identity, общие команды и сохранённый маршрут чата.

    Args:
        registry: Проверенный реестр включённых возможностей.
        user_identity_service: Общий сервис профиля и связей каналов.
        chat_mode_repository: Обязательное хранилище выбора конкретного чата.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        user_identity_service: UserIdentityService,
        chat_mode_repository: ChatModeRepository,
        capability_preference_repository: UserCapabilityPreferenceRepository,
        pending_inputs: PendingInputStore | None = None,
        deferred_actions: DeferredActions | None = None,
        deferred_access: Callable[[IncomingEvent, int, str, str | None], bool] | None = None,
        deferred_menu_refresh: Callable[[ReplyAddress], None] | None = None,
    ) -> None:
        """Внедряет общий реестр и repositories независимого профиля."""
        self._pending_inputs = pending_inputs or PendingInputStore()
        self._deferred_actions = deferred_actions
        self._deferred_access = deferred_access
        self._deferred_menu_refresh = deferred_menu_refresh
        self._registry = registry
        self._user_identity_service = user_identity_service
        self._chat_mode_repository = chat_mode_repository
        self._capability_preference_repository = capability_preference_repository

    def _personal_registry(self, user: AppUser, event: IncomingEvent) -> PersonalCapabilityRegistry:
        """Читает выбор один раз после разрешения AppUser для допуска события."""
        selections = GetUserCapabilitySelections(
            self._capability_preference_repository, self._capability_defaults()
        ).execute(user.id)
        # Профиль из резервного chat_id достаточен для открытых возможностей.
        explicit_user_id = user.id if event.external_user_id is not None else None
        return self._registry.for_user(
            selections,
            app_user_id=explicit_user_id,
            private_chat=is_private_chat_address(event.channel, event.external_user_id, event.chat_id),
        )

    @staticmethod
    def _profile_link_allowed(event: IncomingEvent) -> bool:
        return is_private_chat_address(
            event.channel, event.external_user_id, event.chat_id,
        )

    @staticmethod
    def _group_action_address(event: IncomingEvent) -> bool:
        """Распознаёт групповые адреса с явным положительным ID автора."""
        author = event.external_user_id or ""
        if not author.isascii() or not author.isdecimal() or author.startswith("0"):
            return False
        try:
            chat_id = int(event.chat_id)
        except ValueError:
            return False
        return ((event.channel == "telegram" and chat_id < 0)
                or (event.channel == "vk" and chat_id >= 2_000_000_000))

    def action_context(self, event: IncomingEvent) -> ActionContext:
        """Читает контекст кнопок без изменения identity по непроверенному адресу.

        Raises:
            ValueError: Нет явного автора либо личный адрес занят другой identity.
        """
        if not self._profile_link_allowed(event) and not self._group_action_address(event):
            raise ValueError("Действие требует явного автора поддерживаемого канала")
        if self._profile_link_allowed(event):
            known = self._user_identity_service.find_known_private_chat(event.channel, event.chat_id)
            if known is None:
                if self._user_identity_service.has_known_chat_address(event.channel, event.chat_id):
                    raise ValueError("Неоднозначный адрес")
                user = None
            else:
                user, identity = known
                if identity.external_user_id != event.external_user_id:
                    raise ValueError("Адрес принадлежит другой identity")
        else:
            user = self._user_identity_service.resolve_without_touch(_incoming_identity_from_event(event))
        state = self._chat_mode_repository.get_state(
            ChatModeKey(user.id, event.channel, event.chat_id)
        ) if user else None
        personal = self._personal_registry(user, event) if user else None
        pending = self._pending_for_event(event)
        deferred = self._deferred_actions.get(event.reply_address) if self._deferred_actions is not None else None
        return ActionContext(
            user.id if user else None, personal.visible_mode(state.capability_id) if state else None,
            state.revision if state else 0,
            tuple((item.descriptor.capability_id, item.server_available, item.user_enabled, item.visible)
                  for item in personal.access) if personal else (),
            pending.request_id if pending and not self._pending_inputs.expired(pending) else None,
            deferred.request_id if deferred and user and deferred.app_user_id == user.id
            and deferred.external_user_id == event.external_user_id else None,
        )

    def _pending_for_event(self, event: IncomingEvent) -> PendingInput | None:
        pending = self._pending_inputs.get(event.reply_address)
        return pending if pending is not None and pending.external_user_id in (None, event.external_user_id) else None

    def _group_action_allowed(self, command: str) -> bool:
        """Допускает команды без аргументов, уже разрешённые в группах каталогом."""
        owner = self._registry.mode_command_owner(command)
        return (command in {"/register", "/cancel", "/help", "/exit", "/status", "/name",
                            "/capabilities", "/enable", "/disable"}
                or (command.split() == [command] and (
                    self._registry.by_command(command) is not None
                    or (owner is not None and owner.private_chat_hint is None)
                )))

    def bind_response_actions(
        self, event: IncomingEvent, response: IncomingResponse, store: ResponseActionStore, *,
        view_origin: ActionContext | None = None, generation: int | None = None,
        expected_context: ActionContext | None = None,
    ) -> IncomingResponse:
        """Регистрирует только действия конкретного ответа после проверки контекста.

        Ошибка чтения отключает кнопки, сохраняя текстовый ручной путь.
        Адресные подтверждения связываются только с актуальным публичным ожиданием.

        Args:
            event: Нормализованное событие с автором и адресом ответа.
            response: Ответ владельца сценария.
            store: Процессный реестр действий и управляемых представлений.
            view_origin: Контекст до исполнения запроса на представление.
            generation: Порядок события среди операций с представлениями.
            expected_context: Контекст фонового ответа, который должен сохраниться до привязки.

        Returns:
            Ответ, готовый к доставке, либо подсказка для недоступного обновления.
        """
        view = response.view

        def unavailable(current_view: ResponseView) -> IncomingResponse:
            fallback = self.resume_navigation(event, self._unavailable_view(event, response, current_view))
            return self.bind_response_actions(event, fallback, store, view_origin=view_origin,
                                              expected_context=expected_context)

        descriptions = response.suggested_actions
        response = replace(response, bound_actions=(), view=None, view_delivery=None)
        # Ссылка попадает в канал только после успешной проверки через bound_actions.
        # Даже отклонённая привязка не оставляет исходную URL-кнопку для проекции.
        response = replace(response, suggested_actions=tuple(action for action in descriptions if action.url is None))
        if not response.actions and not descriptions and view is None:
            return response
        private_chat = self._profile_link_allowed(event)
        if not private_chat and view is not None:
            return unavailable(view)
        if not private_chat and not self._group_action_address(event):
            return response
        try:
            context = self.action_context(event)
            if expected_context is not None and context != expected_context:
                return replace(response, suggested_actions=())
            if view is not None and not self._may_deliver_view(view_origin, context):
                return unavailable(view)
            snapshot = self._action_confirmations(event) if response.actions and private_chat else None
            actions = []
            for action in response.actions if snapshot is not None else ():
                # Подпись в списке ожиданий может уточнять предмет действия.
                # Исполняемую идентичность задают ID и точная адресная команда.
                matches = [(item, candidate) for item in snapshot.confirmations
                           if item.origin == event.reply_address for candidate in item.actions
                           if candidate.action_id == action.action_id and candidate.command == action.command]
                if snapshot.status == "complete" and len(matches) == 1:
                    pending, canonical_action = matches[0]
                    actions.append((ReplyAction(action.label, action.command), pending, canonical_action))
            confirmation_commands = {action.command for action in response.actions}
            for action in descriptions:
                if not private_chat:
                    if not self._group_action_allowed(action.command):
                        continue
                    action = replace(action, scope=ActionScope.SCREEN)
                if action.url is not None:
                    if (view_origin is not None and view_origin.app_user_id == context.app_user_id
                            and view_origin.access == context.access
                            and (view_origin.mode_id, view_origin.mode_revision)
                            == (context.mode_id, context.mode_revision)
                            and (context.mode_id is None or any(
                                identifier == context.mode_id and server and enabled and allowed
                                for identifier, server, enabled, allowed in context.access))):
                        actions.append((action, None, None))
                    continue
                if action.purpose == ActionPurpose.SELECTION and view is None:
                    continue
                parts = action.command.split()
                # Адресное решение никогда не разрешается через общий ReplyAction.
                if (not parts or not parts[0].startswith("/")
                        or (parts[0] == "/confirm" and len(parts) > 1)
                        or action.command in confirmation_commands
                        or not self._suggestion_allowed(action.command, context)):
                    continue
                actions.append((action, None, None))
            prepared = None
            delivery = None
            if view is not None:
                if generation is not None:
                    prepared = store.prepare_view(event, context, generation, view)
                if prepared is None:
                    return unavailable(view)
                delivery = prepared.delivery
                if delivery.target_message_id is not None and delivery.unavailable is not None:
                    action = self._current_recovery_action(event, context.mode_id)
                    options = (_HELP_SCREEN_ACTION,)
                    if action is not None and action.command and self._suggestion_allowed(action.command, context):
                        options = (action, *options)
                    recovery = store.register(event, context, tuple((item, None, None) for item in options))
                    delivery = replace(delivery, unavailable_actions=recovery)
            return replace(
                response, bound_actions=store.register(event, context, tuple(actions), view=prepared),
                view_delivery=delivery,
            )
        except Exception:
            LOGGER.exception("Could not bind response actions")
            return unavailable(view) if view is not None else response

    @staticmethod
    def _may_deliver_view(before: ActionContext | None, after: ActionContext) -> bool:
        return (
            before is not None and before.app_user_id is not None and before.mode_id is not None
            and (before.app_user_id, before.mode_id, before.mode_revision)
            == (after.app_user_id, after.mode_id, after.mode_revision)
            and any(identifier == after.mode_id and server and enabled and allowed
                    for identifier, server, enabled, allowed in after.access)
        )

    def _unavailable_view(
        self, event: IncomingEvent, response: IncomingResponse, view: ResponseView,
    ) -> IncomingResponse:
        """Возвращает сообщение об ошибке доставки и безопасные переходы."""
        action = self._current_recovery_action(event, None)
        return replace(
            response,
            content=view.unavailable if view.unavailable is not None else text_content(
                "Состояние чата изменилось. Открой нужный раздел заново."
            ),
            actions=(), suggested_actions=(action, _HELP_SCREEN_ACTION) if action else (_HELP_SCREEN_ACTION,),
            bound_actions=(), view=None, view_delivery=None,
        )

    def select_action(self, event: IncomingEvent, token: str, store: ResponseActionStore,
                      completion_handler: ChannelCompletionHandler | None = None,
                      menu_update_handler: CommandMenuUpdateHandler | None = None) -> IncomingResponse:
        """Принимает одноразовый выбор и повторяет проверки перед обычным входом.

        Args:
            event: Адрес и автор callback; его текст не исполняется.
            token: Непрозрачный ID из bound_actions конкретного ответа.
            store: Общий процессный реестр; после перезапуска старые ID не действуют.
            completion_handler: Обычная доставка последующего ответа.
            menu_update_handler: Обычная доставка изменений меню.

        Returns:
            Ответ обычного маршрута либо одинаковый безопасный отказ для всех
            отсутствующих, чужих, устаревших и уже использованных действий.
        """
        def rejected() -> IncomingResponse:
            return self._rejected_action(event, store.managed_action_owner(event, token))

        managed_source = store.protects_keyboard(event, token)
        context = None
        if managed_source:
            try:
                context = self.action_context(event)
            except Exception:
                LOGGER.exception("Could not validate selected managed response action")
                return rejected()
        with self._pending_inputs.lock(event.reply_address):
            busy = self._busy_response(event, allow_cancel=False)
            entry = store.claim(event, token, context=context, allowed_commands=_BUSY_COMMANDS if busy else None)
            if entry is not None and entry.command == "/cancel" and entry.confirmation is None:
                try:
                    valid = self.action_context(event) == entry.context
                except Exception:
                    LOGGER.exception("Could not validate cancellation")
                    valid = False
                if not valid:
                    return rejected()
                # Отмена относится к тому же поколению, которое только что проверено.
                reply = self.execute(replace(event, text=entry.command, images=()), completion_handler,
                                     menu_update_handler)
                return replace(reply, preserve_source_actions=True) if managed_source else reply
            if entry is None and busy is not None:
                return replace(busy, preserve_source_actions=True)
        if entry is None:
            return rejected()
        try:
            if self.action_context(event) != entry.context:
                return rejected()
            if entry.confirmation is not None:
                snapshot = self._action_confirmations(event)
                if snapshot.status != "complete" or not any(
                        item == entry.confirmation and entry.action in item.actions
                        for item in snapshot.confirmations):
                    return rejected()
            elif (not self._suggestion_allowed(entry.command, entry.context)
                  or (not self._profile_link_allowed(event) and not self._group_action_allowed(entry.command))):
                return rejected()
            if entry.command.strip() == "/confirm":
                snapshot = self._action_confirmations(event)
                if snapshot.status != "complete" or not snapshot.confirmations:
                    return rejected()
        except Exception:
            LOGGER.exception("Could not validate selected response action")
            return rejected()
        # Команда берётся только из серверной записи, никогда из callback/подписи.
        response = self.execute(replace(event, text=entry.command, images=()), completion_handler, menu_update_handler)
        return replace(response, preserve_source_actions=True) if managed_source or entry.view is not None else response

    def _rejected_action(self, event: IncomingEvent, previous_mode: str | None) -> IncomingResponse:
        """Сохраняет исходные кнопки и предлагает актуальные переходы."""
        response = IncomingResponse(text_content("Эта кнопка больше недоступна. Выбери актуальное действие."),
                                    preserve_source_actions=True)
        action = self._current_recovery_action(event, previous_mode)
        if action is not None:
            response = replace(response, suggested_actions=(action,))
        # Один ID мог уже получить свежую клавиатуру. Без подтверждённого перехода
        # неизвестное нажатие не даёт права её снимать.
        return self.resume_navigation(event, response)

    def _current_recovery_action(self, event: IncomingEvent, previous_mode: str | None) -> ReplyAction | None:
        """Запрашивает кнопку восстановления у доступного владельца представления."""
        if is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            try:
                known = self._user_identity_service.find_known_private_chat(event.channel, event.chat_id)
                if known is not None and known[1].external_user_id == event.external_user_id:
                    user = known[0]
                    mode = self._chat_mode_repository.get_state(ChatModeKey(user.id, event.channel, event.chat_id))
                    mode_id = previous_mode or mode.capability_id
                    capability = self._personal_registry(user, event).by_id(mode_id) if mode_id is not None else None
                    if isinstance(capability, ReplyActionRecoveryPort):
                        revision = mode.revision if mode_id == mode.capability_id else None
                        return capability.reply_action_recovery(user, event.reply_address, revision)
            except Exception:
                LOGGER.exception("Could not resolve response action recovery")
        return None

    def _suggestion_allowed(self, text: str, context: ActionContext) -> bool:
        """Не выдаёт неизвестную или недоступную предметную команду другому режиму."""
        command, *arguments = text.split()
        if command == "/cancel":
            return context.pending_request_id is not None or context.deferred_request_id is not None
        if context.app_user_id is None:
            return command in {"/register", "/link", "/help"} and not arguments
        effective = {identifier for identifier, server, enabled, allowed in context.access
                     if server and enabled and allowed}
        owner = self._registry.mode_command_owner(command)
        in_owner_mode = owner is not None and owner.capability_id == context.mode_id and owner.capability_id in effective
        if command in PROFILE_COMMANDS:
            if not in_owner_mode:
                return False
        if command in {ChatCommand.ENABLE.value, ChatCommand.DISABLE.value} and arguments:
            return (len(arguments) == 1
                    and self._registry.is_personally_configurable(arguments[0])
                    and any(capability_id == arguments[0] and allowed
                            for capability_id, _, _, allowed in context.access)
            )
        if command in {item.value for item in ChatCommand}:
            return True
        entry = self._registry.by_command(command)
        if entry is not None:
            return entry.descriptor.capability_id in effective
        return in_owner_mode

    def _action_confirmations(self, event: IncomingEvent) -> ConfirmationQueryResult:
        """Читает ожидания проверенного адреса без touch исходной identity."""
        known = self._user_identity_service.find_known_private_chat(event.channel, event.chat_id)
        user = known[0] if known else None
        state = self._chat_mode_repository.get_state(
            ChatModeKey(user.id, event.channel, event.chat_id)
        ) if user else None
        active = self._personal_registry(user, event).by_id(state.capability_id) if state and state.capability_id else None
        return self._confirmation_snapshot(event, user, active, mode_revision=state.revision if state else None)

    def _capability_defaults(self) -> tuple[CapabilityDefault, ...]:
        """Передаёт domain только устойчивые ID и defaults общего каталога."""
        return tuple(
            CapabilityDefault(entry.capability_id, entry.enabled_by_default)
            for entry in self._registry.descriptors() if entry.personally_configurable
        )

    def _blocked_reply(self, access: CapabilityAccess) -> str:
        """Объясняет причину отказа без открытия предметного обработчика."""
        if not access.profile_allowed:
            return "У твоего профиля нет доступа к этой возможности."
        descriptor = access.descriptor
        if not access.chat_allowed and descriptor.private_chat_hint is not None:
            return descriptor.private_chat_hint
        label = _capability_label(descriptor)
        if not access.server_available:
            return f"Возможность {label} недоступна на сервере."
        return f"Возможность {label} лично выключена. Управлять доступом можно в настройках."

    @staticmethod
    def _capability_catalog(personal: PersonalCapabilityRegistry) -> str:
        """Показывает названия, идентификаторы и доступность возможностей."""
        lines = ["Личные возможности:"]
        visible = tuple(item for item in personal.visible_access if item.descriptor.personally_configurable)
        for access in visible:
            descriptor = access.descriptor
            label = _capability_label(descriptor)
            if descriptor.display_name:
                label += f" ({descriptor.capability_id})"
            lines.append(
                f"{label} — {descriptor.entry.description}. "
                f"Личный выбор: {'включена' if access.user_enabled else 'выключена'}; "
                f"сервер: {'доступна' if access.server_available else 'недоступна'}."
            )
        if not visible:
            lines.append("Сейчас нет известных возможностей.")
        lines.append("Выбор действует во всех привязанных каналах.")
        return "\n".join(lines)

    def execute(
        self,
        event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None = None,
        menu_update_handler: CommandMenuUpdateHandler | None = None,
    ) -> IncomingResponse:
        """Возвращает каналу общий ответ, сохраняя предметные модели внутри возможности.

        Args:
            event: Нормализованное событие канала.
            completion_handler: Доставка последующего ответа по сохранённому адресу.
            menu_update_handler: Доставка меню по адресу текущего или связанного чата.

        Returns:
            Готовое содержимое ответа и необязательные адресные действия.
        """
        from compadre_assistant.core.presentation.responses import format_incoming_message_result

        # Пока операция активна, не вызываем даже чтение меню с обновлением identity.
        if self._deferred_actions is not None:
            with self._pending_inputs.lock(event.reply_address):
                busy = self._busy_response(event)
            if busy is not None:
                if busy is _DEFERRED_CANCELLED_REPLY:
                    if menu_update_handler is not None:
                        self._update_current_menu(event, menu_update_handler, BUSY_COMMAND_MENU)
                    return self.resume_navigation(event, busy)
                return busy
        previous_menu = None
        previous_user = None
        previous_preferences = None
        if menu_update_handler is not None:
            try:
                # Предварительное чтение не очищает ожидание: ответ об истечении
                # или смене контекста должен сформировать обычный маршрут ввода.
                previous_menu, _ = self._command_menu_snapshot(event, cleanup_pending=False)
                previous_user = self._user_identity_service.resolve(_incoming_identity_from_event(event))
                if previous_user is not None:
                    previous_preferences = self._capability_preference_repository.get_for_user(previous_user.id)
            except Exception:
                LOGGER.exception("Could not read command menu before incoming action")

        pending_before = self._pending_for_event(event)
        result = self._execute(event, completion_handler)
        if isinstance(result, IncomingResponse):
            response = result
        else:
            suggested_actions = ()
            if result.type in {
                IncomingMessageResultType.START_UNREGISTERED, IncomingMessageResultType.UNKNOWN_IDENTITY,
                IncomingMessageResultType.START_REGISTERED, IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                IncomingMessageResultType.REGISTRATION_NAME_SAVED, IncomingMessageResultType.ALREADY_REGISTERED,
                IncomingMessageResultType.MODE_EXITED, IncomingMessageResultType.MODE_HINT,
                IncomingMessageResultType.MODE_UNAVAILABLE, IncomingMessageResultType.CAPABILITY_DISABLED,
                IncomingMessageResultType.LINKED,
            }:
                personal = result.personal
                if personal is None and result.app_user is not None:
                    personal = self._personal_registry(result.app_user, event)
                suggested_actions = main_actions(personal, self._pending_for_event(event),
                                                 allow_link=self._profile_link_allowed(event))
                if result.type is IncomingMessageResultType.MODE_UNAVAILABLE:
                    suggested_actions = (HOME_SECTION_ACTION, *suggested_actions)
                if (result.type is IncomingMessageResultType.CAPABILITY_DISABLED
                        and personal is not None and result.app_user is not None):
                    settings = self._registry.mode_command_owner(ChatCommand.CAPABILITIES.value)
                    user_id = result.app_user.id
                    state = self._chat_mode_repository.get_state(ChatModeKey(user_id, event.channel, event.chat_id))
                    if (settings is not None and personal.by_id(settings.capability_id) is not None
                            and state.capability_id and state.capability_id != settings.capability_id
                            and personal.by_id(state.capability_id) is not None):
                        suggested_actions = (replace(capability_entry_action(settings), scope=ActionScope.SCREEN),
                                             _HELP_SCREEN_ACTION)
            response = IncomingResponse(
                format_incoming_message_result(result), result.error,
                suggested_actions=suggested_actions,
                command_menu_delivery=SectionMenuDelivery.RECONCILE,
            )
        finished_transfer = isinstance(result, ProcessIncomingMessageResult) and result.type in {
            IncomingMessageResultType.LINK_REBOUND, IncomingMessageResultType.LINK_REBIND_CANCELLED,
        }
        continued_profile_input = (pending_before is not None and pending_before.owner_id == "core"
                                   and (not event.text.lstrip().startswith("/") or event.text.strip() == "/cancel"
                                        or event.menu_choice))
        cancelled = result is _DEFERRED_CANCELLED_REPLY or event.text.strip() == "/cancel" or (
            event.menu_choice and event.text == CANCEL_SECTION_ACTION.label
        )
        pending_after = self._pending_for_event(event)
        waiting = (pending_after is not None and pending_after.cancel_only_menu
                   and not response.preserve_source_actions and not self._pending_inputs.expired(pending_after))
        if cancelled or waiting or (response.error is not None and not response.suggested_actions):
            response = self.resume_navigation(event, response)
        if (finished_transfer or continued_profile_input) and not response.suggested_actions and not response.actions:
            actions = self._reply_section_actions(event)
            if actions is not None:
                response = replace(response, suggested_actions=actions,
                                   command_menu_delivery=SectionMenuDelivery.RECONCILE)
        if menu_update_handler is not None:
            # Действие могло занять чат между снимком меню и повторным допуском.
            if result is _DEFERRED_CANCELLED_REPLY:
                previous_menu = BUSY_COMMAND_MENU
            self._update_current_menu(event, menu_update_handler, previous_menu)
            try:
                current_user = self._user_identity_service.resolve(_incoming_identity_from_event(event))
                owner_changed = (previous_user.id if previous_user else None) != (current_user.id if current_user else None)
                preferences_changed = (current_user is not None and
                    self._capability_preference_repository.get_for_user(current_user.id) != previous_preferences)
                if owner_changed or preferences_changed:
                    # После переноса перечитываем обе стороны: старый снимок адресов
                    # нельзя использовать для доставки меню прежнего владельца.
                    affected = {user.id: user for user in (previous_user, current_user) if user is not None}
                    for user in affected.values():
                        self._update_linked_chat_menus(user, event.reply_address, menu_update_handler)
            except Exception:
                LOGGER.exception("Could not refresh linked chat menus after incoming action")
        return response

    def resume_navigation(self, event: IncomingEvent, response: IncomingResponse) -> IncomingResponse:
        """Добавляет проверенные действия раздела к ответу после завершения шага.

        Args:
            event: Исходный автор и адрес, по которым перечитывается доступ.
            response: Ответ с действиями владельца сценария, если они заданы.

        Returns:
            Ответ с обратимыми переходами текущего раздела и справкой.
        """
        if response.view is not None:
            return response
        actions = response.suggested_actions
        busy = self._busy_response(event, allow_cancel=False)
        section = busy.suggested_actions if busy is not None else self._reply_section_actions(event) or ()
        help_action = HELP_SECTION_ACTION if any(action.command == "/help" for action in section) else _HELP_SCREEN_ACTION
        current = (*section, help_action)
        seen = {(action.command, action.url) for action in actions}
        available = []
        for action in current:
            key = action.command, action.url
            if key not in seen:
                available.append(action)
                seen.add(key)
        return replace(response, suggested_actions=(*actions, *available))

    def _reply_section_actions(self, event: IncomingEvent) -> tuple[ReplyAction, ...] | None:
        try:
            return self._command_context(event, cleanup_pending=False, include_menu=True)[-1]
        except Exception:
            LOGGER.exception("Could not read current section actions")
            return None

    def _update_current_menu(self, event: IncomingEvent, handler: CommandMenuUpdateHandler,
                             previous_menu: CommandMenu | None) -> None:
        """Передаёт изменившуюся проекцию без отказа уже выполненной команды."""
        try:
            menu = self.command_menu(event)
            if menu != previous_menu:
                handler(CommandMenuUpdate(event.reply_address, menu))
        except Exception:
            LOGGER.exception("Could not update command menu for current chat")

    def _update_linked_chat_menus(
        self, user: AppUser, origin: ReplyAddress, handler: CommandMenuUpdateHandler,
    ) -> None:
        """Пересчитывает каждый связанный адрес с его режимом и ожиданием."""
        try:
            identities = self._user_identity_service.list_known_private_chats(user)
        except Exception:
            LOGGER.exception("Could not enumerate linked chats for menu update")
            return
        for identity in identities:
            address = ReplyAddress(identity.channel, identity.external_chat_id)
            if address == origin:
                continue
            try:
                if not self._user_identity_service.owns_known_private_chat(user, identity):
                    continue
                event = IncomingEvent(identity.channel, identity.external_chat_id,
                    "command-menu-refresh", "/help", identity.external_user_id)
                # Это чтение сохранённого адреса, а не новое событие пользователя:
                # resolve с touch мог бы вернуть устаревший адрес обратно в БД.
                menu, _ = self.command_menu_snapshot(event, known_user=user, preserve_expired=address.channel == "vk")
                if self._user_identity_service.owns_known_private_chat(user, identity):
                    handler(CommandMenuUpdate(address, menu))
            except Exception:
                LOGGER.exception("Could not update command menu for linked chat")

    def _execute(
        self, event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None = None,
    ) -> ProcessIncomingMessageResult | IncomingResponse:
        """Сериализует ожидание чата, затем запускает независимый обычный маршрут."""
        with self._pending_inputs.lock(event.reply_address):
            if event.menu_choice:
                command = self._resolve_menu_choice(event)
                if command is None:
                    return self.resume_navigation(event, IncomingResponse(text_content(
                        "Эта кнопка меню устарела. Выбери действие из актуального меню.",
                    )))
                event = replace(event, text=command, menu_choice=False)
            if busy := self._busy_response(event):
                return busy
            result = self._route_pending_input(event, completion_handler)
            if result is None and not event.text.strip() and not event.images:
                return self.resume_navigation(event, IncomingResponse(text_content(_TEXT_INPUT_HINT)))
            parsed = ParsedChatCommand.parse(event.text.strip())
            if result is None and parsed is not None:
                command = parsed.command
                if command in (ChatCommand.REGISTER, ChatCommand.LINK, ChatCommand.NAME,
                               ChatCommand.ENABLE, ChatCommand.DISABLE):
                    return self._dispatch_message(event, completion_handler)
        if isinstance(result, PendingInputResult):
            try:
                if result.cancellable_action is not None:
                    return result.cancellable_action(lambda: False)
                return result.action() if result.action is not None else result.response
            except Exception as error:
                LOGGER.exception("Accepted pending input action failed")
                return IncomingResponse(text_content(
                    "Не удалось обработать ввод. Проверь состояние действия перед повторной отправкой."
                ), error)
        return result if result is not None else self._dispatch_message(event, completion_handler)

    def _resolve_menu_choice(self, event: IncomingEvent) -> str | None:
        """Разрешает единственное точное совпадение подписи с актуальным меню."""
        if not is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            return None
        try:
            menu, _ = self._command_menu_snapshot(event, cleanup_pending=False)
        except Exception:
            LOGGER.exception("Could not resolve command menu choice")
            return None
        matches = tuple(action for action in (menu.section_actions or ()) if action.label == event.text)
        return matches[0].command if len(matches) == 1 and matches[0].command else None

    def _busy_response(self, event: IncomingEvent, *, allow_cancel: bool = True) -> IncomingResponse | None:
        """Ограничивает занятый чат, не меняя состояние и не запуская обычный маршрут."""
        actions = self._deferred_actions
        if actions is None:
            return None
        action = actions.get(event.reply_address)
        if action is None:
            return None
        text = event.text.strip()
        if allow_cancel and event.menu_choice:
            if self._profile_link_allowed(event):
                text = next((item.command for item in BUSY_COMMAND_MENU.section_actions if item.label == event.text), "")
            else:
                text = ""
        parsed = ParsedChatCommand.parse(text) if allow_cancel else None
        is_author = event.external_user_id is not None and action.external_user_id == event.external_user_id
        if parsed is not None and parsed.command is ChatCommand.HELP:
            user = self._user_identity_service.resolve_without_touch(_incoming_identity_from_event(event)) if is_author else None
            can_cancel = user is not None and user.id == action.app_user_id
            cancel_hint = compose_content("\n", code_span("/cancel"), " — отменить обработку") if can_cancel else text_content("")
            content = compose_content("Compadre Assistant — справка.\nОбработка уже идёт.", cancel_hint,
                                      "\n", code_span("/help"), " — открыть эту справку")
            return replace(self.busy_reply(can_cancel=can_cancel), content=content,
                           preserve_source_actions=True)
        if parsed is not None and parsed.command is ChatCommand.CANCEL:
            if parsed.arguments:
                return self.busy_reply(can_cancel=is_author)
            if is_author:
                user = self._user_identity_service.resolve_without_touch(_incoming_identity_from_event(event))
                if user is not None and user.id == action.app_user_id and actions.cancel(action):
                    return _DEFERRED_CANCELLED_REPLY
        return self.busy_reply(can_cancel=is_author)

    @staticmethod
    def busy_reply(*, can_cancel: bool = False) -> IncomingResponse:
        """Предлагает дождаться результата и даёт автору кнопку отмены.

        Args:
            can_cancel: Автор события совпадает с автором текущей работы.

        Returns:
            Ответ занятого чата с доступными обратимыми действиями.
        """
        return IncomingResponse(text_content("Обработка уже идёт. Дождись результата."),
                                suggested_actions=BUSY_COMMAND_MENU.section_actions if can_cancel else (HELP_SECTION_ACTION,))

    def may_finish_deferred(self, event: IncomingEvent, app_user_id: int,
                            capability_id: str, required_mode: str | None) -> bool:
        """Перепроверяет исходную личность, доступ и режим в новом scope перед доставкой.

        Args:
            event: Адрес и личность без содержимого принятого ввода.
            app_user_id: Профиль, которому принадлежит принятая работа.
            capability_id: Владелец работы и профильного допуска.
            required_mode: Обязательный активный режим либо None.

        Returns:
            True, если этот профиль по-прежнему может получить результат.
        """
        user = self._user_identity_service.resolve_without_touch(_incoming_identity_from_event(event))
        if user is None or user.id != app_user_id or self._personal_registry(user, event).by_id(capability_id) is None:
            return False
        if is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            known = self._user_identity_service.find_known_private_chat(event.channel, event.chat_id)
            if known is None or known[0].id != user.id or known[1].external_user_id != event.external_user_id:
                return False
        if required_mode is None:
            return True
        mode = self._chat_mode_repository.get(ChatModeKey(user.id, event.channel, event.chat_id))
        return mode is not None and mode.capability_id == required_mode

    def _route_pending_input(
        self, event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None,
    ) -> ProcessIncomingMessageResult | IncomingResponse | PendingInputResult | None:
        """Атомарно выбирает продолжение, не блокируя обычные долгие операции."""
        text = event.text.strip()
        parsed = ParsedChatCommand.parse(text)
        command = parsed.command if parsed is not None else None
        pending = self._pending_for_event(event)
        expired = pending is not None and self._pending_inputs.expired(pending)
        if expired:
            self._pending_inputs.clear(event.reply_address, pending.request_id)
        if command is ChatCommand.CANCEL:
            if parsed.arguments:
                return IncomingResponse(text_content("Чтобы закончить ввод, выбери «Отмена»."),
                                        suggested_actions=(CANCEL_SECTION_ACTION, HELP_SECTION_ACTION))
            cleared = pending is not None and self._pending_inputs.clear(event.reply_address, pending.request_id)
            if cleared and pending is not None and pending.cancel_return_command is not None:
                return IncomingResponse(
                    text_content("Ввод отменён. Можно вернуться к предыдущему экрану."),
                    suggested_actions=(ReplyAction("Вернуться", pending.cancel_return_command, scope=ActionScope.SECTION),),
                    close_managed_views=True,
                )
            return IncomingResponse(text_content(
                "Ввод отменён. Можно продолжить в текущем разделе." if cleared else
                "В этом чате нечего отменять."
            ), close_managed_views=bool(cleared and pending is not None and pending.owner_id == "core"))
        if text.startswith("/"):
            if pending is not None and command not in (ChatCommand.HELP, ChatCommand.STATUS,
                                                       ChatCommand.CAPABILITIES, ChatCommand.START):
                self._pending_inputs.clear(event.reply_address, pending.request_id)
        elif expired:
            if self._registry.requires_profile_access(pending.owner_id):
                user = self._user_identity_service.resolve(_incoming_identity_from_event(event))
                personal = self._personal_registry(user, event) if user is not None else None
                access = personal.status(pending.owner_id) if personal is not None else None
                if access is None or not access.visible:
                    return None
            return self.resume_navigation(event, pending_expired_reply(pending))
        elif pending is not None:
            if event.images and (pending.owner_id == "core" or not pending.accepts_images):
                return self.resume_navigation(event, IncomingResponse(text_content(_TEXT_INPUT_HINT)))
            return self._continue_input(pending, event, completion_handler)
        return None

    def _dispatch_message(
        self,
        event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None = None,
    ) -> ProcessIncomingMessageResult | IncomingResponse:
        """Обрабатывает общую часть сообщения и передаёт предметный ввод.

        Args:
            event: Нормализованное событие канала без внутреннего ID пользователя.
            completion_handler: Доставка результата фоновой операции в исходный чат.

        Returns:
            Общий ответ или ответ зарегистрированной возможности.
        """
        text = event.text.strip()
        parsed = ParsedChatCommand.parse(text)
        command = parsed.command if parsed is not None else None
        is_general_entry = (
            command is not None
            or self._registry.by_command(text) is not None
            or self._registry.disabled_by_command(text) is not None
        )
        if command is ChatCommand.HELP:
            return self._contextual_help(event)
        identity = _incoming_identity_from_event(event)
        if command is ChatCommand.CONFIRM:
            parts = parsed.arguments.split() if parsed else []
            if not parts:
                return self._confirmation_response(self.list_pending_confirmations(event))
            if parts and parts[0] == "core":
                if not self._profile_link_allowed(event):
                    return IncomingResponse(text_content(_PROFILE_LINK_PRIVATE_HINT))
                if (
                    len(parts) != 5
                    or parts[1] != "identity"
                    or any(not value.isascii() or not value.isdecimal() or len(value) > 19
                           or not 0 < int(value) <= 2**63 - 1 for value in parts[2:4])
                    or parts[4] not in {"yes", "no"}
                ):
                    return IncomingResponse(text_content('Выбери решение в актуальном запросе привязки.'),
                                            suggested_actions=(_CONFIRM_SCREEN_ACTION, _HELP_SCREEN_ACTION))
                try:
                    if parts[4] == "yes":
                        user = self._user_identity_service.confirm_rebind(
                            identity, confirmation_id=int(parts[2]), version=int(parts[3])
                        )
                        return ProcessIncomingMessageResult(
                            IncomingMessageResultType.LINK_REBOUND, app_user=user
                        )
                    self._user_identity_service.cancel_rebind(
                        identity, confirmation_id=int(parts[2]), version=int(parts[3])
                    )
                    return ProcessIncomingMessageResult(IncomingMessageResultType.LINK_REBIND_CANCELLED)
                except SourceProfileInUseError:
                    return IncomingResponse(compose_content(
                        "Канал остался в исходном профиле, потому что этот профиль ещё используется. "
                        "Привяжи к нему другой канал, затем открой «Подтверждения», чтобы повторить перенос.",
                    ), suggested_actions=(_CONFIRM_SCREEN_ACTION, _HELP_SCREEN_ACTION))
                except InvalidLinkCodeError as error:
                    return IncomingResponse(
                        text_content("Запрос переноса устарел или истёк. Введи действующий код, чтобы создать новый запрос."),
                        error,
                        suggested_actions=(_LINK_INPUT_SCREEN_ACTION, _HELP_SCREEN_ACTION),
                    )
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(IncomingMessageResultType.UNKNOWN_IDENTITY)
            personal = self._personal_registry(user, event)
            mode = self._chat_mode_repository.get(ChatModeKey(user.id, event.channel, event.chat_id))
            target = parts[0] if parts else ""
            if mode is None or mode.capability_id != target:
                capability = personal.by_id(target)
                if capability is not None:
                    return IncomingResponse(
                        text_content("Открой раздел, чтобы продолжить подтверждение в этом чате."),
                        suggested_actions=(replace(capability_entry_action(capability.descriptor), scope=ActionScope.SCREEN),
                                           _HELP_SCREEN_ACTION),
                    )
                return IncomingResponse(text_content(personal.entry_hint()), suggested_actions=main_actions(personal, None))
            return self._dispatch(user, event, completion_handler, personal)

        if command is ChatCommand.START:
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(type=IncomingMessageResultType.START_UNREGISTERED)
            personal = self._personal_registry(user, event)
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.START_REGISTERED,
                app_user=user,
                capability_reply=personal.entry_hint(),
                personal=personal,
            )

        if command is ChatCommand.REGISTER:
            if parsed.arguments:
                return IncomingResponse(text_content(
                    "Сначала выбери «Создать профиль», затем пришли имя отдельным сообщением."
                ), suggested_actions=main_actions(None, None, allow_link=self._profile_link_allowed(event)))
            existing = self._user_identity_service.resolve(identity)
            if existing is None:
                pending = self._pending_inputs.get(event.reply_address)
                if (pending is not None and pending.external_user_id not in (None, event.external_user_id)
                        and not self._pending_inputs.expired(pending)):
                    return IncomingResponse(text_content(
                        "В этом чате другой участник уже вводит данные. Попробуй позже или открой личный чат с ботом."
                    ), suggested_actions=(HELP_SECTION_ACTION,))
                self._pending_inputs.begin(event.reply_address, "core", "register_name", retry_command="/register",
                                           external_user_id=event.external_user_id)
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                )
            personal = self._personal_registry(existing, event)
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.ALREADY_REGISTERED,
                app_user=existing,
                capability_reply=personal.entry_hint(),
                personal=personal,
            )

        user = self._user_identity_service.resolve(identity)
        if user is None:
            if command is ChatCommand.LINK:
                return self._start_link(parsed.arguments, identity, event)
            return ProcessIncomingMessageResult(type=IncomingMessageResultType.UNKNOWN_IDENTITY)
        if command is ChatCommand.EXIT:
            personal = self._personal_registry(user, event)
            self._chat_mode_repository.clear(ChatModeKey(user.id, event.channel, event.chat_id))
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_EXITED,
                app_user=user,
                capability_reply=personal.entry_hint(),
                personal=personal,
            )
        return self._dispatch(user, event, completion_handler, self._personal_registry(user, event))

    def _profile_operation(
        self, command: ChatCommand, arguments: str, user: AppUser, event: IncomingEvent, mode_id: str,
        personal: PersonalCapabilityRegistry,
    ) -> IncomingResponse:
        """Выполняет узкую операцию ядра после проверки владельца режима."""
        from compadre_assistant.core.presentation.responses import format_incoming_message_result

        owner = self._registry.mode_command_owner(command.value)
        if owner is None or owner.capability_id != mode_id or command not in owner.profile_commands:
            return IncomingResponse(text_content("Это действие недоступно в текущем разделе."),
                                    suggested_actions=(_HELP_SCREEN_ACTION,))
        if not arguments and command in (ChatCommand.NAME, ChatCommand.ENABLE, ChatCommand.DISABLE):
            pending = self._pending_inputs.get(event.reply_address)
            if (pending is not None and pending.external_user_id not in (None, event.external_user_id)
                    and not self._pending_inputs.expired(pending)):
                return IncomingResponse(text_content(
                    "В этом чате другой участник уже вводит данные. Попробуй позже или открой личный чат с ботом."
                ))
        result = self._apply_profile_operation(command, arguments, user, event, mode_id, personal)
        return result if isinstance(result, IncomingResponse) else IncomingResponse(
            format_incoming_message_result(result), result.error,
        )

    def _apply_profile_operation(
        self, command: ChatCommand, arguments: str, user: AppUser, event: IncomingEvent, mode_id: str,
        personal: PersonalCapabilityRegistry,
    ) -> ProcessIncomingMessageResult | IncomingResponse:
        if command is ChatCommand.CAPABILITIES:
            if arguments:
                return IncomingResponse(text_content('Открой список личных возможностей отдельным действием.'))
            actions = []
            if self._profile_choices(personal, enabled=True):
                actions.append(ReplyAction("Включить возможность", "/enable"))
            if self._profile_choices(personal, enabled=False):
                actions.append(ReplyAction("Выключить возможность", "/disable"))
            return IncomingResponse(text_content(self._capability_catalog(personal)), suggested_actions=tuple(actions))
        if command in (ChatCommand.ENABLE, ChatCommand.DISABLE):
            return self._change_capability(command, arguments, user, event, personal, required_mode=mode_id)
        if command is ChatCommand.LINK:
            return self._start_link(
                arguments, _incoming_identity_from_event(event), event, user=user, required_mode=mode_id,
            )
        if command is ChatCommand.CONNECT:
            if not is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
                return IncomingResponse(text_content(_PROFILE_LINK_PRIVATE_HINT))
            try:
                link_code = self._user_identity_service.create_link_code(user.id, target_channel=arguments)
            except IdentityAlreadyBoundError:
                return IncomingResponse(text_content("Этот мессенджер уже привязан."))
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.LINK_CODE_CREATED,
                app_user=user,
                link_code=link_code,
            )
        if command is ChatCommand.NAME:
            if not arguments:
                self._pending_inputs.begin(
                    event.reply_address, "core", "profile_name", app_user_id=user.id,
                    required_mode=mode_id, retry_command="/name", external_user_id=event.external_user_id,
                )
                return IncomingResponse(text_content(
                    "Пришли новое имя профиля. Ожидание действует 10 минут."
                ))
            return self._save_name(arguments, user)
        if command is ChatCommand.STATUS:
            access = personal.status(mode_id)
            mode_label = _capability_label(access.descriptor) if access else mode_id
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.PROFILE_STATUS,
                app_user=user,
                capability_reply=(
                    f"Профиль: {user.display_name or 'имя ещё не задано'}.\n"
                    f"Режим этого чата: {mode_label}."
                ),
            )
        return IncomingResponse(text_content("Это действие недоступно в текущем разделе."),
                                suggested_actions=(_HELP_SCREEN_ACTION,))

    def _start_link(
        self, code: str, identity: IncomingIdentity, event: IncomingEvent,
        *, user: AppUser | None = None, required_mode: str | None = None,
    ) -> ProcessIncomingMessageResult | IncomingResponse:
        if not self._profile_link_allowed(event):
            return IncomingResponse(text_content(_PROFILE_LINK_PRIVATE_HINT))
        if code:
            from compadre_assistant.core.presentation.responses import format_incoming_message_result

            result = self._link_identity(code, identity)
            if isinstance(result, ProcessIncomingMessageResult) and result.type is IncomingMessageResultType.LINK_CODE_INVALID:
                return IncomingResponse(format_incoming_message_result(result), result.error,
                                        suggested_actions=(_LINK_INPUT_SCREEN_ACTION, _HELP_SCREEN_ACTION))
            return result
        self._pending_inputs.begin(
            event.reply_address, "core", "link_code", app_user_id=user.id if user else None,
            required_mode=required_mode, retry_command="/link", external_user_id=event.external_user_id,
        )
        return IncomingResponse(text_content(
            "Пришли код привязки следующим сообщением. Ожидание действует 10 минут."
        ), suggested_actions=(CANCEL_SECTION_ACTION, HELP_SECTION_ACTION))

    def _link_identity(self, code: str, identity: IncomingIdentity) -> ProcessIncomingMessageResult | IncomingResponse:
        """Применяет код одинаково для прямой команды и ожидаемого значения."""
        try:
            user = self._user_identity_service.link(code=code, identity=identity)
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.LINKED,
                app_user=user,
            )
        except IdentityAlreadyBoundError as error:
            try:
                pending = self._user_identity_service.request_rebind_confirmation(
                    code=code, identity=identity
                )
                return IncomingResponse(
                    text_content('Канал уже привязан к другому профилю. Перенести аккаунт?'),
                    actions=_rebind_actions(pending),
                    suggested_actions=(_HELP_SCREEN_ACTION,),
                )
            except IdentityAlreadyBoundError:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.LINK_ALREADY_BOUND,
                    error=error,
                )
            except InvalidLinkCodeError as rebind_error:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.LINK_CODE_INVALID,
                    error=rebind_error,
                )
        except InvalidLinkCodeError as error:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.LINK_CODE_INVALID,
                error=error,
            )

    def _continue_input(
        self, pending: PendingInput, event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None,
    ) -> ProcessIncomingMessageResult | IncomingResponse | PendingInputResult:
        """Повторно проверяет допуск и передаёт исходное значение владельцу."""
        if pending.owner_id == "core" and pending.input_kind == "register_name":
            try:
                user = self._user_identity_service.register_with_name(
                    _incoming_identity_from_event(event), display_name=event.text,
                )
            except InvalidDisplayNameError as error:
                return IncomingResponse(text_content(
                    "Имя не принято. Используй от 1 до 80 символов без ссылки или команды. "
                    "Отправь исправленное имя ещё раз до истечения срока."
                ), error)
            except Exception as error:
                self._pending_inputs.clear(event.reply_address, pending.request_id)
                LOGGER.exception("Registration failed")
                return IncomingResponse(text_content(
                    "Не удалось завершить регистрацию. Выбери «Создать профиль», чтобы начать заново."
                ), error)
            self._pending_inputs.clear(event.reply_address, pending.request_id)
            personal = self._personal_registry(user, event)
            return ProcessIncomingMessageResult(
                # Параллельная регистрация могла сохранить другое имя раньше нас.
                (IncomingMessageResultType.REGISTRATION_NAME_SAVED
                 if user.display_name == " ".join(event.text.split())
                 else IncomingMessageResultType.ALREADY_REGISTERED), app_user=user,
                capability_reply=personal.entry_hint(),
                personal=personal,
            )
        if pending.owner_id == "core":
            return self._continue_core_input(pending, event)
        user = self._user_identity_service.resolve(_incoming_identity_from_event(event))
        personal = self._personal_registry(user, event) if user is not None else None
        owner = personal.by_id(pending.owner_id) if personal is not None else None
        if owner is None or not isinstance(owner, PendingInputContinuationPort):
            self._pending_inputs.clear(event.reply_address, pending.request_id)
            access = personal.status(pending.owner_id) if personal is not None else None
            return self.resume_navigation(event, IncomingResponse(text_content(
                self._blocked_reply(access) if access is not None and not access.effective
                else "Этот ввод больше недоступен. Выбери актуальное действие."
            )))
        if event.images and not owner.descriptor.accepts_images:
            return self.resume_navigation(event, IncomingResponse(text_content(_TEXT_INPUT_HINT)))
        mode = self._chat_mode_repository.get_state(ChatModeKey(user.id, event.channel, event.chat_id))
        if ((pending.app_user_id is not None and pending.app_user_id != user.id)
                or (pending.required_mode is not None
                    and mode.capability_id != pending.required_mode)):
            self._pending_inputs.clear(event.reply_address, pending.request_id)
            return self.resume_navigation(event, IncomingResponse(text_content(
                "Контекст ввода изменился. Ожидание отменено. Выбери действие в текущем разделе."
            )))
        context = HandlerContext(
            user, CallbackReplyDelivery(completion_handler) if completion_handler else None,
            self._pending_inputs,
            mode_revision=mode.revision if mode.capability_id == pending.owner_id else None,
            action_layout=event.action_layout,
        )
        try:
            result = owner.continue_input(context, pending, event)
        except Exception as error:
            # При неизвестном исходе внешнего действия повтор опасен: закрываем ввод.
            self._pending_inputs.clear(event.reply_address, pending.request_id)
            LOGGER.exception("Pending input failed: owner_id=%s", pending.owner_id)
            return IncomingResponse(text_content(
                "Не удалось обработать ввод. Проверь состояние действия перед повторной отправкой."
            ), error)
        if result.response.view is not None and context.mode_revision is None:
            result = replace(result, response=self._unavailable_view(event, result.response, result.response.view))
        if result.cancellable_action is not None and self._can_defer(event, completion_handler):
            return self._defer_action(
                event, user.id, pending.owner_id, pending.required_mode, result.response,
                result.cancellable_action, completion_handler, pending=pending,
            )
        if result.outcome is PendingInputOutcome.COMPLETE:
            self._pending_inputs.clear(event.reply_address, pending.request_id)
        return result

    def _can_defer(self, event: IncomingEvent, completion_handler: ChannelCompletionHandler | None) -> bool:
        return (completion_handler is not None and event.external_user_id is not None
                and self._deferred_actions is not None and self._deferred_access is not None)

    def _defer_action(
        self, event: IncomingEvent, app_user_id: int, capability_id: str,
        required_mode: str | None, receipt: IncomingResponse, work: CancellableAction,
        completion_handler: ChannelCompletionHandler | None, *, pending: PendingInput | None = None,
    ) -> IncomingResponse:
        """Под блокировкой чата резервирует работу и возвращает квитанцию без сети."""
        actions, may_finish = self._deferred_actions, self._deferred_access
        assert actions is not None and completion_handler is not None and may_finish is not None
        action = actions.reserve(event.reply_address, app_user_id, event.external_user_id)
        if action is None:
            return self.resume_navigation(event, IncomingResponse(text_content(
                "Обработка сейчас занята. Попробуй ещё раз немного позже."
            )))
        if pending is not None and not self._pending_inputs.clear(event.reply_address, pending.request_id):
            actions.cancel(action)
            return self.resume_navigation(event, IncomingResponse(text_content(
                "Ввод уже изменился. Выбери актуальное действие."
            )))
        origin = replace(event, text="принятый ввод", images=())
        pending_inputs = self._pending_inputs
        refresh_menu = self._deferred_menu_refresh

        def finish(response: IncomingResponse) -> None:
            with pending_inputs.lock(action.address):
                if action.cancelled.is_set() or not may_finish(origin, app_user_id, capability_id, required_mode):
                    return
                if not actions.claim_result(action):
                    return
            completion_handler(BackgroundReply(action.address, response))

        def refresh_after_release() -> None:
            if refresh_menu is not None:
                refresh_menu(action.address)

        def after_delivery(delivered: bool) -> None:
            actions.after_delivery(action, delivered, work, finish, refresh_after_release)

        return replace(receipt, suggested_actions=BUSY_COMMAND_MENU.section_actions, after_delivery=after_delivery)

    def _continue_core_input(self, pending: PendingInput, event: IncomingEvent) -> ProcessIncomingMessageResult | IncomingResponse:
        """Повторно проверяет профиль и завершает либо сохраняет исходное ожидание."""
        try:
            identity = _incoming_identity_from_event(event)
            user = self._user_identity_service.resolve(identity)
            if (user.id if user else None) != pending.app_user_id:
                self._pending_inputs.clear(event.reply_address, pending.request_id)
                return self.resume_navigation(event, IncomingResponse(text_content(
                    "Профиль канала изменился. Выбери действие для текущего профиля."
                )))
            personal = self._personal_registry(user, event) if user is not None else None
            if pending.required_mode is not None:
                mode = self._chat_mode_repository.get(ChatModeKey(user.id, event.channel, event.chat_id)) if user else None
                if (mode is None or mode.capability_id != pending.required_mode or personal is None
                        or personal.by_id(pending.required_mode) is None):
                    self._pending_inputs.clear(event.reply_address, pending.request_id)
                    return self.resume_navigation(event, IncomingResponse(text_content(
                        "Контекст ввода изменился. Ожидание отменено. Выбери действие в текущем разделе."
                    )))
            if pending.input_kind in ("enable", "disable") and user is not None and personal is not None:
                command = ChatCommand.ENABLE if pending.input_kind == "enable" else ChatCommand.DISABLE
                return self._change_capability(command, event.text, user, event, personal, pending)
            if pending.input_kind == "link_code":
                if not self._profile_link_allowed(event):
                    self._pending_inputs.clear(event.reply_address, pending.request_id)
                    return IncomingResponse(text_content(_PROFILE_LINK_PRIVATE_HINT))
                result = self._link_identity(event.text.strip(), identity)
                if isinstance(result, ProcessIncomingMessageResult) and result.type is IncomingMessageResultType.LINK_CODE_INVALID:
                    return IncomingResponse(text_content(
                        "Код привязки не принят. Отправь исправленный код до истечения ожидания. "
                        "Новый код можно получить в настройках мессенджеров другого канала."
                    ), result.error)
            elif pending.input_kind == "profile_name" and user is not None:
                result = self._save_name(event.text, user)
                if result.type is IncomingMessageResultType.NAME_COMMAND_INVALID:
                    return IncomingResponse(text_content(
                        "Имя не принято. Используй от 1 до 80 символов без ссылки или команды. "
                        "Отправь исправленное имя до истечения ожидания."
                    ), result.error)
            else:
                result = IncomingResponse(text_content("Этот ввод больше недоступен. Выбери актуальное действие."),
                                          suggested_actions=(_HELP_SCREEN_ACTION,))
        except Exception as error:
            LOGGER.exception("Core pending input failed: kind=%s", pending.input_kind)
            result = IncomingResponse(text_content(
                "Не удалось обработать ввод. Проверь состояние действия перед повторной отправкой."
            ), error)
        self._pending_inputs.clear(event.reply_address, pending.request_id)
        return result

    def _change_capability(
        self, command: ChatCommand, arguments: str, user: AppUser,
        event: IncomingEvent, personal: PersonalCapabilityRegistry,
        pending: PendingInput | None = None, *, required_mode: str | None = None,
    ) -> IncomingResponse:
        """Проверяет общую команду и сохраняет один личный выбор AppUser.

        Args:
            command: Команда включения либо отключения.
            arguments: Строка аргументов после команды.
            user: Профиль, для которого проверен режим команды или исходное ожидание.
            event: Исходное событие для ожидания в конкретном чате.
            personal: Снимок доступа допущенного профиля.
            pending: Продолжаемый ввод либо прямой вызов.
            required_mode: Режим, которому принадлежит новое ожидание.

        Returns:
            Подтверждение собственного commit или понятный отказ без записи.
        """
        enabled = command is ChatCommand.ENABLE
        parts = arguments.split()
        if not parts and pending is None:
            choices = self._profile_choices(personal, enabled)
            if not choices:
                return IncomingResponse(text_content(
                    "Нет возможностей, которые можно включить." if enabled else "Нет включённых возможностей."
                ))
            self._pending_inputs.begin(
                event.reply_address, "core", "enable" if enabled else "disable",
                app_user_id=user.id, required_mode=required_mode, retry_command=command.value,
                external_user_id=event.external_user_id,
            )
            option_lines = []
            actions = []
            action = "Включить" if enabled else "Выключить"
            for choice in choices:
                descriptor = choice.descriptor
                label = _capability_label(descriptor)
                command_text = f"{command.value} {descriptor.capability_id}"
                option_lines.append(f"{label} ({descriptor.capability_id})" if descriptor.display_name else label)
                actions.append(ReplyAction(f"{action} {label}", command_text, purpose=ActionPurpose.SELECTION))
            options = "\n".join(option_lines)
            private = is_private_chat_address(event.channel, event.external_user_id, event.chat_id)
            return IncomingResponse(text_content(
                f"Выбери, что нужно {'включить' if enabled else 'выключить'}:\n{options}\n"
                "Можно прислать ID из списка. Ожидание действует 10 минут."
            ), suggested_actions=tuple(actions) if private else (),
                view=ResponseView("profile-capability-choice") if private else None)
        if len(parts) != 1:
            return IncomingResponse(text_content(
                "Значение не принято. Пришли один ID из списка."
                if pending else "Можно изменить только одну возможность за раз. Открой личные возможности, чтобы выбрать её."
            ))
        access = personal.status(parts[0])
        if access is None or not access.visible:
            return IncomingResponse(text_content(
                'Неизвестная возможность. Пришли ID из списка.' if pending else
                'Неизвестная возможность. Открой личные возможности, чтобы выбрать её.'
            ))
        descriptor = access.descriptor
        if not descriptor.personally_configurable:
            return IncomingResponse(text_content(
                f"Личный доступ к возможности {_capability_label(descriptor)} нельзя изменить."
            ))
        label = _capability_label(descriptor)
        label_content = label if descriptor.display_name else code_span(label)
        if enabled and not access.server_available:
            return IncomingResponse(
                compose_content(
                    'Возможность ',
                    label_content,
                    ' недоступна на сервере.',
                    ' Пришли ID другой возможности из списка.' if pending else '',
                )
            )
        if pending is not None:
            # После допуска запись может иметь неизвестный исход; автоматического повтора нет.
            self._pending_inputs.clear(event.reply_address, pending.request_id)
        try:
            changed = SetUserCapabilityPreference(
                self._capability_preference_repository, self._capability_defaults()
            ).execute(user.id, parts[0], enabled)
        except Exception as error:
            LOGGER.exception("Could not change user capability: capability_id=%s", parts[0])
            return IncomingResponse(compose_content('Не удалось сохранить выбор. Попробуй позже.'), error)
        if enabled and descriptor.display_name:
            action = "включён" if changed else "уже включён"
            return IncomingResponse(text_content(
                f"Личный доступ к возможности {label} {action}. "
                "Выбор действует во всех привязанных каналах."
            ), close_managed_views=True)
        state = "включена" if enabled else "выключена"
        return IncomingResponse(
            compose_content(
                'Возможность ',
                label_content,
                ' ' + str('уже ' if not changed else '') + str(state) + '. Выбор действует во всех привязанных каналах.',
            ), close_managed_views=True,
        )

    @staticmethod
    def _profile_choices(personal: PersonalCapabilityRegistry, enabled: bool) -> tuple[CapabilityAccess, ...]:
        """Отбирает доступные переключения для текущего состояния профиля."""
        return tuple(
            access for access in personal.visible_access
            if access.descriptor.personally_configurable and access.user_enabled != enabled
            and (not enabled or access.server_available)
        )

    def _dispatch(
        self,
        user: AppUser,
        event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None,
        personal: PersonalCapabilityRegistry,
    ) -> ProcessIncomingMessageResult | IncomingResponse:
        """Выбирает вход по команде или ID без анализа предметного содержания."""
        context = HandlerContext(
            user,
            CallbackReplyDelivery(completion_handler) if completion_handler else None,
            self._pending_inputs,
            action_layout=event.action_layout,
        )
        key = ChatModeKey(user.id, event.channel, event.chat_id)
        blocked = personal.blocked_by_command(event.text)
        if blocked is not None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.CAPABILITY_DISABLED,
                app_user=user,
                capability_reply=self._blocked_reply(blocked),
                personal=personal,
            )
        entry = personal.by_command(event.text)
        if entry is not None:
            state = self._chat_mode_repository.get_state(key)
            try:
                parts = event.text.strip().split(maxsplit=1)
                result = entry.enter(context, event, parts[1] if len(parts) > 1 else "")
            except Exception as error:
                LOGGER.exception(
                    "Capability entry failed: capability_id=%s",
                    entry.descriptor.capability_id,
                )
                return IncomingResponse(
                    text_content("Не удалось открыть раздел. Попробуй ещё раз."), error,
                    suggested_actions=(replace(capability_entry_action(entry.descriptor), scope=ActionScope.SCREEN),
                                       _HELP_SCREEN_ACTION),
                )
            # Одноразовый вход и ошибка никогда не заменяют прежний режим.
            if (
                result.response.error is None
                and result.mode is CapabilityMode.ACTIVE
            ):
                self._chat_mode_repository.replace_if_revision(
                    key, state.revision, entry.descriptor.capability_id
                )
            response = result.response
            return self._unavailable_view(event, response, response.view) if response.view is not None else response
        state = self._chat_mode_repository.get_state(key)
        active_access = personal.status(state.capability_id) if state.capability_id else None
        if active_access is not None and active_access.profile_allowed and not active_access.chat_allowed:
            # Такой режим мог остаться от прежней сборки в чате, где вход закрыт.
            self._chat_mode_repository.replace_if_revision(key, state.revision, None)
            return self.resume_navigation(event, IncomingResponse(text_content(self._blocked_reply(active_access))))
        parts = event.text.split(maxsplit=1)
        owner = self._registry.mode_command_owner(parts[0]) if parts else None
        if owner is not None:
            access = personal.status(owner.capability_id)
            if access is not None and not access.effective:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.CAPABILITY_DISABLED,
                    app_user=user,
                    capability_reply=self._blocked_reply(access),
                    personal=personal,
                )
            if state.capability_id != owner.capability_id:
                return IncomingResponse(
                    text_content("Открой раздел, чтобы продолжить."),
                    suggested_actions=(replace(capability_entry_action(owner), scope=ActionScope.SCREEN), _HELP_SCREEN_ACTION),
                )
        if state.capability_id is None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_HINT,
                app_user=user,
                capability_reply=_TEXT_INPUT_HINT if event.images else personal.entry_hint(),
                personal=personal,
            )
        capability = personal.by_id(state.capability_id)
        if event.images and (capability is None or not capability.descriptor.accepts_images):
            return self.resume_navigation(event, IncomingResponse(text_content(_TEXT_INPUT_HINT)))
        if capability is None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_UNAVAILABLE,
                app_user=user,
                capability_reply=(
                    f"{self._blocked_reply(active_access)}\n{personal.entry_hint()}"
                    if active_access is not None else personal.entry_hint()
                ),
                personal=personal,
            )
        try:
            context = replace(context, mode_revision=state.revision)
            if capability.descriptor.profile_commands:
                context = replace(context, profile_operation=lambda command, arguments: self._profile_operation(
                    command, arguments, user, event, capability.descriptor.capability_id, personal,
                ))
            result = capability.handle(context, event)
            if result.cancellable_action is not None:
                if not self._can_defer(event, completion_handler):
                    return result.cancellable_action(lambda: False)
                with self._pending_inputs.lock(event.reply_address):
                    if busy := self._busy_response(event, allow_cancel=False):
                        return busy
                    capability_id = capability.descriptor.capability_id
                    if not self.may_finish_deferred(event, user.id, capability_id, capability_id):
                        return self.resume_navigation(event, IncomingResponse(text_content(
                            "Контекст сообщения изменился. Выбери актуальное действие."
                        )))
                    return self._defer_action(
                        event, user.id, capability_id, capability_id, result.response,
                        result.cancellable_action, completion_handler,
                    )
        except Exception as error:
            LOGGER.exception(
                "Capability handling failed: capability_id=%s",
                capability.descriptor.capability_id,
            )
            return IncomingResponse(
                compose_content(
                    'Не удалось обработать сообщение. Проверь состояние действия перед повторной отправкой.',
                ),
                error,
            )
        if result.response.error is None and result.mode is CapabilityMode.COMPLETE:
            self._chat_mode_repository.replace_if_revision(key, state.revision, None)
        return result.response

    def _contextual_help(self, event: IncomingEvent) -> IncomingResponse:
        """Собирает контекст справки, не выполняя ожидаемый ввод или подтверждение."""
        from compadre_assistant.core.presentation.incoming.help import format_contextual_help

        try:
            context = self._command_context(event, include_menu=True)
        except Exception:
            LOGGER.exception("Could not read section actions for contextual help")
            context = self._command_context(event)
        personal, mode_id, pending, snapshot, active_help, section_actions = context
        response = format_contextual_help(
            personal, mode_id, pending, snapshot, self._confirmation_response(snapshot, show_commands=True),
            active_help=active_help, allow_link=self._profile_link_allowed(event),
        )
        common_screen = mode_id is None or (pending is not None and pending.owner_id == "core")
        actions = (main_actions(personal, pending, allow_link=self._profile_link_allowed(event))
                   if common_screen else section_actions or ())
        return replace(response, suggested_actions=actions, preserve_source_actions=True)

    def command_menu(self, event: IncomingEvent) -> CommandMenu:
        """Возвращает актуальное меню чата, не публикуя и не выполняя его.

        Args:
            event: Identity и адрес чата; текст события не исполняется.

        Returns:
            Независимый от канала список команд и тип ожидаемого значения.
            Просроченный или недоступный ввод очищается, как при /help.
        """
        return self.command_menu_snapshot(event)[0]

    def command_menu_snapshot(
        self, event: IncomingEvent, *, known_user: AppUser | None = None,
        preserve_expired: bool = False, cleanup_pending: bool = True,
    ) -> tuple[CommandMenu, bool]:
        """Возвращает меню и полноту снимка подтверждений для безопасной публикации.

        Args:
            event: Сохранённый адрес и identity; текст не исполняется.
            known_user: Повторно проверенный владелец адреса без обновления identity.
            preserve_expired: Сохранить просроченную запись для фонового уведомления.
            cleanup_pending: Удалять утратившее силу ожидание при подготовке меню.

        Returns:
            Меню и признак успешного чтения всех источников подтверждений.
        """
        return self._command_menu_snapshot(
            event, known_user=known_user, preserve_expired=preserve_expired, cleanup_pending=cleanup_pending,
        )

    def _section_actions_for_context(
        self, event: IncomingEvent, user: AppUser, active: SectionActionsQueryPort,
        mode_id: str, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Читает действия уже выбранного владельца, проверяя неизменность допуска."""
        before = self.action_context(event)
        if ((before.app_user_id, before.mode_id, before.mode_revision) != (user.id, mode_id, mode_revision)
                or not any(identifier == mode_id and server and enabled and allowed
                           for identifier, server, enabled, allowed in before.access)):
            raise RuntimeError("section action owner changed before query")
        if pending is not None and pending.cancel_only_menu:
            return CANCEL_SECTION_ACTION, HELP_SECTION_ACTION
        actions = active.section_actions(user, event.reply_address, before.mode_revision, pending)
        if any(action.scope != ActionScope.SECTION for action in actions):
            raise ValueError("section action query returned an action belonging to a screen")
        if self.action_context(event) != before:
            raise RuntimeError("section action context changed during query")
        # Адресные подтверждения требуют публичного снимка и не исполняются
        # через подпись нижнего меню; ссылка вообще не является командой.
        if not self._profile_link_allowed(event):
            actions = tuple(action for action in actions if self._group_action_allowed(action.command))
        return tuple(action for action in actions if action.url is not None or (
            action.command.split()[0] != "/confirm" or len(action.command.split()) == 1
        ) and self._suggestion_allowed(action.command, before))

    def may_notify_input_expired(
        self, event: IncomingEvent, pending: PendingInput, *, known_user: AppUser | None,
    ) -> bool:
        """Проверяет, применим ли совет повторить просроченную команду.

        Args:
            event: Проверенный адрес сохранённого личного чата.
            pending: Запись, захваченная под блокировкой чата.
            known_user: Текущий профиль адреса либо None до регистрации.

        Returns:
            True, если срок истёк и исходный сценарий доступен в этом чате.
        """
        if not pending.retry_command or not self._pending_inputs.expired(pending):
            return False
        personal = self._personal_registry(known_user, event) if known_user is not None else None
        mode = self._chat_mode_repository.get(
            ChatModeKey(known_user.id, event.channel, event.chat_id)
        ) if known_user is not None else None
        return self._pending_is_applicable(event, pending, known_user, personal,
                                           mode.capability_id if mode is not None else None)

    def _command_menu_snapshot(
        self, event: IncomingEvent, *, known_user: AppUser | None = None, cleanup_pending: bool = True,
        preserve_expired: bool = False,
    ) -> tuple[CommandMenu, bool]:
        """Учитывает текущую работу в той же проекции, что ожидание и режим."""
        actions = self._deferred_actions
        if actions is not None and actions.get(event.reply_address) is not None:
            return BUSY_COMMAND_MENU, True
        context = self._command_context(event, known_user=known_user, cleanup_pending=cleanup_pending, include_menu=True,
                                        preserve_expired=preserve_expired)
        # Работа может быть принята во время чтения источников подтверждений.
        if actions is not None and actions.get(event.reply_address) is not None:
            return BUSY_COMMAND_MENU, True
        personal, mode_id, pending, snapshot, active_help, section_actions = context
        complete = (pending is not None and pending.cancel_only_menu) or snapshot.status == "complete"
        menu = build_command_menu(personal, mode_id, pending, snapshot, active_help=active_help,
                                  allow_link=self._profile_link_allowed(event))
        if personal is not None and pending is None and not any(item.command == "/help" for item in menu.items):
            menu = replace(menu, items=(*menu.items, CommandSuggestion("/help", ChatCommand.HELP.description)))
        return replace(menu, section_actions=section_actions), complete

    def _command_context(
        self, event: IncomingEvent, *, cleanup_pending: bool = True,
        known_user: AppUser | None = None, preserve_expired: bool = False, include_menu: bool = False,
    ) -> tuple[
        PersonalCapabilityRegistry | None, str | None, PendingInput | None, ConfirmationQueryResult,
        CapabilityHelp | None, tuple[ReplyAction, ...] | None,
    ]:
        """Проверяет единый контекст справки и меню без продления ожиданий."""
        user = known_user or self._user_identity_service.resolve(_incoming_identity_from_event(event))
        personal = self._personal_registry(user, event) if user is not None else None
        mode = self._chat_mode_repository.get_state(
            ChatModeKey(user.id, event.channel, event.chat_id)
        ) if user is not None else None
        valid_pending = None
        with self._pending_inputs.lock(event.reply_address):
            pending = self._pending_for_event(event)
            if pending is not None:
                applicable = self._pending_is_applicable(
                    event, pending, user, personal, mode.capability_id if mode else None,
                )
                expired = self._pending_inputs.expired(pending)
                if applicable and not expired:
                    valid_pending = pending
                if cleanup_pending and (expired or not applicable):
                    if not (preserve_expired and expired and applicable and pending.retry_command):
                        self._pending_inputs.clear(event.reply_address, pending.request_id)
                    pending = None
        active = (
            personal.by_id(mode.capability_id)
            if personal is not None and mode is not None and mode.capability_id is not None else None
        )
        mode_id = personal.visible_mode(mode.capability_id) if personal is not None and mode else None
        section_actions = None
        if include_menu:
            if active is None:
                section_actions = main_actions(personal, valid_pending, allow_link=self._profile_link_allowed(event))
                if mode_id and valid_pending is None:
                    section_actions = (HOME_SECTION_ACTION, *section_actions)
            elif (user is not None and mode is not None and isinstance(active, SectionActionsQueryPort)
                  and (self._profile_link_allowed(event)
                       or (active.descriptor.private_chat_hint is None and self._group_action_address(event)))):
                section_actions = self._section_actions_for_context(
                    event, user, active, mode.capability_id, mode.revision, valid_pending,
                )
                if section_actions and not any(action.command == "/help" for action in section_actions):
                    section_actions = (*section_actions, HELP_SECTION_ACTION)
        snapshot = self._confirmation_snapshot(event, user, active, mode_revision=mode.revision if mode else None)
        active_help = (
            self._project_command_help(active, user, event.reply_address, mode.revision, personal)
            if active is not None and user is not None and mode is not None else None
        )
        if active is not None and active.descriptor.profile_commands and not self._profile_link_allowed(event):
            catalog = active_help or active.descriptor.help
            sections = tuple(replace(section, commands=commands) for section in catalog.sections
                             if (commands := tuple(item for item in section.commands
                                                   if item.name not in {"/connect", "/link"})))
            active_help = replace(catalog, sections=sections)
        return personal, mode_id, pending, snapshot, active_help, section_actions

    def _project_command_help(
        self, capability: IncomingCapabilityPort, user: AppUser, address: ReplyAddress, mode_revision: int,
        personal: PersonalCapabilityRegistry,
    ) -> CapabilityHelp | None:
        if not capability.descriptor.persistent_mode or not isinstance(capability, ContextualCommandQueryPort):
            return None
        try:
            names = capability.visible_mode_commands(user, address, mode_revision)
            for command, enabled in ((ChatCommand.ENABLE, True), (ChatCommand.DISABLE, False)):
                if command in names and not self._profile_choices(personal, enabled):
                    names -= {command.value}
            catalog = capability.descriptor.help
            sections = tuple(
                replace(section, commands=commands)
                for section in catalog.sections
                if (commands := tuple(command for command in section.commands if command.name in names))
            )
            entry = capability.descriptor.entry
            if entry.name in names and not any(item.name == entry.name for section in sections for item in section.commands):
                sections += (HelpSection("Навигация", (entry,)),)
            return replace(catalog, sections=sections)
        except Exception:
            LOGGER.exception("Could not read contextual commands: capability_id=%s", capability.descriptor.capability_id)
            return CapabilityHelp()

    def _pending_is_applicable(
        self, event: IncomingEvent, pending: PendingInput, user: AppUser | None,
        personal: PersonalCapabilityRegistry | None, mode_id: str | None,
    ) -> bool:
        """Проверяет владельца и доступ к сценарию независимо от его срока."""
        if pending.app_user_id != (user.id if user else None) and (
            pending.owner_id == "core" or pending.app_user_id is not None
        ):
            return False
        if pending.required_mode is not None and (
            mode_id != pending.required_mode or personal is None or personal.by_id(pending.required_mode) is None
        ):
            return False
        if pending.owner_id != "core":
            owner = personal.by_id(pending.owner_id) if personal else None
            return isinstance(owner, PendingInputContinuationPort)
        return pending.input_kind != "link_code" or self._profile_link_allowed(event)

    def list_pending_confirmations(self, event: IncomingEvent) -> ConfirmationQueryResult:
        """Собирает снимок ядра и доступного активного режима без выбора решения.

        Args:
            event: Исходная identity и адрес чата, включая канал без профиля.

        Returns:
            Полный снимок либо известная часть с признаком недоступности источников.
        """
        user = None
        active = None
        mode_revision = None
        complete = True
        try:
            user = self._user_identity_service.resolve(_incoming_identity_from_event(event))
            if user is not None:
                personal = self._personal_registry(user, event)
                mode = self._chat_mode_repository.get_state(ChatModeKey(user.id, event.channel, event.chat_id))
                if mode.capability_id is not None:
                    active = personal.by_id(mode.capability_id)
                    mode_revision = mode.revision
        except Exception:
            LOGGER.exception("Could not resolve confirmation context")
            complete = False
        return self._confirmation_snapshot(event, user, active, complete, mode_revision=mode_revision)

    def _confirmation_snapshot(
        self, event: IncomingEvent, user: AppUser | None,
        capability: IncomingCapabilityPort | None, complete: bool = True, *, mode_revision: int | None,
    ) -> ConfirmationQueryResult:
        """Опрашивает источники по одному снимку допуска, сохраняя известные действия."""
        identity = _incoming_identity_from_event(event)
        confirmations: list[PendingConfirmation] = []
        try:
            pending = self._user_identity_service.get_pending_rebind(identity) if self._profile_link_allowed(event) else None
            if (pending is not None and pending.channel == identity.channel
                    and pending.chat_id == identity.external_chat_id
                    and pending.external_user_id == identity.external_user_id):
                actions = _rebind_actions(pending)
                confirmations.append(PendingConfirmation(
                    "core", "identity", pending.id, pending.version, event.reply_address, actions,
                ))
        except Exception:
            LOGGER.exception("Could not query identity confirmations")
            complete = False
        try:
            # Недоступный режим не является источником: его действия нельзя выполнить.
            if capability is not None and capability.descriptor.persistent_mode and user is not None:
                if not isinstance(capability, PendingConfirmationQueryPort):
                    complete = False
                else:
                    result = capability.list_pending_confirmations(
                        HandlerContext(user, mode_revision=mode_revision), event.reply_address,
                    )
                    complete = complete and result.status == "complete"
                    confirmations.extend(item for item in result.confirmations
                                         if item.origin == event.reply_address
                                         and item.owner_id == capability.descriptor.capability_id)
        except Exception:
            LOGGER.exception("Could not query active capability confirmations")
            complete = False
        return ConfirmationQueryResult("complete" if complete else "unavailable", tuple(confirmations))

    @staticmethod
    def _confirmation_response(result: ConfirmationQueryResult, *, show_commands: bool = False) -> IncomingResponse:
        """Показывает обе стороны каждого решения с явной оговоркой неполноты."""
        if not result.confirmations:
            return IncomingResponse(text_content(
                "В этом чате нет действий, которые нужно подтвердить."
                if result.status == "complete" else
                "Не удалось получить полный список действий для подтверждения. Попробуй позже."
            ), suggested_actions=() if show_commands else (_HELP_SCREEN_ACTION,))
        prefix = ("Список действий неполный: часть источников подтверждений недоступна.\n"
                  if result.status != "complete" else "")
        actions = tuple(action for item in result.confirmations for action in item.actions)
        content = text_content(prefix + "Доступные действия:")
        if show_commands:
            content = MessageContent((*content.blocks, *(confirmation_action_paragraph(action) for action in actions)))
        return IncomingResponse(content, actions=actions,
                                suggested_actions=() if show_commands else (_HELP_SCREEN_ACTION,))

    def _save_name(
        self,
        display_name: str,
        user: AppUser,
    ) -> ProcessIncomingMessageResult:
        """Меняет имя уже зарегистрированного профиля."""
        try:
            updated = self._user_identity_service.update_display_name(
                app_user_id=user.id, display_name=display_name
            )
        except (InvalidDisplayNameError, ValueError) as error:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.NAME_COMMAND_INVALID,
                app_user=user,
                error=error,
            )
        return ProcessIncomingMessageResult(
            type=IncomingMessageResultType.NAME_UPDATED,
            app_user=updated,
        )


def _incoming_identity_from_event(event: IncomingEvent) -> IncomingIdentity:
    """Преобразует адрес события в общую модель identity."""
    return IncomingIdentity(
        channel=event.channel,
        external_user_id=event.external_user_id or event.chat_id,
        external_chat_id=event.chat_id,
        username=event.username,
        display_name=event.display_name,
    )
