"""Представляет настройки через публичные операции ядра."""

from compadre_assistant.core.presentation.incoming.commands import (
    ChatCommand, ParsedChatCommand, PROFILE_COMMANDS,
)
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult,
    CommandParameter, ConfirmationQueryResult, HandlerContext, HelpSection, IncomingCapabilityPort,
    IncomingResponse, PendingConfirmationQueryPort,
)
from compadre_assistant.core.presentation.message_content import code_span, compose_content


def _command(command: ChatCommand) -> CapabilityCommand:
    parameters = (
        (CommandParameter(command.arguments_hint[1:-1], command.arguments_hint, required=False),)
        if command.arguments_hint else ()
    )
    return CapabilityCommand(command.value, parameters, command.description)


class SettingsCapability(IncomingCapabilityPort, PendingConfirmationQueryPort):
    """Открывает настройки профиля, связей каналов и личного выбора."""

    descriptor = CapabilityDescriptor(
        "settings", CapabilityCommand("/settings", (), "открыть настройки"),
        help=CapabilityHelp(sections=(
            HelpSection("Профиль", tuple(map(_command, (ChatCommand.STATUS, ChatCommand.NAME)))),
            HelpSection("Связь Telegram и VK", tuple(map(_command, (ChatCommand.LINK_CODE, ChatCommand.LINK)))),
            HelpSection("Личные возможности", tuple(map(_command, (
                ChatCommand.CAPABILITIES, ChatCommand.ENABLE, ChatCommand.DISABLE,
            )))),
        )),
        enabled_by_default=True,
        display_name="Настройки",
        personally_configurable=False,
        profile_commands=PROFILE_COMMANDS,
    )

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
        return CapabilityResult(IncomingResponse(compose_content(
            "Настройки открыты. Профиль: ", name, ".\n"
            "Изменить имя: ", code_span("/name"), ".\n"
            "Связать Telegram и VK: ", code_span("/link_code"), " или ", code_span("/link"), ".\n"
            "Управлять возможностями: ", code_span("/capabilities"), ".\n"
            "Все команды: ", code_span("/help"), ". На главную: ", code_span("/exit"), ".",
        )), CapabilityMode.ACTIVE)

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
            return CapabilityResult(context.profile_operation(parsed.command, parsed.arguments), CapabilityMode.ACTIVE)
        return CapabilityResult(IncomingResponse(compose_content(
            "Выбери команду настроек через ", code_span("/help"), ".",
        )), CapabilityMode.ACTIVE)

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Сообщает, что настройки не создают предметных подтверждений."""
        return ConfirmationQueryResult("complete")
