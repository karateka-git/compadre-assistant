"""Нейтральные блоки ответа без разметки и зависимостей от каналов."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction


class TextStyle(StrEnum):
    """Задаёт смысл оформления; способ отображения выбирает канал."""

    PLAIN = "plain"  # Обычный текст без выделения.
    EMPHASIS = "emphasis"  # Короткий смысловой акцент.
    CODE = "code"  # Команда, путь или другое значение для копирования.


@dataclass(frozen=True, slots=True)
class TextSpan:
    """Хранит буквальный текст и пожелание к его оформлению.

    Args:
        text: Исходные символы, включая HTML/Markdown из внешних данных.
            Разметка не разбирается, пробелы и переносы не нормализуются.
        style: Смысловой стиль без тегов или ограничителей в самом тексте.
    """

    text: str
    style: TextStyle = TextStyle.PLAIN


@dataclass(frozen=True, slots=True)
class Paragraph:
    """Объединяет фрагменты абзаца в порядке чтения без добавочных пробелов."""

    spans: tuple[TextSpan, ...]


@dataclass(frozen=True, slots=True)
class ListItem:
    """Хранит фрагменты одного пункта; маркер списка добавляет канал."""

    spans: tuple[TextSpan, ...]


@dataclass(frozen=True, slots=True)
class BulletList:
    """Сохраняет порядок пунктов маркированного списка."""

    items: tuple[ListItem, ...]


@dataclass(frozen=True, slots=True)
class MessageContent:
    """Передаёт упорядоченные абзацы и списки независимо от канала доставки."""

    blocks: tuple[Paragraph | BulletList, ...]


def confirmation_action_paragraph(action: ConfirmationAction) -> Paragraph:
    """Представляет действие подписью и полной командой для текстового вывода.

    Args:
        action: Готовое адресное действие. Его владелец задаёт подпись и полную
            команду; эта функция не выбирает и не проверяет предметные аргументы.

    Returns:
        Абзац вида «подпись: команда» без HTML/Markdown-ограничителей. Команда
        сохраняется целиком в отдельном CODE-фрагменте для копирования даже
        в канале без кнопок и поддержки оформления.
    """
    return Paragraph(spans=(
        TextSpan(action.label),
        TextSpan(": "),
        TextSpan(action.command, TextStyle.CODE),
    ))
