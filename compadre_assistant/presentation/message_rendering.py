"""Общие чистые операции адаптеров над структурой и границами текста."""

from collections.abc import Callable
import re
import unicodedata

from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction
from compadre_assistant.core.presentation.message_content import (
    BulletList, MessageContent, Paragraph, TextSpan, confirmation_action_paragraph,
)


def content_groups(
    content: MessageContent, actions: tuple[ConfirmationAction, ...],
) -> tuple[tuple[TextSpan, ...], ...]:
    """Разворачивает блоки и действия по порядку, сохраняя буквальные данные.

    Абзацы разделяет двумя LF, пункты списка — одним LF с маркером «• ».
    Пустые структурные блоки пропускает; пробелы непустых spans не удаляет.

    Args:
        content: Упорядоченные нейтральные блоки ответа.
        actions: Адресные действия для вывода после основных блоков.

    Returns:
        Группы spans с буквальными разделителями и текстовыми командами.

    Raises:
        TypeError: Передан неподдерживаемый тип блока.
    """
    groups: list[tuple[TextSpan, ...]] = []
    blocks = (*content.blocks, *(confirmation_action_paragraph(a) for a in actions))
    for block in blocks:
        spans: list[TextSpan] = []
        if isinstance(block, Paragraph):
            spans.extend(block.spans)
        elif isinstance(block, BulletList):
            for index, item in enumerate(block.items):
                spans.append(TextSpan("\n• " if index else "• "))
                spans.extend(item.spans)
        else:
            raise TypeError(f"Unsupported message block: {type(block)!r}")
        if not any(span.text for span in spans):
            continue
        if groups:
            spans.insert(0, TextSpan("\n\n"))
        groups.append(tuple(span for span in spans if span.text))
    return tuple(groups)


def _safe_boundaries(text: str) -> list[int]:
    """Находит границы типичных составных символов без внешней зависимости.

    Сохраняет combining marks, variation selectors, emoji modifiers, ZWJ,
    пары regional indicators и CRLF. Это не полная реализация Unicode UAX #29.
    """
    boundaries = [0]
    regional_count = 0
    for index, char in enumerate(text):
        code = ord(char)
        regional = 0x1F1E6 <= code <= 0x1F1FF
        if index:
            previous = text[index - 1]
            attached = (
                unicodedata.category(char) in {"Mn", "Mc", "Me"}
                or 0x1F3FB <= code <= 0x1F3FF
                or 0xE0020 <= code <= 0xE007F
                or char == "\u200d" or previous == "\u200d"
                or (previous == "\r" and char == "\n")
                or (regional and regional_count % 2 == 1)
            )
            if not attached:
                boundaries.append(index)
        regional_count = regional_count + 1 if regional else 0
    boundaries.append(len(text))
    return boundaries


def split_content(
    groups: tuple[tuple[TextSpan, ...], ...], *, limit: int,
    measure: Callable[[TextSpan], int],
) -> tuple[tuple[TextSpan, ...], ...]:
    """Укладывает блоки и spans в лимит канала без потери символов.

    Args:
        groups: Блоки с явными разделителями.
        limit: Положительный лимит готовой части.
        measure: Стоимость span с учётом сериализации и служебных тегов.

    Returns:
        Непустые части в исходном порядке; для пустого содержания — пустой tuple.

    Raises:
        ValueError: Лимит неположительный или не вмещает составной символ со стилем.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    chunks: list[tuple[TextSpan, ...]] = []
    current: list[TextSpan] = []
    size = 0

    def flush() -> None:
        nonlocal size
        if current:
            chunks.append(tuple(current))
            current.clear()
            size = 0

    def append(span: TextSpan) -> None:
        nonlocal size
        cost = measure(span)
        if size + cost > limit:
            flush()
        current.append(span)
        size += cost

    for group in groups:
        cost = sum(measure(span) for span in group)
        if cost <= limit:
            if size + cost > limit:
                flush()
            for span in group:
                append(span)
            continue
        for span in group:
            if measure(span) <= limit:
                append(span)
                continue
            # Сначала сохраняем целые слова/URL, затем режем только слишком
            # длинный токен. Пробелы остаются данными, а не отбрасываются.
            for token in re.findall(r"\s+|\S+", span.text):
                fragment = TextSpan(token, span.style)
                if measure(fragment) <= limit:
                    append(fragment)
                    continue
                flush()
                start = 0
                boundaries = _safe_boundaries(token)
                while start < len(token):
                    low, high = 0, len(boundaries) - 1
                    while low < high:
                        middle = (low + high + 1) // 2
                        if measure(TextSpan(token[start:boundaries[middle]], span.style)) <= limit:
                            low = middle
                        else:
                            high = middle - 1
                    end = boundaries[low]
                    if end <= start:
                        raise ValueError("limit cannot fit a styled grapheme cluster")
                    append(TextSpan(token[start:end], span.style))
                    start = end
                    if start < len(token):
                        flush()
    flush()
    text = "".join(span.text for chunk in chunks for span in chunk)
    boundaries = _safe_boundaries(text)
    safe = set(boundaries)
    offset = 0
    for chunk in chunks[:-1]:
        offset += sum(len(span.text) for span in chunk)
        if offset not in safe:
            # Presentation может разделить основу и combining mark разными
            # стилями. В этом редком случае перепаковываем атомарные clusters,
            # сохраняя стили всех их частей; повреждённый cluster не отправляем.
            return _pack_clusters(groups, boundaries, limit=limit, measure=measure)
    return tuple(chunks)


def _pack_clusters(
    groups: tuple[tuple[TextSpan, ...], ...], boundaries: list[int], *,
    limit: int, measure: Callable[[TextSpan], int],
) -> tuple[tuple[TextSpan, ...], ...]:
    """Перепаковывает составные символы, пересекающие исходные spans."""
    source = iter(span for group in groups for span in group)
    span = next(source)
    position = 0
    chunks: list[tuple[TextSpan, ...]] = []
    current: list[TextSpan] = []
    size = 0
    for start, end in zip(boundaries, boundaries[1:]):
        remaining = end - start
        cluster: list[TextSpan] = []
        while remaining:
            count = min(remaining, len(span.text) - position)
            cluster.append(TextSpan(span.text[position:position + count], span.style))
            remaining -= count
            position += count
            if position == len(span.text):
                span = next(source, None)
                position = 0
        cost = sum(measure(part) for part in cluster)
        if cost > limit:
            raise ValueError("limit cannot fit a styled grapheme cluster")
        if current and size + cost > limit:
            chunks.append(tuple(current))
            current = []
            size = 0
        current.extend(cluster)
        size += cost
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)
