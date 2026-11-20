"""Представляет настройки через публичные операции ядра."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from enum import Enum

from compadre_assistant.core.domain.users.entities import AppUser, is_private_chat_address
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.commands import (
    ChatCommand, ParsedChatCommand, PROFILE_COMMANDS,
)
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION, HOME_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode,
    CapabilityResult, CommandParameter, ConfirmationQueryResult, ContextualCommandQueryPort,
    HandlerContext, HelpSection, IncomingCapabilityPort, IncomingResponse, PendingConfirmationQueryPort,
    ReplyAction, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.message_content import compose_content, text_content


_MESSENGER_NAMES = {"telegram": "Telegram", "vk": "VK"}
_BACK_TO_SETTINGS = ReplyAction("Назад", "/settings", scope=ActionScope.SECTION, purpose=ActionPurpose.NAVIGATION,
                               destination=ScreenDestination("settings"))
_PROFILE_CATALOG = ReplyAction("Личные возможности", "/capabilities", scope=ActionScope.SECTION)
_PROFILE_ACTIONS = (
    ReplyAction("Профиль", "/status", scope=ActionScope.SECTION),
    ReplyAction("Изменить имя", "/name", scope=ActionScope.SECTION),
    _PROFILE_CATALOG,
    ReplyAction("Мессенджеры", "/connect", scope=ActionScope.SECTION),
    HELP_SECTION_ACTION,
    HOME_SECTION_ACTION,
)


class _SettingsScreen(Enum):
    """Определяет раздел настроек для справки и действий нижнего меню."""

    PROFILE = "profile"  # Начальный экран и сведения профиля.
    MESSENGERS = "messengers"  # Связи, получение и применение кода.
    CAPABILITIES = "capabilities"  # Список возможностей и личные настройки.


def _command(command: ChatCommand) -> CapabilityCommand:
    parameters = (
        (CommandParameter(command.arguments_hint[1:-1], command.arguments_hint, required=False),)
        if command.arguments_hint else ()
    )
    return CapabilityCommand(command.value, parameters, command.description)


class SettingsCapability(IncomingCapabilityPort, PendingConfirmationQueryPort, SectionActionsQueryPort,
                         ContextualCommandQueryPort):
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
        self._screens: dict[tuple[int, ReplyAddress], tuple[int, _SettingsScreen]] = {}

    def _current_screen(self, user: AppUser, address: ReplyAddress, revision: int) -> _SettingsScreen:
        saved = self._screens.get((user.id, address))
        return saved[1] if saved is not None and saved[0] == revision else _SettingsScreen.PROFILE

    def visible_mode_commands(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
    ) -> frozenset[str]:
        """Возвращает команды открытого экрана настроек для контекстной справки.

        Args:
            app_user: Владелец настроек.
            address: Адрес текущего чата в мессенджере.
            mode_revision: Ревизия активного режима этого чата.

        Returns:
            Имена команд текущего раздела или начального экрана настроек.
        """
        screen = self._current_screen(app_user, address, mode_revision)
        if screen is _SettingsScreen.MESSENGERS:
            return frozenset((ChatCommand.CONNECT.value, ChatCommand.LINK.value, self.descriptor.entry.name))
        if screen is _SettingsScreen.CAPABILITIES:
            return frozenset((ChatCommand.CAPABILITIES.value, ChatCommand.ENABLE.value, ChatCommand.DISABLE.value,
                              self.descriptor.entry.name))
        return frozenset((ChatCommand.STATUS.value, ChatCommand.NAME.value,
                          ChatCommand.CAPABILITIES.value, ChatCommand.CONNECT.value))

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Возвращает действия раздела настроек или отмену текущего ввода."""
        if pending:
            return CANCEL_SECTION_ACTION, HELP_SECTION_ACTION
        screen = self._current_screen(app_user, address, mode_revision)
        if screen is _SettingsScreen.MESSENGERS:
            linked = self._channels(app_user, address)
            return self._messenger_actions(address.channel, linked)
        if screen is _SettingsScreen.CAPABILITIES:
            return _PROFILE_CATALOG, _BACK_TO_SETTINGS, HELP_SECTION_ACTION
        return _PROFILE_ACTIONS

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
        return (*actions, _BACK_TO_SETTINGS, HELP_SECTION_ACTION)

    def _connect(self, context: HandlerContext, event: IncomingEvent, target: str) -> IncomingResponse:
        if not is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            return IncomingResponse(compose_content("Для привязки профиля открой личный чат с ботом."))
        linked = self._channels(context.app_user, event.reply_address)
        actions = self._messenger_actions(event.channel, linked)
        if target and target not in self._dialogs:
            return IncomingResponse(text_content("Этот мессенджер недоступен. Выбери другой из списка."),
                                    suggested_actions=actions)
        if not target:
            parts = ["Мессенджеры профиля:"]
            for channel, url in self._dialogs.items():
                name = _MESSENGER_NAMES[channel]
                parts.append(f"\n{name} — " + ("привязан." if channel in linked else "не привязан."))
                if channel != event.channel:
                    if channel in linked and url:
                        parts.extend((" Открыть бота: ", url))
                    if not url:
                        parts.append(" Адрес бота не указан.")
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
        pending = context.pending_inputs.get(event.reply_address) if context.pending_inputs is not None else None
        if pending is not None and pending.external_user_id not in (None, event.external_user_id):
            pending = None
        actions = self.section_actions(context.app_user, event.reply_address, context.mode_revision or 0, pending)
        if not is_private_chat_address(event.channel, event.external_user_id, event.chat_id):
            actions = tuple(action for action in actions if action.command != ChatCommand.CONNECT.value)
        screen = tuple(action for action in response.suggested_actions if action.scope == ActionScope.SCREEN)
        screen_commands = {action.command for action in screen}
        section = tuple(action for action in actions if action.command not in screen_commands)
        response = replace(response, suggested_actions=(*screen, *section),
                           command_menu_delivery=SectionMenuDelivery.RECONCILE)
        return CapabilityResult(response, CapabilityMode.ACTIVE)

    def enter(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> CapabilityResult:
        """Открывает настройки и показывает текущий профиль.

        Args:
            context: Контекст с зарегистрированным профилем.
            event: Входное событие чата.
            arguments: Аргументы после `/settings`.

        Returns:
            Подсказку и активный режим либо отказ при наличии аргументов.
        """
        if arguments:
            return CapabilityResult(IncomingResponse(compose_content(
                "Открой настройки отдельным действием.",
            ), suggested_actions=(ReplyAction("Настройки", "/settings"),)), CapabilityMode.COMPLETE)
        name = context.app_user.display_name or "имя ещё не задано"
        self._screens.pop((context.app_user.id, event.reply_address), None)
        content = compose_content("Настройки открыты. Профиль: ", name, ".")
        return self._screen(context, event, IncomingResponse(content))

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
            if parsed.command in (ChatCommand.CONNECT, ChatCommand.LINK):
                screen = (_SettingsScreen.MESSENGERS
                          if is_private_chat_address(event.channel, event.external_user_id, event.chat_id)
                          else _SettingsScreen.PROFILE)
            elif parsed.command in (ChatCommand.CAPABILITIES, ChatCommand.ENABLE, ChatCommand.DISABLE):
                screen = _SettingsScreen.CAPABILITIES
            else:
                screen = _SettingsScreen.PROFILE
            self._screens[(context.app_user.id, event.reply_address)] = (context.mode_revision or 0, screen)
            if parsed.command is ChatCommand.CONNECT:
                response = self._connect(context, event, parsed.arguments.strip().lower())
                return CapabilityResult(replace(response, command_menu_delivery=SectionMenuDelivery.RECONCILE),
                                        CapabilityMode.ACTIVE)
            return self._screen(context, event, context.profile_operation(parsed.command, parsed.arguments))
        return self._screen(context, event, IncomingResponse(text_content("Выбери действие в настройках.")))

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Сообщает, что настройки не создают предметных подтверждений."""
        return ConfirmationQueryResult("complete")
