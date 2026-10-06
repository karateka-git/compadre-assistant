from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import logging

from obs_chat_bot.application.incoming.commands import ChatCommand, ParsedChatCommand
from obs_chat_bot.application.incoming.events import IncomingEvent
from obs_chat_bot.application.incoming.ports import CapabilityHelp, IncomingCapabilityPort, IncomingResult, OnboardingReplyKind, OnboardingState
from obs_chat_bot.application.users.identity import (
    CreatedLinkCode,
    IdentityAlreadyBoundError,
    InvalidDisplayNameError,
    InvalidLinkCodeError,
    UserIdentityService,
)
from obs_chat_bot.domain.users.entities import AppUser, IncomingIdentity


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


IncomingCompletionHandler = Callable[[IncomingResult], None]


class ProcessIncomingMessageUseCase:
    """Разрешает identity, общие команды и приоритет перепривязки канала."""

    def __init__(
        self,
        *,
        incoming_capability: IncomingCapabilityPort,
        user_identity_service: UserIdentityService | None = None,
    ) -> None:
        self._incoming_capability = incoming_capability
        self._user_identity_service = user_identity_service

    def execute(
        self,
        event: IncomingEvent,
        completion_handler: IncomingCompletionHandler | None = None,
    ) -> IncomingResult:
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
        if command is ChatCommand.HELP:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.HELP,
                capability_help=self._incoming_capability.help(),
            )
        if self._user_identity_service is None:
            return self._incoming_capability.handle(
                AppUser(id=1), event, completion_handler
            )

        identity = _incoming_identity_from_event(event)
        normalized = text.lower()
        if normalized in {"да", "yes", "y", "нет", "no", "n"}:
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
                    capability_reply=self._incoming_capability.confirmation_missing_reply(),
                )
            return self._incoming_capability.handle(user, event, completion_handler)

        if self._user_identity_service.has_pending_rebind(identity):
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
            state = self._incoming_capability.onboarding_state(user)
            return ProcessIncomingMessageResult(
                type=(
                    IncomingMessageResultType.START_REGISTERED
                    if state.ready
                    else IncomingMessageResultType.ONBOARDING_REQUIRED
                ),
                app_user=user,
                onboarding=state,
                capability_reply=self._incoming_capability.onboarding_reply(
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
            state = self._incoming_capability.onboarding_state(existing)
            return ProcessIncomingMessageResult(
                type=(
                    IncomingMessageResultType.ALREADY_REGISTERED
                    if state.ready
                    else IncomingMessageResultType.ONBOARDING_REQUIRED
                ),
                app_user=existing,
                onboarding=state,
                capability_reply=self._incoming_capability.onboarding_reply(
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
                registration=False,
            )
        if user.display_name is None:
            if command is not None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            return self._save_name(text, user, registration=True)
        if command is ChatCommand.STATUS:
            return self._incoming_capability.status(user)
        return self._incoming_capability.handle(user, event, completion_handler)

    def _save_name(
        self,
        display_name: str,
        user: AppUser,
        *,
        registration: bool,
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
        return ProcessIncomingMessageResult(
            type=(
                IncomingMessageResultType.REGISTRATION_NAME_SAVED
                if registration
                else IncomingMessageResultType.NAME_UPDATED
            ),
            app_user=updated,
            onboarding=(
                self._incoming_capability.onboarding_state(updated)
                if registration
                else None
            ),
            capability_reply=(
                self._incoming_capability.onboarding_reply(OnboardingReplyKind.NAME_SAVED, updated)
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
