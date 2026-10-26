from __future__ import annotations

from compadre_assistant.core.presentation.message_content import MessageContent, compose_content, text_content, code_span

from compadre_assistant.core.presentation.incoming.commands import ChatCommand, CommandSection
from compadre_assistant.core.presentation.incoming.ports import CapabilityHelp
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageResultType, ProcessIncomingMessageResult


PROCESSING_ACK_TEXT = "Принял, обрабатываю. Это может занять немного времени."


def format_incoming_message_result(result: ProcessIncomingMessageResult) -> MessageContent:
    """Формирует только общие ответы регистрации и связывания каналов."""
    match result.type:
        case IncomingMessageResultType.MODE_HINT | IncomingMessageResultType.PROFILE_STATUS | IncomingMessageResultType.CAPABILITY_DISABLED:
            return text_content(result.capability_reply or "Выбери возможность через /help.")
        case IncomingMessageResultType.MODE_EXITED:
            return compose_content('Режим этого чата завершён.\n' + str(result.capability_reply or 'Справка: /help.'))
        case IncomingMessageResultType.MODE_UNAVAILABLE:
            return compose_content(
                'Сохранённая возможность этого чата отключена или недоступна. Выйти: ',
                code_span('/exit'),
                '.\n' + str(result.capability_reply or 'Справка: /help.'),
            )
        case IncomingMessageResultType.UNKNOWN_IDENTITY:
            return _format_unknown_identity_response()
        case IncomingMessageResultType.HELP:
            return _format_help(result.capability_help)
        case IncomingMessageResultType.START_UNREGISTERED:
            return (
                compose_content(
                    'Compadre Assistant запущен.\nЭтот канал пока не зарегистрирован.\nОтправь ',
                    code_span('/register'),
                    ', чтобы создать нового пользователя, или ',
                    code_span('/link <код>'),
                    ', чтобы привязать этот канал к уже существующему пользователю.\nВсе команды: ',
                    code_span('/help'),
                    '.',
                )
            )
        case IncomingMessageResultType.START_REGISTERED:
            return compose_content(
                'Compadre Assistant запущен.\nРад тебя видеть, ' + str(_display_name(result)) + '.\n' + str(result.capability_reply or 'Справка: /help.'),
            )
        case IncomingMessageResultType.REGISTERED:
            return (
                compose_content(
                    'Готово, я зарегистрировал тебя.\nКак к тебе обращаться? Пришли имя или название профиля.',
                )
            )
        case IncomingMessageResultType.REGISTRATION_NAME_REQUIRED:
            return (
                compose_content(
                    'Регистрация ещё не завершена.\nКак к тебе обращаться? Пришли имя или название профиля.',
                )
            )
        case IncomingMessageResultType.REGISTRATION_NAME_SAVED:
            return compose_content(
                'Приятно познакомиться, ' + str(_display_name(result)) + '.\nРегистрация завершена.\n' + str(result.capability_reply or 'Справка: /help.'),
            )
        case IncomingMessageResultType.NAME_COMMAND_INVALID:
            return (
                compose_content(
                    'Не получилось сохранить имя. Используй от 1 до 80 символов без ссылки или команды.\nНапример: ',
                    code_span('/name Влад'),
                    '.',
                )
            )
        case IncomingMessageResultType.NAME_UPDATED:
            return compose_content('Готово. Теперь буду обращаться к тебе: ' + str(_display_name(result)) + '.')
        case IncomingMessageResultType.CONFIRMATION_MISSING:
            if result.capability_reply is not None:
                return text_content(result.capability_reply)
            return (
                compose_content(
                    'Сейчас нет действия, которое нужно подтвердить.\nСначала отправь действие, требующее подтверждения, или ',
                    code_span('/link <код>'),
                    '.',
                )
            )
        case IncomingMessageResultType.ALREADY_REGISTERED:
            return compose_content(
                str(_display_name(result)) + ', ты уже зарегистрирован.\n' + str(result.capability_reply or 'Справка: /help.'),
            )
        case IncomingMessageResultType.LINK_CODE_CREATED:
            if result.link_code is None:
                return compose_content('Не удалось создать код привязки. Попробуй позже.')
            return (
                compose_content(
                    'Код привязки: ',
                    code_span(str(result.link_code.code)),
                    '\nОткрой другой канал и отправь: ',
                    code_span('/link ' + str(result.link_code.code)),
                    '\nКод действует 10 минут.',
                )
            )
        case IncomingMessageResultType.LINK_COMMAND_INVALID:
            return compose_content('Пришли команду в формате ', code_span('/link <код>'), '.')
        case IncomingMessageResultType.LINKED:
            return compose_content('Готово, я привязал этот канал к твоему пользователю.')
        case IncomingMessageResultType.LINK_ALREADY_BOUND:
            return compose_content('Этот канал уже привязан к пользователю.')
        case IncomingMessageResultType.LINK_CODE_INVALID:
            return compose_content(
                'Код привязки не принят. Повтори /link <код> с действующим кодом. Если истёк сам код, получи новый через ',
                code_span('/link_code'),
                ' в уже привязанном канале; код действует 10 минут.',
            )
        case IncomingMessageResultType.LINK_REBOUND:
            if result.app_user is None:
                return compose_content('Готово, я перепривязал этот канал.')
            return (
                compose_content(
                    'Готово, я перепривязал этот канал.\nТеперь он связан с профилем «' + str(_display_name(result)) + '».',
                )
            )
        case IncomingMessageResultType.LINK_REBIND_CANCELLED:
            return compose_content('Ок, оставляю текущую привязку без изменений.')


def _display_name(result: ProcessIncomingMessageResult) -> str:
    """Возвращает имя профиля для общего ответа."""
    if result.app_user is None or result.app_user.display_name is None:
        return "пользователь"
    return result.app_user.display_name


def _format_help(capability: CapabilityHelp | None) -> MessageContent:
    """Объединяет общие команды и разделы подключённой возможности."""
    lines = ["Compadre Assistant — справка.", "Доступные команды:"]
    for section in CommandSection:
        commands = [
            command for command in ChatCommand
            if command.section is section and command is not ChatCommand.CANCEL
        ]
        if not commands:
            continue
        lines.extend(("", f"{section.value}:"))
        for command in commands:
            lines.append(str(command))
    if capability is not None:
        for section in capability.sections:
            lines.extend(("", f"{section.title}:"))
            for command in section.commands:
                syntax = " ".join((command.name, *(parameter.syntax for parameter in command.parameters)))
                lines.append(f"{syntax} — {command.description}")
        if capability.instructions:
            lines.extend(("", *capability.instructions))
    else:
        lines.extend(("", "Ответы да и нет подтверждают перепривязку канала."))
    return text_content("\n".join(lines))


def _format_unknown_identity_response() -> MessageContent:
    """Возвращает подсказку для первого входа из нового канала."""
    return (
        compose_content(
            'Я пока не знаю этот канал.\nОтправь ',
            code_span('/register'),
            ', чтобы создать нового пользователя, или ',
            code_span('/link <код>'),
            ', чтобы привязать этот канал к уже существующему пользователю.',
        )
    )
