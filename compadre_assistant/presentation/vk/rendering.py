"""Чистое представление VK с консервативными диапазонами format_data."""

from dataclasses import dataclass
import json
import unicodedata

from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction
from compadre_assistant.core.presentation.message_content import MessageContent, TextStyle
from compadre_assistant.presentation.message_rendering import content_groups, split_content


@dataclass(frozen=True, slots=True)
class VkChunk:
    """Хранит буквальный текст и необязательный JSON параметра format_data."""

    text: str
    format_data: str | None = None


def _unambiguous_offsets(text: str) -> bool:
    # Пока live-проба не подтвердила единицы offsets, используем bold только
    # там, где позиции code points, UTF-16 и простых символов совпадают.
    return all(
        ord(char) <= 0xFFFF and unicodedata.category(char) not in {"Mn", "Mc", "Me", "Cf"}
        for char in text
    )


def render_vk_content(
    content: MessageContent, actions: tuple[ConfirmationAction, ...] = (), *, limit: int = 3500,
) -> tuple[VkChunk, ...]:
    """Возвращает текст и диапазоны bold/italic для смысловых стилей.

    Args:
        content: Нейтральные блоки ответа, без доверенной разметки.
        actions: Действия с полными командами после блоков.
        limit: Положительный лимит текста в UTF-16 code units.

    Returns:
        Части с локальными offsets. Для части с non-BMP, combining или
        format-символами заранее выбирается plain fallback: единицы offsets
        для такого текста ещё не подтверждены живой проверкой API 5.199.
        JSON использует version="1", как в сохранённой пробе этапа 3.

    Raises:
        ValueError: Лимит неположительный или не вмещает символ UTF-16.
    """
    groups = split_content(
        content_groups(content, actions), limit=limit,
        measure=lambda span: len(span.text.encode("utf-16-le")) // 2,
    )
    chunks: list[VkChunk] = []
    for group in groups:
        text = "".join(span.text for span in group)
        items = []
        offset = 0
        if _unambiguous_offsets(text):
            for span in group:
                style_type = {
                    TextStyle.EMPHASIS: "bold",
                    TextStyle.CODE: "italic",
                }.get(span.style)
                if style_type and span.text:
                    items.append({"type": style_type, "offset": offset, "length": len(span.text)})
                offset += len(span.text)
        data = json.dumps({"version": "1", "items": items}) if items else None
        chunks.append(VkChunk(text, data))
    return tuple(chunks)
