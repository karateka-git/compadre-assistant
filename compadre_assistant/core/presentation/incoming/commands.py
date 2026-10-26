from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CommandSection(StrEnum):
    """Раздел пользовательских команд в общей справке."""

    BASIC = "Основные"
    CHANNEL_LINKING = "Связь Telegram и VK"
    CAPABILITIES = "Личные возможности"  # Команды управляют выбором одного AppUser во всех каналах.


class ChatCommand(StrEnum):
    """Описывает поддерживаемую chat-команду и её строку справки.

    Каждый элемент является единым источником имени, аргументов, раздела и
    пользовательского описания. Новый элемент автоматически попадает в /help.
    """

    section: CommandSection
    arguments_hint: str
    description: str

    def __new__(
        cls,
        value: str,
        section: CommandSection,
        arguments_hint: str,
        description: str,
    ) -> ChatCommand:
        member = str.__new__(cls, value)
        member._value_ = value
        member.section = section
        member.arguments_hint = arguments_hint
        member.description = description
        return member

    CONFIRM = (
        "/confirm",
        CommandSection.BASIC,
        "<модуль> <операция> <id> <версия> <yes|no>",
        "подтвердить конкретное действие",
    )
    START = (
        "/start",
        CommandSection.BASIC,
        "",
        "проверить подключение бота и канала",
    )
    HELP = (
        "/help",
        CommandSection.BASIC,
        "",
        "показать список доступных команд",
    )
    EXIT = (
        "/exit",
        CommandSection.BASIC,
        "",
        "выйти из активного режима этого чата",
    )
    REGISTER = (
        "/register",
        CommandSection.BASIC,
        "",
        "зарегистрироваться",
    )
    NAME = (
        "/name",
        CommandSection.BASIC,
        "<имя>",
        "изменить имя или название профиля",
    )
    STATUS = (
        "/status",
        CommandSection.BASIC,
        "",
        "показать состояние подключения",
    )
    CAPABILITIES = (
        "/capabilities",
        CommandSection.CAPABILITIES,
        "",
        "показать личный выбор и серверную доступность возможностей",
    )
    ENABLE = (
        "/enable",
        CommandSection.CAPABILITIES,
        "<id>",
        "включить доступную возможность для своего профиля",
    )
    DISABLE = (
        "/disable",
        CommandSection.CAPABILITIES,
        "<id>",
        "выключить возможность для своего профиля, сохранив режим и данные",
    )
    LINK_CODE = (
        "/link_code",
        CommandSection.CHANNEL_LINKING,
        "",
        "создать код привязки второго канала",
    )
    LINK = (
        "/link",
        CommandSection.CHANNEL_LINKING,
        "<код>",
        "привязать канал по полученному коду",
    )

    @property
    def usage(self) -> str:
        """Возвращает имя команды с подсказкой обязательных аргументов."""
        if not self.arguments_hint:
            return self.value
        return f"{self.value} {self.arguments_hint}"

    def __str__(self) -> str:
        """Форматирует одну строку Markdown для /help."""
        return f"{self.usage} — {self.description}"


@dataclass(frozen=True, slots=True)
class ParsedChatCommand:
    """Содержит распознанную команду и необработанную строку аргументов."""

    command: ChatCommand
    arguments: str = ""

    @classmethod
    def parse(cls, text: str) -> ParsedChatCommand | None:
        """Распознаёт точное имя известной команды.

        Args:
            text: Полный текст входящего сообщения.

        Returns:
            Команду с аргументами или None для обычного текста и неизвестной
            slash-команды.
        """
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        try:
            command = ChatCommand(parts[0])
        except ValueError:
            return None
        arguments = parts[1].strip() if len(parts) == 2 else ""
        return cls(command=command, arguments=arguments)
