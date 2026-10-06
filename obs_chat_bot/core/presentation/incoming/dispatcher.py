from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging

from obs_chat_bot.core.presentation.incoming.commands import ChatCommand, ParsedChatCommand
from obs_chat_bot.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from obs_chat_bot.core.presentation.incoming.ports import (
    BackgroundReply, CapabilityHelp, CapabilityMode, ChannelCompletionHandler,
    HandlerContext, IncomingResponse, OnboardingReplyKind, OnboardingState,
    ProfileSetupPort, ReplyDeliveryPort,
)
from obs_chat_bot.core.presentation.incoming.registry import CapabilityRegistry
from obs_chat_bot.core.domain.users.identity import (
    CreatedLinkCode,
    IdentityAlreadyBoundError,
    InvalidDisplayNameError,
    InvalidLinkCodeError,
    UserIdentityService,
)
from obs_chat_bot.core.domain.users.entities import AppUser, IncomingIdentity
from obs_chat_bot.core.domain.chat_modes import ChatMode, ChatModeKey, ChatModeRepository


LOGGER = logging.getLogger(__name__)


class IncomingMessageResultType(StrEnum):
    """Тип результата обработки входящего сообщения."""

    UNKNOWN_IDENTITY = "unknown_identity"
    HELP = "help"
    START_UNREGISTERED = "start_unregistered"
    START_REGISTERED = "start_registered"
    REGISTERED = "registered"
    ALREADY_REGISTERED = "already_registered"
    REGISTRATION_NAME_REQUIRED = "registration_name_required"
    REGISTRATION_NAME_SAVED = "registration_name_saved"
    NAME_COMMAND_INVALID = "name_command_invalid"
    NAME_UPDATED = "name_updated"
    CONFIRMATION_MISSING = "confirmation_missing"
    LINK_CODE_CREATED = "link_code_created"
    LINK_COMMAND_INVALID = "link_command_invalid"
    LINKED = "linked"
    LINK_ALREADY_BOUND = "link_already_bound"
    LINK_CODE_INVALID = "link_code_invalid"
    LINK_REBIND_CONFIRMATION_REQUIRED = "link_rebind_confirmation_required"
    LINK_REBOUND = "link_rebound"
    LINK_REBIND_CANCELLED = "link_rebind_cancelled"
    LINK_REBIND_CONFIRMATION_MISSING = "link_rebind_confirmation_missing"
    LINK_REBIND_CONFIRMATION_PENDING = "link_rebind_confirmation_pending"
    ONBOARDING_REQUIRED = "onboarding_required"
    MODE_HINT = "mode_hint"  # В этом чате возможность ещё не выбрана.
    MODE_EXITED = "mode_exited"  # Выбор очищен только для указанного чата.
    PROFILE_STATUS = "profile_status"  # Общие сведения о пользователе и выбранном режиме.
    MODE_UNAVAILABLE = "mode_unavailable"  # Сохранённый маршрут не подключён к текущему входу.


@dataclass(frozen=True, slots=True)
class ProcessIncomingMessageResult:
    """Хранит ответ общего входа без моделей подключённых возможностей."""

    type: IncomingMessageResultType
    app_user: AppUser | None = None
    link_code: CreatedLinkCode | None = None
    onboarding: OnboardingState | None = None
    error: Exception | None = None
    capability_reply: str | None = None
    capability_help: CapabilityHelp | None = None


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
        profile_setup: Прежний onboarding до отдельного шага 2.3.
        registration_capability_id: Вход прежнего onboarding после задания имени.
        user_identity_service: Общий сервис профиля и связей каналов.
        chat_mode_repository: Обязательное хранилище выбора конкретного чата.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        profile_setup: ProfileSetupPort | None = None,
        registration_capability_id: str | None = None,
        user_identity_service: UserIdentityService,
        chat_mode_repository: ChatModeRepository,
    ) -> None:
        """Внедряет общий реестр, repositories и временную политику onboarding."""
        if (
            registration_capability_id is not None
            and registry.by_id(registration_capability_id) is None
        ):
            raise ValueError(
                f"Не включён регистрационный вход: {registration_capability_id}"
            )
        self._registry = registry
        self._profile_setup = profile_setup
        self._registration_capability_id = registration_capability_id
        self._user_identity_service = user_identity_service
        self._chat_mode_repository = chat_mode_repository

    def execute(
        self,
        event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None = None,
    ) -> IncomingResponse:
        """Возвращает каналу общий ответ, сохраняя предметные модели внутри возможности.

        Args:
            event: Нормализованное событие канала.
            completion_handler: Доставка последующего ответа по сохранённому адресу.

        Returns:
            Готовое содержимое ответа и необязательные адресные действия.
        """
        from obs_chat_bot.core.presentation.responses import format_incoming_message_result

        result = self._execute(event, completion_handler)
        if isinstance(result, IncomingResponse):
            return result
        return IncomingResponse(format_incoming_message_result(result), result.error)

    def _execute(
        self,
        event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None = None,
    ) -> ProcessIncomingMessageResult | IncomingResponse:
        """Обрабатывает общую часть сообщения и передаёт предметный текст.

        Args:
            event: Нормализованное событие канала без внутреннего ID пользователя.
            completion_handler: Доставка результата фоновой операции в исходный чат.

        Returns:
            Общий ответ или ответ зарегистрированной возможности.
        """
        text = event.text.strip()
        parsed = ParsedChatCommand.parse(text)
        command = parsed.command if parsed is not None else None
        is_general_entry = command is not None or self._registry.by_command(text) is not None
        if command is ChatCommand.HELP:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.HELP,
                capability_help=self._registry.help(),
            )
        identity = _incoming_identity_from_event(event)
        normalized = text.lower()
        if not is_general_entry and normalized in {"да", "yes", "y", "нет", "no", "n"}:
            if self._user_identity_service.has_pending_rebind(identity):
                if normalized in {"да", "yes", "y"}:
                    try:
                        user = self._user_identity_service.confirm_rebind(identity)
                    except InvalidLinkCodeError as error:
                        return ProcessIncomingMessageResult(
                            type=IncomingMessageResultType.LINK_REBIND_CONFIRMATION_MISSING,
                            error=error,
                        )
                    return ProcessIncomingMessageResult(
                        type=IncomingMessageResultType.LINK_REBOUND,
                        app_user=user,
                    )
                self._user_identity_service.cancel_rebind(identity)
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.LINK_REBIND_CANCELLED
                )
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.CONFIRMATION_MISSING,
                )
            return self._dispatch(user, event, completion_handler)

        if not is_general_entry and self._user_identity_service.has_pending_rebind(identity):
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.LINK_REBIND_CONFIRMATION_PENDING
            )

        if command is ChatCommand.START:
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.START_UNREGISTERED
                )
            if user.display_name is None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            state = self._onboarding_state(user)
            return ProcessIncomingMessageResult(
                type=(
                    IncomingMessageResultType.START_REGISTERED
                    if state.ready
                    else IncomingMessageResultType.ONBOARDING_REQUIRED
                ),
                app_user=user,
                onboarding=state,
                capability_reply=self._onboarding_reply(
                    OnboardingReplyKind.START if state.ready else OnboardingReplyKind.REQUIRED,
                    user,
                ),
            )

        if command is ChatCommand.REGISTER:
            existing = self._user_identity_service.resolve(identity)
            if existing is None:
                user = self._user_identity_service.register(identity)
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTERED,
                    app_user=user,
                )
            if existing.display_name is None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=existing,
                )
            state = self._onboarding_state(existing)
            return ProcessIncomingMessageResult(
                type=(
                    IncomingMessageResultType.ALREADY_REGISTERED
                    if state.ready
                    else IncomingMessageResultType.ONBOARDING_REQUIRED
                ),
                app_user=existing,
                onboarding=state,
                capability_reply=self._onboarding_reply(
                    OnboardingReplyKind.ALREADY_REGISTERED
                    if state.ready
                    else OnboardingReplyKind.REQUIRED,
                    existing,
                ),
            )

        if command is ChatCommand.LINK:
            if parsed is None or not parsed.arguments:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.LINK_COMMAND_INVALID
                )
            try:
                user = self._user_identity_service.link(
                    code=parsed.arguments, identity=identity
                )
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.LINKED,
                    app_user=user,
                )
            except IdentityAlreadyBoundError as error:
                try:
                    target = self._user_identity_service.request_rebind_confirmation(
                        code=parsed.arguments, identity=identity
                    )
                    return ProcessIncomingMessageResult(
                        type=IncomingMessageResultType.LINK_REBIND_CONFIRMATION_REQUIRED,
                        app_user=target,
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

        user = self._user_identity_service.resolve(identity)
        if user is None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.UNKNOWN_IDENTITY
            )
        if command is ChatCommand.EXIT:
            self._chat_mode_repository.clear(ChatModeKey(user.id, event.channel, event.chat_id))
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_EXITED,
                app_user=user,
                capability_reply=self._registry.entry_hint(),
            )
        if command is ChatCommand.LINK_CODE:
            if user.display_name is None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            link_code = self._user_identity_service.create_link_code(user.id)
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.LINK_CODE_CREATED,
                app_user=user,
                link_code=link_code,
            )
        if command is ChatCommand.NAME:
            return self._save_name(
                parsed.arguments if parsed is not None else "", user,
                registration=user.display_name is None, event=event,
            )
        if command is ChatCommand.STATUS:
            mode = self._chat_mode_repository.get(
                ChatModeKey(user.id, event.channel, event.chat_id)
            )
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.PROFILE_STATUS,
                app_user=user,
                capability_reply=(
                    f"Профиль: {user.display_name or 'имя ещё не задано'}.\n"
                    f"Режим этого чата: {mode.capability_id if mode else 'не выбран'}.\n"
                    f"{self._registry.entry_hint()}"
                ),
            )
        if user.display_name is None:
            if command is not None or self._registry.by_command(text) is not None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            return self._save_name(text, user, registration=True, event=event)
        return self._dispatch(user, event, completion_handler)

    def _dispatch(
        self,
        user: AppUser,
        event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None,
    ) -> ProcessIncomingMessageResult | IncomingResponse:
        """Выбирает вход по команде или ID без анализа предметного содержания."""
        context = HandlerContext(
            user,
            CallbackReplyDelivery(completion_handler) if completion_handler else None,
        )
        key = ChatModeKey(user.id, event.channel, event.chat_id)
        entry = self._registry.by_command(event.text)
        if entry is not None:
            try:
                result = entry.enter(context, event)
            except Exception as error:
                LOGGER.exception(
                    "Capability entry failed: capability_id=%s",
                    entry.descriptor.capability_id,
                )
                return IncomingResponse(
                    "Не удалось войти в возможность. Попробуй позже или отправь `/help`.",
                    error,
                )
            # Одноразовый вход и ошибка никогда не заменяют прежний режим.
            if (
                result.response.error is None
                and result.mode is CapabilityMode.ACTIVE
            ):
                self._chat_mode_repository.save(ChatMode(key, entry.descriptor.capability_id))
            return result.response
        mode = self._chat_mode_repository.get(key)
        if mode is None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_HINT,
                app_user=user,
                capability_reply=self._registry.entry_hint(),
            )
        capability = self._registry.by_id(mode.capability_id)
        if capability is None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_UNAVAILABLE,
                app_user=user,
                capability_reply=self._registry.entry_hint(),
            )
        result = capability.handle(context, event)
        if result.mode is CapabilityMode.COMPLETE:
            self._chat_mode_repository.clear(key)
        return result.response

    def _onboarding_state(self, user: AppUser) -> OnboardingState:
        """Читает прежнюю готовность настройки через отдельный контракт."""
        if self._profile_setup is None:
            return OnboardingState(True)
        return self._profile_setup.onboarding_state(user)

    def _onboarding_reply(self, kind: OnboardingReplyKind, user: AppUser) -> str:
        """Добавляет доступный вход к прежнему ответу настройки профиля."""
        reply = (
            self._profile_setup.onboarding_reply(kind, user)
            if self._profile_setup
            else "Профиль сохранён."
        )
        return f"{reply}\n{self._registry.entry_hint()}"

    def _save_name(
        self,
        display_name: str,
        user: AppUser,
        *,
        registration: bool,
        event: IncomingEvent,
    ) -> ProcessIncomingMessageResult:
        """Сохраняет имя и запрашивает готовность выбранной возможности."""
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
        if registration and self._registration_capability_id is not None:
            # До шага 2.3 первоначальное имя продолжает прежний onboarding в этом чате.
            entry = self._registry.by_id(self._registration_capability_id)
            if entry is not None:
                self._chat_mode_repository.save(
                    ChatMode(
                        ChatModeKey(updated.id, event.channel, event.chat_id),
                        entry.descriptor.capability_id,
                    )
                )
        return ProcessIncomingMessageResult(
            type=(
                IncomingMessageResultType.REGISTRATION_NAME_SAVED
                if registration
                else IncomingMessageResultType.NAME_UPDATED
            ),
            app_user=updated,
            onboarding=(
                self._onboarding_state(updated)
                if registration
                else None
            ),
            capability_reply=(
                self._onboarding_reply(OnboardingReplyKind.NAME_SAVED, updated)
                if registration
                else None
            ),
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
