from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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


def help_sections() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Возвращает разделы предметных команд в прежнем порядке справки."""
    return (
        (
            "Obsidian vault",
            (
                "/github_status — показать состояние локальной копии vault",
                "/github_sync — проверить GitHub и синхронизировать vault",
                "/github_disconnect — отключить vault после подтверждения",
            ),
        ),
        ("Статьи", ("/reanalyze <ID статьи> — выполнить анализ статьи повторно",)),
    )
