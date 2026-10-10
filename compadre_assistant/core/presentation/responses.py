from __future__ import annotations

from compadre_assistant.core.presentation.incoming.commands import ChatCommand, CommandSection
from compadre_assistant.core.presentation.incoming.ports import CapabilityHelp
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageResultType, ProcessIncomingMessageResult


PROCESSING_ACK_TEXT = "Принял, обрабатываю. Это может занять немного времени."


def format_incoming_message_result(result: ProcessIncomingMessageResult) -> str:
    """Формирует только общие ответы регистрации и связывания каналов."""
    match result.type:
        case IncomingMessageResultType.MODE_HINT | IncomingMessageResultType.PROFILE_STATUS | IncomingMessageResultType.CAPABILITY_DISABLED:
            return result.capability_reply or "Выбери возможность через `/help`."
        case IncomingMessageResultType.MODE_EXITED:
            return f"Режим этого чата завершён.\n{result.capability_reply or 'Справка: `/help`.'}"
        case IncomingMessageResultType.MODE_UNAVAILABLE:
            return f"Сохранённая возможность этого чата отключена или недоступна. Выйти: `/exit`.\n{result.capability_reply or 'Справка: `/help`.'}"
        case IncomingMessageResultType.UNKNOWN_IDENTITY:
            return _format_unknown_identity_response()
        case IncomingMessageResultType.HELP:
            return _format_help(result.capability_help)
        case IncomingMessageResultType.START_UNREGISTERED:
            return (
                "Compadre Assistant запущен.\n"
                "Этот канал пока не зарегистрирован.\n"
                "Отправь `/register`, чтобы создать нового пользователя, или `/link <код>`, "
                "чтобы привязать этот канал к уже существующему пользователю.\n"
                "Все команды: `/help`."
            )
        case IncomingMessageResultType.START_REGISTERED:
            return f"Compadre Assistant запущен.\nРад тебя видеть, {_display_name(result)}.\n{result.capability_reply or 'Справка: `/help`.'}"
        case IncomingMessageResultType.REGISTERED:
            return (
                "Готово, я зарегистрировал тебя.\n"
                "Как к тебе обращаться? Пришли имя или название профиля."
            )
        case IncomingMessageResultType.REGISTRATION_NAME_REQUIRED:
            return (
                "Регистрация ещё не завершена.\n"
                "Как к тебе обращаться? Пришли имя или название профиля."
            )
        case IncomingMessageResultType.REGISTRATION_NAME_SAVED:
            return f"Приятно познакомиться, {_display_name(result)}.\nРегистрация завершена.\n{result.capability_reply or 'Справка: `/help`.'}"
        case IncomingMessageResultType.NAME_COMMAND_INVALID:
            return (
                "Не получилось сохранить имя. Используй от 1 до 80 символов "
                "без ссылки или команды.\n"
                "Например: `/name Влад`."
            )
        case IncomingMessageResultType.NAME_UPDATED:
            return f"Готово. Теперь буду обращаться к тебе: {_display_name(result)}."
        case IncomingMessageResultType.CONFIRMATION_MISSING:
            if result.capability_reply is not None:
                return result.capability_reply
            return (
                "Сейчас нет действия, которое нужно подтвердить.\n"
                "Сначала отправь действие, требующее подтверждения, или `/link <код>`."
            )
        case IncomingMessageResultType.ALREADY_REGISTERED:
            return f"{_display_name(result)}, ты уже зарегистрирован.\n{result.capability_reply or 'Справка: `/help`.'}"
        case IncomingMessageResultType.LINK_CODE_CREATED:
            if result.link_code is None:
                return "Не удалось создать код привязки. Попробуй позже."
            return (
                f"Код привязки: `{result.link_code.code}`\n"
                f"Открой другой канал и отправь: `/link {result.link_code.code}`\n"
                "Код действует 10 минут."
            )
        case IncomingMessageResultType.LINK_COMMAND_INVALID:
            return "Пришли команду в формате `/link <код>`."
        case IncomingMessageResultType.LINKED:
            return "Готово, я привязал этот канал к твоему пользователю."
        case IncomingMessageResultType.LINK_ALREADY_BOUND:
            return "Этот канал уже привязан к пользователю."
        case IncomingMessageResultType.LINK_CODE_INVALID:
            return "Код привязки не найден или уже истек. Создай новый через `/link_code`."
        case IncomingMessageResultType.LINK_REBOUND:
            if result.app_user is None:
                return "Готово, я перепривязал этот канал."
            return (
                "Готово, я перепривязал этот канал.\n"
                f"Теперь он связан с профилем «{_display_name(result)}»."
            )
        case IncomingMessageResultType.LINK_REBIND_CANCELLED:
            return "Ок, оставляю текущую привязку без изменений."


def _display_name(result: ProcessIncomingMessageResult) -> str:
    """Возвращает имя профиля для общего ответа."""
    if result.app_user is None or result.app_user.display_name is None:
        return "пользователь"
    return result.app_user.display_name


def _format_help(capability: CapabilityHelp | None) -> str:
    """Объединяет общие команды и разделы подключённой возможности."""
    lines = ["Compadre Assistant — справка.", "Доступные команды:"]
    for section in CommandSection:
        commands = [command for command in ChatCommand if command.section is section]
        if not commands:
            continue
        lines.extend(("", f"{section.value}:"))
        for command in commands:
            lines.append(str(command))
    if capability is not None:
        for section in capability.sections:
            lines.extend(("", f"{section.title}:", *section.commands))
        if capability.instructions:
            lines.extend(("", *capability.instructions))
    else:
        lines.extend(("", "Ответы `да` и `нет` подтверждают перепривязку канала."))
    return "\n".join(lines)


def _format_unknown_identity_response() -> str:
    """Возвращает подсказку для первого входа из нового канала."""
    return (
        "Я пока не знаю этот канал.\n"
        "Отправь `/register`, чтобы создать нового пользователя, или `/link <код>`, "
        "чтобы привязать этот канал к уже существующему пользователю."
    )
