from __future__ import annotations

from compadre_assistant.core.presentation.message_content import MessageContent, compose_content, text_content, code_span

from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageResultType, ProcessIncomingMessageResult


PROCESSING_ACK_TEXT = "Принял, обрабатываю. Это может занять немного времени."


def format_incoming_message_result(result: ProcessIncomingMessageResult) -> MessageContent:
    """Формирует только общие ответы регистрации и связывания каналов."""
    match result.type:
        case IncomingMessageResultType.MODE_HINT | IncomingMessageResultType.PROFILE_STATUS | IncomingMessageResultType.CAPABILITY_DISABLED:
            return text_content(result.capability_reply or "Выбери, что сделать дальше.")
        case IncomingMessageResultType.MODE_EXITED:
            return text_content('Режим этого чата завершён.\n' + (result.capability_reply or 'Выбери, что сделать дальше.'))
        case IncomingMessageResultType.MODE_UNAVAILABLE:
            return compose_content(
                'Сохранённая возможность этого чата отключена или недоступна.\n',
                result.capability_reply or 'Выбери другой раздел.',
            )
        case IncomingMessageResultType.UNKNOWN_IDENTITY:
            return text_content('У тебя пока нет профиля в этом чате. Выбери действие ниже.')
        case IncomingMessageResultType.START_UNREGISTERED:
            return text_content('Привет! Я Compadre Assistant. Выбери, как начать.')
        case IncomingMessageResultType.START_REGISTERED:
            return compose_content(
                'Рад тебя видеть, ', _display_name(result), '.\n',
                result.capability_reply or 'Выбери, что сделать дальше.',
            )
        case IncomingMessageResultType.REGISTRATION_NAME_REQUIRED:
            return compose_content(
                'Как к тебе обращаться? Пришли имя или название профиля. '
                'Профиль будет создан после принятия имени. Ожидание действует 10 минут.',
            )
        case IncomingMessageResultType.REGISTRATION_NAME_SAVED:
            return compose_content(
                'Приятно познакомиться, ', _display_name(result), '.\nРегистрация завершена.\n',
                result.capability_reply or 'Выбери, что сделать дальше.',
            )
        case IncomingMessageResultType.NAME_COMMAND_INVALID:
            return text_content('Не получилось сохранить имя. Используй от 1 до 80 символов без ссылки или команды.')
        case IncomingMessageResultType.NAME_UPDATED:
            return compose_content('Готово. Теперь буду обращаться к тебе: ' + str(_display_name(result)) + '.')
        case IncomingMessageResultType.CONFIRMATION_MISSING:
            return text_content(result.capability_reply or 'Сейчас нет действий, которые нужно подтвердить.')
        case IncomingMessageResultType.ALREADY_REGISTERED:
            return compose_content(
                _display_name(result), ', ты уже зарегистрирован.\n',
                result.capability_reply or 'Выбери, что сделать дальше.',
            )
        case IncomingMessageResultType.LINK_CODE_CREATED:
            if result.link_code is None:
                return compose_content('Не удалось создать код привязки. Попробуй позже.')
            return (
                compose_content(
                    'Код привязки: ',
                    code_span(str(result.link_code.code)),
                    '\nКод действует 10 минут. Открой чат с ботом в другом мессенджере, '
                    'выбери «У меня есть код» и пришли код отдельным сообщением. '
                    'Если там уже есть профиль, выбери в настройках мессенджеров привязку к другому профилю.',
                )
            )
        case IncomingMessageResultType.LINK_COMMAND_INVALID:
            return text_content('Пришли код привязки отдельным сообщением.')
        case IncomingMessageResultType.LINKED:
            return compose_content('Готово, я привязал этот канал к твоему пользователю.')
        case IncomingMessageResultType.LINK_ALREADY_BOUND:
            return compose_content('Этот канал уже привязан к пользователю.')
        case IncomingMessageResultType.LINK_CODE_INVALID:
            return text_content(
                'Код привязки не принят. Если срок истёк, получи новый код в настройках мессенджеров другого канала.'
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
