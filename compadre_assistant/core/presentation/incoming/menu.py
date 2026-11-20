"""Проекция публичного каталога в меню без зависимости от канала доставки."""

from compadre_assistant.core.presentation.incoming.commands import ChatCommand
from compadre_assistant.core.presentation.incoming.action_contracts import ActionScope
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CommandMenu, CommandSuggestion,
    ConfirmationQueryResult, ReplyAction,
)
from compadre_assistant.core.presentation.incoming.registry import PersonalCapabilityRegistry


HELP_SECTION_ACTION = ReplyAction("Помощь", ChatCommand.HELP.value, scope=ActionScope.SECTION)
HOME_SECTION_ACTION = ReplyAction("На главную", ChatCommand.EXIT.value, scope=ActionScope.SECTION)


# Пока принятое действие занимает чат, только отмена может изменить его состояние.
BUSY_COMMAND_MENU = CommandMenu(
    (CommandSuggestion(ChatCommand.CANCEL.value, ChatCommand.CANCEL.description),),
    section_actions=(CANCEL_SECTION_ACTION,),
)


def capability_entry_action(descriptor: CapabilityDescriptor) -> ReplyAction:
    """Создаёт кнопку входа с публичным названием возможности."""
    return ReplyAction(descriptor.display_name or descriptor.entry.description,
                       descriptor.entry.name, scope=ActionScope.SECTION)


def main_actions(
    personal: PersonalCapabilityRegistry | None, pending: PendingInput | None, *, allow_link: bool = True,
) -> tuple[ReplyAction, ...]:
    """Возвращает действия первого входа, ожидания или главного меню по допуску профиля.

    Args:
        personal: Проверенный доступ профиля либо None до регистрации.
        pending: Применимое ожидание этого пользователя.
        allow_link: Предлагать ввод кода в личном чате.

    Returns:
        Действия общего экрана, включая контекстную справку.
    """
    if pending is not None:
        return CANCEL_SECTION_ACTION, HELP_SECTION_ACTION
    if personal is None:
        create = ReplyAction("Создать профиль", "/register", scope=ActionScope.SECTION)
        link = (ReplyAction("У меня есть код", "/link", scope=ActionScope.SECTION),) if allow_link else ()
        return create, *link, HELP_SECTION_ACTION
    entries = (access.descriptor for access in personal.access if access.effective)
    return (*(
        capability_entry_action(descriptor) for descriptor in entries
        if not any(parameter.required for parameter in descriptor.entry.parameters)
    ), HELP_SECTION_ACTION)


def build_command_menu(
    personal: PersonalCapabilityRegistry | None,
    mode_id: str | None,
    pending: PendingInput | None,
    snapshot: ConfirmationQueryResult,
    *,
    active_help: CapabilityHelp | None = None,
    allow_link: bool = True,
) -> CommandMenu:
    """Собирает выбираемые команды по проверенному состоянию текущего чата.

    Args:
        personal: Снимок эффективного доступа либо None до регистрации.
        mode_id: Сохранённый режим данного чата.
        pending: Уже проверенное по сроку, профилю и доступу ожидание ввода.
        snapshot: Снимок всех применимых источников подтверждений.
        active_help: Отобранная справка активной возможности либо None для её каталога.
        allow_link: Предлагать привязку до регистрации в проверенном личном чате.

    Returns:
        Меню с точными командами, запускающими действие или пошаговый ввод.
        Команды с обязательными параметрами без пути ввода не включаются:
        каталог не содержит конкретных значений для их выполнения.
    """
    if pending is not None and pending.cancel_only_menu:
        return CommandMenu(BUSY_COMMAND_MENU.items, pending.value_label)

    items: list[CommandSuggestion] = []

    def common(command: ChatCommand) -> None:
        items.append(CommandSuggestion(command.value, command.description))

    def capability(command: CapabilityCommand) -> None:
        if not any(parameter.required for parameter in command.parameters):
            items.append(CommandSuggestion(command.name, command.description))

    registration = pending is not None and pending.owner_id == "core" and pending.input_kind == "register_name"
    if registration:
        pass  # Ввод имени заменяет базовое меню единственной отменой ниже.
    elif personal is None:
        common(ChatCommand.REGISTER)
        if allow_link:
            common(ChatCommand.LINK)
    else:
        active = personal.by_id(mode_id) if mode_id else None
        if active is not None and active.descriptor.persistent_mode:
            catalog = active.descriptor.help if active_help is None else active_help
            for section in catalog.sections:
                if not section.menu_help_only:
                    for command in section.commands:
                        capability(command)
            common(ChatCommand.HELP)
            common(ChatCommand.EXIT)
            for access in personal.access:
                if access.effective and not access.descriptor.persistent_mode:
                    capability(access.descriptor.entry)
        else:
            if mode_id:
                common(ChatCommand.EXIT)
            for access in personal.access:
                if access.effective and access.descriptor.capability_id != mode_id:
                    capability(access.descriptor.entry)
    if pending is not None:
        common(ChatCommand.CANCEL)
    if (personal is not None and not registration and snapshot.status == "complete"
            and any(item.actions for item in snapshot.confirmations)):
        items.append(CommandSuggestion("/confirm", "показать актуальные действия без выбора решения"))
    label = pending.value_label if pending else None
    if pending is not None and pending.owner_id == "core":
        label = {"register_name": "имя или название профиля", "link_code": "код привязки",
                 "profile_name": "имя", "enable": "ID возможности для включения",
                 "disable": "ID возможности для выключения"}.get(pending.input_kind, label)
    return CommandMenu(tuple(items), label)
