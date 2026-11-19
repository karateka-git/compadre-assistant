"""Представляет настройки через публичные операции ядра."""

from collections.abc import Callable, Mapping
from dataclasses import replace

from compadre_assistant.core.domain.users.entities import AppUser, is_private_chat_address
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.commands import (
    ChatCommand, ParsedChatCommand, PROFILE_COMMANDS,
)
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult,
    CommandParameter, ConfirmationQueryResult, HandlerContext, HelpSection, IncomingCapabilityPort,
    CANCEL_SECTION_ACTION, IncomingResponse, PendingConfirmationQueryPort, ReplyAction, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.message_content import code_span, compose_content


_MESSENGER_NAMES = {"telegram": "Telegram", "vk": "VK"}


def _command(command: ChatCommand) -> CapabilityCommand:
    parameters = (
        (CommandParameter(command.arguments_hint[1:-1], command.arguments_hint, required=False),)
        if command.arguments_hint else ()
    )
    return CapabilityCommand(command.value, parameters, command.description)


class SettingsCapability(IncomingCapabilityPort, PendingConfirmationQueryPort, SectionActionsQueryPort):
    """Открывает настройки профиля, связей каналов и личного выбора."""

    descriptor = CapabilityDescriptor(
        "settings", CapabilityCommand("/settings", (), "открыть настройки"),
        help=CapabilityHelp(sections=(
            HelpSection("Профиль", tuple(map(_command, (ChatCommand.STATUS, ChatCommand.NAME)))),
            HelpSection("Мессенджеры", tuple(map(_command, (ChatCommand.CONNECT, ChatCommand.LINK)))),
            HelpSection("Личные возможности", tuple(map(_command, (
                ChatCommand.CAPABILITIES, ChatCommand.ENABLE, ChatCommand.DISABLE,
            )))),
        )),
        enabled_by_default=True,
        display_name="Настройки",
        personally_configurable=False,
        profile_commands=PROFILE_COMMANDS,
    )

    def __init__(self, dialogs: Mapping[str, str | None] | None = None,
                 linked_channels: Callable[[int], frozenset[str]] | None = None) -> None:
        """Принимает публичные адреса установки и чтение связей зарегистрированного профиля."""
        self._dialogs = dict(dialogs) if dialogs is not None else dict.fromkeys(_MESSENGER_NAMES)
        self._linked_channels = linked_channels
        self._messenger_sections: dict[ReplyAddress, tuple[int, int]] = {}

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Возвращает действия настроек личного чата либо отмену текущего ввода."""
        if pending:
            return (CANCEL_SECTION_ACTION,)
        if self._messenger_sections.get(address) == (app_user.id, mode_revision):
            linked = self._channels(app_user, address)
            return self._messenger_actions(address.channel, linked)
        return tuple(ReplyAction(label, command, scope=ActionScope.SECTION) for label, command in (
            ("Профиль", "/status"), ("Изменить имя", "/name"), ("Личные возможности", "/capabilities"),
            ("Мессенджеры", "/connect"), ("Помощь", "/help"), ("На главную", "/exit"),
        ))

    def _channels(self, user: AppUser, address: ReplyAddress) -> frozenset[str]:
        if self._linked_channels is None:
            raise RuntimeError("Не подключено чтение связей профиля")
        return self._linked_channels(user.id) | {address.channel}

    def _messenger_actions(self, current: str, linked: frozenset[str]) -> tuple[ReplyAction, ...]:
        actions = []
        for channel, url in self._dialogs.items():
            if channel == current:
                continue
            if channel not in linked:
                actions.append(ReplyAction(f"Привязать {_MESSENGER_NAMES[channel]}", f"/connect {channel}",
                                           scope=ActionScope.SECTION))
            elif url:
                actions.append(ReplyAction("Открыть бота", url=url, scope=ActionScope.SECTION))
        if current in self._dialogs:
            actions.append(ReplyAction(f"Привязать {_MESSENGER_NAMES[current]} к другому профилю",
                                       "/link", scope=ActionScope.SECTION))
        actions.append(ReplyAction("Назад", "/settings", scope=ActionScope.SECTION, purpose=ActionPurpose.NAVIGATION,
                                   destination=ScreenDestination("settings")))
        return tuple(actions)

    def _connect(self, context: HandlerContext, event: IncomingEvent, target: str) -> IncomingResponse:
        if not is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            return IncomingResponse(compose_content("Для привязки профиля открой личный чат с ботом."))
        linked = self._channels(context.app_user, event.reply_address)
        self._messenger_sections[event.reply_address] = (context.app_user.id, context.mode_revision or 0)
        actions = self._messenger_actions(event.channel, linked)
        if target and target not in self._dialogs:
            return IncomingResponse(compose_content("Этот мессенджер недоступен. Выбери доступный через ",
                                                    code_span("/connect"), "."), suggested_actions=actions)
        if not target:
            parts = ["Мессенджеры профиля:"]
            for channel, url in self._dialogs.items():
                name = _MESSENGER_NAMES[channel]
                parts.append(f"\n{name} — " + ("привязан." if channel in linked else "не привязан."))
                if channel != event.channel:
                    if channel not in linked:
                        parts.extend((" Для привязки: ", code_span(f"/connect {channel}"), "."))
                    elif url:
                        parts.extend((" Открыть бота: ", url))
                    if not url:
                        parts.append(" Адрес бота не указан.")
            parts.extend(("\nЧтобы перенести этот аккаунт, используй ", code_span("/link <код>"),
                          ". Вернуться: ", code_span("/settings"), "."))
            return IncomingResponse(compose_content(*parts), suggested_actions=actions)
        url = self._dialogs[target]
        if target in linked:
            return IncomingResponse(compose_content(
                f"{_MESSENGER_NAMES[target]} уже привязан. ", f"Открыть бота: {url}" if url else "Адрес бота не указан.",
            ), suggested_actions=actions)
        response = context.profile_operation(ChatCommand.CONNECT, target)
        return replace(response, content=compose_content(
            response.content, f"\nДиалог {_MESSENGER_NAMES[target]}: {url}" if url
            else f"\nАдрес бота не указан. Открой знакомый диалог в {_MESSENGER_NAMES[target]}.",
        ), suggested_actions=(ReplyAction("Открыть бота", url=url), *actions) if url else actions)

    def _screen(self, context: HandlerContext, event: IncomingEvent, response: IncomingResponse) -> CapabilityResult:
        if is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            pending = context.pending_inputs.get(event.reply_address) if context.pending_inputs is not None else None
            actions = self.section_actions(context.app_user, event.reply_address, context.mode_revision or 0, pending)
            screen = tuple(action for action in response.suggested_actions if action.scope == ActionScope.SCREEN)
            response = replace(response, suggested_actions=(*screen, *actions),
                               command_menu_delivery=SectionMenuDelivery.RECONCILE)
        return CapabilityResult(response, CapabilityMode.ACTIVE)

    def enter(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> CapabilityResult:
        """Открывает настройки и показывает текущий профиль и команды.

        Args:
            context: Контекст с зарегистрированным профилем.
            event: Входное событие чата.
            arguments: Аргументы после `/settings`.

        Returns:
            Подсказку и активный режим либо отказ при наличии аргументов.
        """
        if arguments:
            return CapabilityResult(IncomingResponse(compose_content(
                "Используй ", code_span("/settings"), " без аргументов.",
            )), CapabilityMode.COMPLETE)
        name = context.app_user.display_name or "имя ещё не задано"
        self._messenger_sections.pop(event.reply_address, None)
        connection = ("Мессенджеры: ", code_span("/connect"), ".\n") if is_private_chat_address(
            event.channel, event.external_user_id, event.chat_id,
        ) else ()
        return self._screen(context, event, IncomingResponse(compose_content(
            "Настройки открыты. Профиль: ", name, ".\n"
            "Изменить имя: ", code_span("/name"), ".\n",
            *connection,
            "Управлять возможностями: ", code_span("/capabilities"), ".\n"
            "Все команды: ", code_span("/help"), ". На главную: ", code_span("/exit"), ".",
        )))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Передаёт распознанную команду настроек публичной операции ядра.

        Args:
            context: Профиль и операции ядра для допущенного режима.
            event: Текст или команда активного чата.

        Returns:
            Ответ операции либо подсказку с сохранением настроек активными.
        """
        parsed = ParsedChatCommand.parse(event.text.strip())
        if parsed is not None and parsed.command in PROFILE_COMMANDS and context.profile_operation is not None:
            if parsed.command is ChatCommand.CONNECT:
                response = self._connect(context, event, parsed.arguments.strip().lower())
                return CapabilityResult(replace(response, command_menu_delivery=SectionMenuDelivery.RECONCILE),
                                        CapabilityMode.ACTIVE)
            if parsed.command is not ChatCommand.LINK:
                self._messenger_sections.pop(event.reply_address, None)
            return self._screen(context, event, context.profile_operation(parsed.command, parsed.arguments))
        return self._screen(context, event, IncomingResponse(compose_content(
            "Выбери команду настроек через ", code_span("/help"), ".",
        )))

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Сообщает, что настройки не создают предметных подтверждений."""
        return ConfirmationQueryResult("complete")
