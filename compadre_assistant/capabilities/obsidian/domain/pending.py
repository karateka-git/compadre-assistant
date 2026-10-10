"""Адрес и проверяемая версия конкретного интерактивного действия Obsidian."""

from dataclasses import dataclass


class PendingOperationError(RuntimeError):
    """Операция недоступна, устарела или принадлежит другому исходному чату."""


class VaultResourceBusyError(RuntimeError):
    """Конфигурация vault занята другой операцией пользователя."""


@dataclass(frozen=True, slots=True)
class PendingOrigin:
    """Обозначает исходный чат независимо от SDK канала."""

    channel: str
    chat_id: str

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.chat_id.strip():
            raise ValueError("pending origin must contain channel and chat_id")
