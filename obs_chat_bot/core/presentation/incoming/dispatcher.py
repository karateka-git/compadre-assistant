from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging

from obs_chat_bot.core.presentation.incoming.commands import ChatCommand, ParsedChatCommand
from obs_chat_bot.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from obs_chat_bot.core.presentation.incoming.ports import (
    ConfirmationAction, BackgroundReply, CapabilityHelp, CapabilityMode, ChannelCompletionHandler,
    HandlerContext, IncomingResponse, ReplyDeliveryPort,
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
        user_identity_service: Общий сервис профиля и связей каналов.
        chat_mode_repository: Обязательное хранилище выбора конкретного чата.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        user_identity_service: UserIdentityService,
        chat_mode_repository: ChatModeRepository,
    ) -> None:
        """Внедряет общий реестр и repositories независимого профиля."""
        self._registry = registry
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
        is_general_entry = (
            command is not None
            or self._registry.by_command(text) is not None
            or self._registry.disabled_by_command(text) is not None
        )
        if command is ChatCommand.HELP:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.HELP,
                capability_help=self._registry.help(),
            )
        identity = _incoming_identity_from_event(event)
        if command is ChatCommand.CONFIRM:
            parts = parsed.arguments.split() if parsed else []
            if parts and parts[0] == "core":
                if (
                    len(parts) != 5
                    or parts[1] != "identity"
                    or not parts[2].isdigit()
                    or not parts[3].isdigit()
                    or parts[4] not in {"yes", "no"}
                ):
                    return IncomingResponse(
                        "Используй адресную команду из актуального запроса привязки."
                    )
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
                except InvalidLinkCodeError as error:
                    return IncomingResponse(
                        "Действие недоступно, устарело или истекло. Повтори /link в исходном чате.",
                        error,
                    )
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(IncomingMessageResultType.UNKNOWN_IDENTITY)
            mode = self._chat_mode_repository.get(ChatModeKey(user.id, event.channel, event.chat_id))
            target = parts[0] if parts else ""
            if mode is None or mode.capability_id != target:
                capability = self._registry.by_id(target)
                return IncomingResponse(
                    f"Войди снова через {capability.descriptor.entry_command}, чтобы завершить действие в исходном чате."
                    if capability
                    else self._registry.entry_hint()
                )
            return self._dispatch(user, event, completion_handler)

        if command is ChatCommand.START:
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(type=IncomingMessageResultType.START_UNREGISTERED)
            if user.display_name is None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.START_REGISTERED,
                app_user=user,
                capability_reply=self._registry.entry_hint(),
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
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.ALREADY_REGISTERED,
                app_user=existing,
                capability_reply=self._registry.entry_hint(),
            )

        if command is ChatCommand.LINK:
            if parsed is None or not parsed.arguments:
                return ProcessIncomingMessageResult(type=IncomingMessageResultType.LINK_COMMAND_INVALID)
            try:
                user = self._user_identity_service.link(code=parsed.arguments, identity=identity)
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.LINKED,
                    app_user=user,
                )
            except IdentityAlreadyBoundError as error:
                try:
                    pending = self._user_identity_service.request_rebind_confirmation(
                        code=parsed.arguments, identity=identity
                    )
                    actions = tuple(
                        ConfirmationAction(
                            f"core:identity:{pending.id}:{pending.version}:{decision}",
                            label,
                            f"/confirm core identity {pending.id} {pending.version} {decision}",
                        )
                        for decision, label in (("yes", "Перенести identity"), ("no", "Отменить"))
                    )
                    return IncomingResponse(
                        "Канал уже привязан к другому профилю. Подтверди перенос адресной командой.",
                        actions=actions,
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
            return ProcessIncomingMessageResult(type=IncomingMessageResultType.UNKNOWN_IDENTITY)
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
                parsed.arguments if parsed is not None else "",
                user,
                registration=user.display_name is None,
            )
        if command is ChatCommand.STATUS:
            mode = self._chat_mode_repository.get(ChatModeKey(user.id, event.channel, event.chat_id))
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
            if is_general_entry:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            return self._save_name(text, user, registration=True)
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
        disabled = self._registry.disabled_by_command(event.text)
        if disabled is not None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.CAPABILITY_DISABLED,
                app_user=user,
                capability_reply=f"Возможность `{disabled.capability_id}` отключена.\n{self._registry.entry_hint()}",
            )
        entry = self._registry.by_command(event.text)
        if entry is not None:
            try:
                parts = event.text.strip().split(maxsplit=1)
                result = entry.enter(context, event, parts[1] if len(parts) > 1 else "")
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

    def _save_name(
        self,
        display_name: str,
        user: AppUser,
        *,
        registration: bool,
    ) -> ProcessIncomingMessageResult:
        """Сохраняет имя и завершает регистрацию без выбора возможности."""
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
            capability_reply=self._registry.entry_hint() if registration else None,
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
