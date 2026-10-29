"""Чистое HTML-представление структурированного ответа Telegram."""

from dataclasses import dataclass
from html import escape

from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction
from compadre_assistant.core.presentation.message_content import MessageContent, TextSpan, TextStyle
from compadre_assistant.presentation.message_rendering import content_groups, split_content


@dataclass(frozen=True, slots=True)
class TelegramChunk:
    """Хранит готовую HTML-часть для последовательной отправки."""

    text: str
    parse_mode: str = "HTML"


def _serialize(span: TextSpan) -> str:
    text = escape(span.text)
    if span.style == TextStyle.EMPHASIS:
        return f"<b>{text}</b>"
    if span.style == TextStyle.CODE:
        return f"<code>{text}</code>"
    return text


def render_telegram_content(
    content: MessageContent, actions: tuple[ConfirmationAction, ...] = (), *, limit: int = 3900,
) -> tuple[TelegramChunk, ...]:
    """Экранирует данные и возвращает самостоятельные закрытые HTML-части.

    Args:
        content: Нейтральные блоки ответа.
        actions: Действия в порядке вывода после блоков.
        limit: Лимит сериализованного HTML в UTF-16 code units, включая теги
            и entities; это консервативнее лимита видимого текста Telegram.

    Returns:
        Готовые части; пустой ответ не создаёт пустого сообщения API.

    Raises:
        ValueError: Лимит не вмещает даже один экранированный символ со стилем.
    """
    groups = split_content(
        content_groups(content, actions), limit=limit,
        measure=lambda span: len(_serialize(span).encode("utf-16-le")) // 2,
    )
    return tuple(TelegramChunk("".join(_serialize(span) for span in group)) for group in groups)
