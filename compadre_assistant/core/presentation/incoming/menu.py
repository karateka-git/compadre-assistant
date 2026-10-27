"""Проекция публичного каталога в меню без зависимости от канала доставки."""

from compadre_assistant.core.presentation.incoming.commands import ChatCommand
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CommandMenu, CommandSuggestion, ConfirmationQueryResult,
)
from compadre_assistant.core.presentation.incoming.registry import PersonalCapabilityRegistry


def build_command_menu(
    personal: PersonalCapabilityRegistry | None,
    mode_id: str | None,
    pending: PendingInput | None,
    snapshot: ConfirmationQueryResult,
) -> CommandMenu:
    """Собирает выбираемые команды по проверенному состоянию текущего чата.

    Args:
        personal: Снимок эффективного доступа либо None до регистрации.
        mode_id: Сохранённый режим данного чата.
        pending: Уже проверенное по сроку, профилю и доступу ожидание ввода.
        snapshot: Снимок всех применимых источников подтверждений.

    Returns:
        Меню с точными командами, запускающими действие или пошаговый ввод.
        Команды с обязательными параметрами без пути ввода не включаются:
        каталог не содержит конкретных значений для их выполнения.
    """
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
        common(ChatCommand.LINK)
    else:
        active = personal.by_id(mode_id) if mode_id else None
        if active is not None and active.descriptor.persistent_mode:
            for section in active.descriptor.help.sections:
                for command in section.commands:
                    capability(command)
            common(ChatCommand.EXIT)
            ask = personal.by_id("ask")
            if ask is not None and not ask.descriptor.persistent_mode:
                capability(ask.descriptor.entry)
        else:
            if mode_id:
                common(ChatCommand.EXIT)
            else:
                for command in ChatCommand:
                    if command not in (ChatCommand.CANCEL, ChatCommand.CONFIRM):
                        common(command)
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
