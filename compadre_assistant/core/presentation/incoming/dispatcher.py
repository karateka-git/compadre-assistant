from __future__ import annotations


from dataclasses import dataclass
from enum import StrEnum
import logging

from compadre_assistant.core.presentation.message_content import compose_content, code_span, text_content
from compadre_assistant.core.presentation.incoming.commands import ChatCommand, ParsedChatCommand
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    ConfirmationAction, BackgroundReply, CapabilityHelp, CapabilityMode, ChannelCompletionHandler,
    HandlerContext, IncomingResponse, ReplyDeliveryPort,
    PendingInputContinuationPort, PendingInputOutcome,
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
    UserIdentityService,
)
from compadre_assistant.core.domain.users.entities import AppUser, IncomingIdentity
from compadre_assistant.core.domain.chat_modes import ChatModeKey, ChatModeRepository


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
        capability_preference_repository: UserCapabilityPreferenceRepository,
        pending_inputs: PendingInputStore | None = None,
    ) -> None:
        """Внедряет общий реестр и repositories независимого профиля."""
        self._pending_inputs = pending_inputs or PendingInputStore()
        self._registry = registry
        self._user_identity_service = user_identity_service
        self._chat_mode_repository = chat_mode_repository
        self._capability_preference_repository = capability_preference_repository

    def _personal_registry(self, user: AppUser) -> PersonalCapabilityRegistry:
        """Читает выбор один раз после разрешения AppUser для допуска события."""
        selections = GetUserCapabilitySelections(
            self._capability_preference_repository, self._capability_defaults()
        ).execute(user.id)
        return self._registry.for_user(selections)

    def _capability_defaults(self) -> tuple[CapabilityDefault, ...]:
        """Передаёт domain только устойчивые ID и defaults общего каталога."""
        return tuple(
            CapabilityDefault(entry.capability_id, entry.enabled_by_default)
            for entry in self._registry.descriptors()
        )

    @staticmethod
    def _blocked_reply(access: CapabilityAccess) -> str:
        """Объясняет причину отказа без открытия предметного обработчика."""
        if not access.server_available:
            return (
                f"Возможность {access.descriptor.capability_id} недоступна на сервере. "
                "Список: /capabilities."
            )
        return (
            f"Возможность {access.descriptor.capability_id} лично выключена. "
            f"Включить: /enable {access.descriptor.capability_id}. Список: /capabilities."
        )

    @staticmethod
    def _capability_catalog(personal: PersonalCapabilityRegistry) -> str:
        """Показывает известные ID, личный выбор и серверную доступность."""
        lines = ["Личные возможности:"]
        for access in personal.access:
            lines.append(
                f"{access.descriptor.capability_id} — {access.descriptor.entry.description}. "
                f"Личный выбор: {'включена' if access.user_enabled else 'выключена'}; "
                f"сервер: {'доступна' if access.server_available else 'недоступна'}."
            )
        if not personal.access:
            lines.append("Сейчас нет известных возможностей.")
        lines.append("Управление: /enable <id>, /disable <id>. Выбор действует во всех привязанных каналах.")
        return "\n".join(lines)

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
        from compadre_assistant.core.presentation.responses import format_incoming_message_result

        with self._pending_inputs.lock(event.reply_address):
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
        pending = self._pending_inputs.get(event.reply_address)
        expired = pending is not None and self._pending_inputs.expired(pending)
        if expired:
            self._pending_inputs.clear(event.reply_address, pending.request_id)
        if command is ChatCommand.CANCEL:
            if parsed.arguments:
                return IncomingResponse(compose_content(
                    "Используй ", code_span("/cancel"), " без аргументов."
                ))
            cleared = self._pending_inputs.clear(event.reply_address)
            return IncomingResponse(text_content(
                "Ввод отменён." if cleared else "Нет активного ожидания ввода."
            ))
        if text.startswith("/"):
            if command not in (ChatCommand.HELP, ChatCommand.STATUS,
                               ChatCommand.CAPABILITIES, ChatCommand.START):
                self._pending_inputs.clear(event.reply_address)
        elif expired:
            return IncomingResponse(text_content("Время ожидания истекло. Повтори команду."))
        elif pending is not None:
            return self._continue_input(pending, event, completion_handler)
        is_general_entry = (
            command is not None
            or self._registry.by_command(text) is not None
            or self._registry.disabled_by_command(text) is not None
        )
        if command is ChatCommand.HELP:
            user = self._user_identity_service.resolve(_incoming_identity_from_event(event))
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.HELP,
                capability_help=self._personal_registry(user).help() if user is not None else None,
            )
        identity = _incoming_identity_from_event(event)
        if command is ChatCommand.CAPABILITIES:
            if parsed is not None and parsed.arguments:
                return IncomingResponse(compose_content('Используй ', code_span('/capabilities'), ' без аргументов.'))
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return IncomingResponse(compose_content('Сначала зарегистрируйся: ', code_span('/register'), '.'))
            return IncomingResponse(text_content(self._capability_catalog(self._personal_registry(user))))
        if command in (ChatCommand.ENABLE, ChatCommand.DISABLE):
            return self._change_capability(command, parsed.arguments if parsed else "", identity)
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
                        compose_content('Используй адресную команду из актуального запроса привязки.')
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
                        compose_content('Действие недоступно, устарело или истекло. Повтори /link в исходном чате.'),
                        error,
                    )
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(IncomingMessageResultType.UNKNOWN_IDENTITY)
            personal = self._personal_registry(user)
            mode = self._chat_mode_repository.get(ChatModeKey(user.id, event.channel, event.chat_id))
            target = parts[0] if parts else ""
            if mode is None or mode.capability_id != target:
                capability = personal.by_id(target)
                return IncomingResponse(
                    text_content(f"Войди снова через {capability.descriptor.entry.name}, чтобы завершить действие в исходном чате."
                    if capability
                    else personal.entry_hint())
                )
            return self._dispatch(user, event, completion_handler, personal)

        if command is ChatCommand.START:
            user = self._user_identity_service.resolve(identity)
            if user is None:
                return ProcessIncomingMessageResult(type=IncomingMessageResultType.START_UNREGISTERED)
            personal = self._personal_registry(user)
            if user.display_name is None:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.START_REGISTERED,
                app_user=user,
                capability_reply=personal.entry_hint(),
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
                capability_reply=self._personal_registry(existing).entry_hint(),
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
                        compose_content('Канал уже привязан к другому профилю. Подтверди перенос адресной командой.'),
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
            personal = self._personal_registry(user)
            self._chat_mode_repository.clear(ChatModeKey(user.id, event.channel, event.chat_id))
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_EXITED,
                app_user=user,
                capability_reply=personal.entry_hint(),
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
            personal = self._personal_registry(user)
            return self._save_name(
                parsed.arguments if parsed is not None else "",
                user,
                registration=user.display_name is None,
                personal=personal,
            )
        if command is ChatCommand.STATUS:
            personal = self._personal_registry(user)
            mode = self._chat_mode_repository.get(ChatModeKey(user.id, event.channel, event.chat_id))
            access = personal.status(mode.capability_id) if mode else None
            status_hint = (
                f"\n{self._blocked_reply(access)}"
                if access is not None and not access.effective else ""
            )
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.PROFILE_STATUS,
                app_user=user,
                capability_reply=(
                    f"Профиль: {user.display_name or 'имя ещё не задано'}.\n"
                    f"Режим этого чата: {mode.capability_id if mode else 'не выбран'}.\n"
                    f"{personal.entry_hint()}{status_hint}"
                ),
            )
        if user.display_name is None:
            if is_general_entry:
                return ProcessIncomingMessageResult(
                    type=IncomingMessageResultType.REGISTRATION_NAME_REQUIRED,
                    app_user=user,
                )
            return self._save_name(
                text, user, registration=True, personal=self._personal_registry(user)
            )
        return self._dispatch(user, event, completion_handler, self._personal_registry(user))

    def _continue_input(
        self, pending: PendingInput, event: IncomingEvent,
        completion_handler: ChannelCompletionHandler | None,
    ) -> IncomingResponse:
        """Повторно проверяет допуск и передаёт исходное значение владельцу."""
        user = self._user_identity_service.resolve(_incoming_identity_from_event(event))
        personal = self._personal_registry(user) if user is not None else None
        owner = personal.by_id(pending.owner_id) if personal is not None else None
        if owner is None or not isinstance(owner, PendingInputContinuationPort):
            self._pending_inputs.clear(event.reply_address, pending.request_id)
            access = personal.status(pending.owner_id) if personal is not None else None
            return IncomingResponse(text_content(
                self._blocked_reply(access) if access is not None and not access.effective
                else "Владелец ввода недоступен. Повтори команду."
            ))
        context = HandlerContext(
            user, CallbackReplyDelivery(completion_handler) if completion_handler else None,
            self._pending_inputs,
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
        if result.outcome is PendingInputOutcome.COMPLETE:
            self._pending_inputs.clear(event.reply_address, pending.request_id)
        return result.response

    def _change_capability(
        self, command: ChatCommand, arguments: str, identity: IncomingIdentity
    ) -> IncomingResponse:
        """Проверяет общую команду и сохраняет один личный выбор AppUser.

        Args:
            command: Команда включения либо отключения.
            arguments: Строка аргументов после команды.
            identity: Разрешаемая identity исходного канала.

        Returns:
            Подтверждение собственного commit или понятный отказ без записи.
        """
        parts = arguments.split()
        if len(parts) != 1:
            return IncomingResponse(compose_content(
                'Используй ',
                code_span(str(command.usage)),
                '. Список: ',
                code_span('/capabilities'),
                '.',
            ))
        user = self._user_identity_service.resolve(identity)
        if user is None:
            return IncomingResponse(compose_content('Сначала зарегистрируйся: ', code_span('/register'), '.'))
        access = self._personal_registry(user).status(parts[0])
        if access is None:
            return IncomingResponse(
                compose_content(
                    'Неизвестная возможность. Используй ',
                    code_span(str(command.usage)),
                    '. Список: ',
                    code_span('/capabilities'),
                    '.',
                )
            )
        enabled = command is ChatCommand.ENABLE
        if enabled and not access.server_available:
            return IncomingResponse(
                compose_content(
                    'Возможность ',
                    code_span(str(parts[0])),
                    ' недоступна на сервере. Личный выбор не изменён. Список: ',
                    code_span('/capabilities'),
                    '.',
                )
            )
        try:
            changed = SetUserCapabilityPreference(
                self._capability_preference_repository, self._capability_defaults()
            ).execute(user.id, parts[0], enabled)
        except Exception as error:
            LOGGER.exception("Could not change user capability: capability_id=%s", parts[0])
            return IncomingResponse(compose_content('Не удалось сохранить выбор. Попробуй позже.'), error)
        state = "включена" if enabled else "выключена"
        return IncomingResponse(
            compose_content(
                'Возможность ',
                code_span(str(parts[0])),
                ' ' + str('уже ' if not changed else '') + str(state) + '. Выбор действует во всех привязанных каналах. Режим и данные сохранены.',
            )
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
        )
        key = ChatModeKey(user.id, event.channel, event.chat_id)
        blocked = personal.blocked_by_command(event.text)
        if blocked is not None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.CAPABILITY_DISABLED,
                app_user=user,
                capability_reply=self._blocked_reply(blocked),
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
                    compose_content(
                        'Не удалось войти в возможность. Попробуй позже или отправь ',
                        code_span('/help'),
                        '.',
                    ),
                    error,
                )
            # Одноразовый вход и ошибка никогда не заменяют прежний режим.
            if (
                result.response.error is None
                and result.mode is CapabilityMode.ACTIVE
            ):
                self._chat_mode_repository.replace_if_revision(
                    key, state.revision, entry.descriptor.capability_id
                )
            return result.response
        state = self._chat_mode_repository.get_state(key)
        if state.capability_id is None:
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_HINT,
                app_user=user,
                capability_reply=personal.entry_hint(),
            )
        capability = personal.by_id(state.capability_id)
        if capability is None:
            access = personal.status(state.capability_id)
            return ProcessIncomingMessageResult(
                type=IncomingMessageResultType.MODE_UNAVAILABLE,
                app_user=user,
                capability_reply=(
                    f"{self._blocked_reply(access)}\n{personal.entry_hint()}"
                    if access is not None else personal.entry_hint()
                ),
            )
        try:
            result = capability.handle(context, event)
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

    def _save_name(
        self,
        display_name: str,
        user: AppUser,
        *,
        registration: bool,
        personal: PersonalCapabilityRegistry,
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
            capability_reply=personal.entry_hint() if registration else None,
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
