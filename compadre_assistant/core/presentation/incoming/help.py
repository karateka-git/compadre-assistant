"""Контекстная справка из публичного каталога и проверенных снимков чата."""

from compadre_assistant.core.presentation.incoming.commands import ChatCommand, CommandSection
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, ConfirmationQueryResult, IncomingResponse,
)
from compadre_assistant.core.presentation.incoming.registry import PersonalCapabilityRegistry
from compadre_assistant.core.presentation.message_content import compose_content, code_span, text_content


def format_contextual_help(
    personal: PersonalCapabilityRegistry | None,
    mode_id: str | None,
    pending: PendingInput | None,
    snapshot: ConfirmationQueryResult,
    confirmation: IncomingResponse,
) -> IncomingResponse:
    """Формирует справку только по допущенным командам и актуальным ожиданиям.

    Args:
        personal: Эффективный доступ профиля либо None до регистрации.
        mode_id: Сохранённый режим конкретного чата.
        pending: Проверенное ожидание ввода, срок которого не продлевается.
        snapshot: Результат опроса применимых источников подтверждений.
        confirmation: Готовое представление адресных действий этого снимка.

    Returns:
        Справку с отдельным блоком точных адресных действий.
    """
    parts = [text_content("Compadre Assistant — справка.")]

    def line(text: str) -> None:
        parts.append(text_content("\n" + text))

    def command(name: str, description: str) -> None:
        parts.append(compose_content("\n", code_span(name), " — " + description))

    def entry(item: CapabilityCommand, *, one_shot: bool = False) -> None:
        syntax = " ".join((item.name, *(parameter.syntax for parameter in item.parameters)))
        command(syntax, item.description + ("; одноразовый вопрос без смены режима" if one_shot else ""))

    registration = pending is not None and pending.owner_id == "core" and pending.input_kind == "register_name"
    if registration:
        line("Регистрация не завершена. Как к тебе обращаться? Пришли имя или название профиля.")
        command("/cancel", "отменить ввод имени")
    elif personal is None:
        line("Доступные действия:")
        command("/register", "создать профиль")
        command("/link", "ввести код привязки к существующему профилю")
        parts.append(compose_content("\nЕсли код уже есть, можно сразу отправить ", code_span("/link <code>"), "."))
    else:
        active = personal.by_id(mode_id) if mode_id else None
        if active is not None and active.descriptor.persistent_mode:
            line(f"Команды режима {mode_id}:")
            for section in active.descriptor.help.sections:
                line(section.title + ":")
                for item in section.commands:
                    entry(item)
            for instruction in active.descriptor.help.instructions:
                line(instruction)
            command("/exit", "выйти из режима этого чата")
            parts.append(compose_content(
                "\nОбщие команды: ", code_span("/status"), ", ", code_span("/capabilities"),
                ", ", code_span("/link_code"), ". Полная общая справка: ", code_span("/exit"),
                ", затем ", code_span("/help"), ".",
            ))
            # Ask объявлен публичным ID и непостоянным входом; его сценарий ядро не разбирает.
            ask = personal.by_id("ask")
            if ask is not None and not ask.descriptor.persistent_mode:
                entry(ask.descriptor.entry, one_shot=True)
        else:
            if mode_id:
                access = personal.status(mode_id)
                if access is not None and access.effective and not access.descriptor.persistent_mode:
                    line(f"Сохранённый режим {mode_id} устарел: эта возможность выполняет одноразовый запрос и не поддерживает постоянный режим. Выйти: /exit.")
                else:
                    reason = "недоступна на сервере" if access is None or not access.server_available else "лично выключена"
                    line(f"Режим {mode_id} недоступен: возможность {reason}. Выйти: /exit.")
            for section in CommandSection:
                line(section.value + ":")
                for item in ChatCommand:
                    if item.section is section and item not in (ChatCommand.CANCEL, ChatCommand.CONFIRM):
                        command(item.usage, item.description)
            available = [item for item in personal.access if item.effective]
            line("Возможности:" if available else "Сейчас нет доступных возможностей.")
            for item in available:
                entry(item.descriptor.entry, one_shot=not item.descriptor.persistent_mode)
    if pending is not None and not registration:
        label = pending.value_label
        if pending.owner_id == "core":
            label = {"link_code": "код привязки", "profile_name": "имя",
                     "enable": "ID возможности для включения", "disable": "ID возможности для выключения"}.get(pending.input_kind, label)
        line(f"Ожидается {label}. Отправь значение следующим сообщением.")
        command("/cancel", "отменить ожидающий ввод")
    if snapshot.confirmations or snapshot.status != "complete":
        parts.append(compose_content("\n\n", confirmation.content))
        if personal is not None and snapshot.status == "complete" and confirmation.actions:
            command("/confirm", "показать актуальные действия без выбора решения")
    return IncomingResponse(compose_content(*parts), actions=confirmation.actions)
