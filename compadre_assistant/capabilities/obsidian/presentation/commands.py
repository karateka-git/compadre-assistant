from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CommandParameter, HelpSection,
)


class ObsidianCommand(StrEnum):
    """Перечисляет команды, принадлежащие возможности Obsidian."""

    GITHUB_STATUS = "/github_status"
    GITHUB_SYNC = "/github_sync"
    GITHUB_DISCONNECT = "/github_disconnect"
    REANALYZE = "/reanalyze"


@dataclass(frozen=True, slots=True)
class ParsedObsidianCommand:
    """Содержит команду Obsidian и её аргументы."""

    command: ObsidianCommand
    arguments: str = ""

    @classmethod
    def parse(cls, text: str) -> ParsedObsidianCommand | None:
        """Распознаёт точное имя предметной команды.

        Args:
            text: Полный текст входящего сообщения.

        Returns:
            Команду с аргументами или `None` для другого текста.
        """
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        try:
            command = ObsidianCommand(parts[0])
        except ValueError:
            return None
        return cls(command, parts[1].strip() if len(parts) == 2 else "")


def help_sections() -> tuple[HelpSection, ...]:
    """Возвращает структурированные команды vault и статей в порядке справки."""
    return (
        HelpSection("Obsidian vault", (
            CapabilityCommand(ObsidianCommand.GITHUB_STATUS.value, (), "показать состояние локальной копии vault"),
            CapabilityCommand(ObsidianCommand.GITHUB_SYNC.value, (), "проверить GitHub и синхронизировать vault"),
            CapabilityCommand(ObsidianCommand.GITHUB_DISCONNECT.value, (), "отключить vault после подтверждения"),
        )),
        HelpSection("Статьи", (
            CapabilityCommand(ObsidianCommand.REANALYZE.value,
                              (CommandParameter("ID статьи", "<ID статьи>", False),),
                              "выполнить анализ статьи повторно"),
        )),
    )


def obsidian_descriptor() -> CapabilityDescriptor:
    """Объявляет полный каталог Obsidian без сборки внешних зависимостей."""
    return CapabilityDescriptor(
        "obsidian",
        CapabilityCommand("/obsidian", (CommandParameter("текст или URL", "<текст или URL>", False),),
                          "работа со статьями и Obsidian vault"),
        CapabilityHelp(
            sections=help_sections(),
            instructions=(
                "Подключение vault выполняется внутри этого режима.",
                "После настройки пришли HTTP/HTTPS-ссылку — сохранить и проанализировать статью.",
            ),
        ),
    )
