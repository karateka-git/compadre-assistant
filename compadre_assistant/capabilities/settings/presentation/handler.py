"""Представляет настройки через публичные операции ядра."""

from dataclasses import replace

from compadre_assistant.core.domain.users.entities import AppUser, is_private_chat_address
from compadre_assistant.core.presentation.incoming.action_contracts import ActionScope, SectionMenuDelivery
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

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Возвращает действия настроек личного чата либо отмену текущего ввода."""
        if pending:
            return (CANCEL_SECTION_ACTION,)
        return tuple(ReplyAction(label, command, scope=ActionScope.SECTION) for label, command in (
            ("Профиль", "/status"), ("Изменить имя", "/name"), ("Личные возможности", "/capabilities"),
            ("Получить код", "/link_code"), ("Ввести код", "/link"), ("Помощь", "/help"), ("На главную", "/exit"),
        ))

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
        return self._screen(context, event, IncomingResponse(compose_content(
            "Настройки открыты. Профиль: ", name, ".\n"
            "Изменить имя: ", code_span("/name"), ".\n"
            "Связать Telegram и VK: ", code_span("/link_code"), " или ", code_span("/link"), ".\n"
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
            return self._screen(context, event, context.profile_operation(parsed.command, parsed.arguments))
        return self._screen(context, event, IncomingResponse(compose_content(
            "Выбери команду настроек через ", code_span("/help"), ".",
        )))

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Сообщает, что настройки не создают предметных подтверждений."""
        return ConfirmationQueryResult("complete")
