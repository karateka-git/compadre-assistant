"""Явный реестр подключённых точек входа без поиска плагинов."""

from collections.abc import Iterable
import re

from obs_chat_bot.core.presentation.incoming.commands import ChatCommand
from obs_chat_bot.core.presentation.incoming.ports import (
    CapabilityHelp,
    CapabilityDescriptor,
    HelpSection,
    IncomingCapabilityPort,
)


class CapabilityRegistry:
    """Проверяет описание включённых возможностей при сборке приложения.

    Args:
        capabilities: Явно собранные composition root обработчики.
        disabled_capabilities: Метаданные отключённых входов без их сборки.

    Raises:
        ValueError: ID или команда некорректны, повторяются либо зарезервированы.
    """

    def __init__(
        self,
        capabilities: Iterable[IncomingCapabilityPort] = (),
        disabled_capabilities: Iterable[CapabilityDescriptor] = (),
    ) -> None:
        """Проверяет и индексирует явно переданные обработчики."""
        self._by_id: dict[str, IncomingCapabilityPort] = {}
        self._by_command: dict[str, IncomingCapabilityPort] = {}
        reserved_commands = {item.value for item in ChatCommand}
        for capability in capabilities:
            descriptor = capability.descriptor
            identifier = descriptor.capability_id
            command = descriptor.entry_command
            if not identifier.strip() or identifier != identifier.strip():
                raise ValueError(f"Некорректный ID возможности: {identifier!r}")
            if re.fullmatch(r"/[a-z][a-z0-9_]*", command) is None:
                raise ValueError(
                    f"Некорректная команда входа возможности {identifier}: {command!r}"
                )
            if identifier in self._by_id:
                raise ValueError(f"Конфликт ID возможностей: {identifier}")
            if command in self._by_command or command in reserved_commands:
                raise ValueError(f"Конфликт команды входа возможностей: {command}")
            if not descriptor.description.strip():
                raise ValueError(f"Нет описания возможности: {identifier}")
            self._by_id[identifier] = capability
            self._by_command[command] = capability
        self._disabled_by_command: dict[str, CapabilityDescriptor] = {}
        disabled_ids: set[str] = set()
        for descriptor in disabled_capabilities:
            if (
                not descriptor.capability_id.strip()
                or descriptor.capability_id != descriptor.capability_id.strip()
            ):
                raise ValueError("Некорректный ID отключённой возможности")
            if re.fullmatch(r"/[a-z][a-z0-9_]*", descriptor.entry_command) is None:
                raise ValueError("Некорректная команда отключённой возможности")
            if (
                descriptor.capability_id in self._by_id
                or descriptor.capability_id in disabled_ids
            ):
                raise ValueError("Конфликт ID отключённых возможностей")
            if (
                descriptor.entry_command in reserved_commands
                or descriptor.entry_command in self._by_command
                or descriptor.entry_command in self._disabled_by_command
            ):
                raise ValueError("Конфликт команды отключённой возможности")
            if not descriptor.description.strip():
                raise ValueError("Нет описания отключённой возможности")
            disabled_ids.add(descriptor.capability_id)
            self._disabled_by_command[descriptor.entry_command] = descriptor

    def by_id(self, capability_id: str) -> IncomingCapabilityPort | None:
        """Возвращает включённый обработчик сохранённого ID."""
        return self._by_id.get(capability_id)

    def by_command(self, text: str) -> IncomingCapabilityPort | None:
        """Распознаёт точную команду входа, оставляя аргументы обработчику."""
        parts = text.strip().split(maxsplit=1)
        return self._by_command.get(parts[0]) if parts else None

    def help(self) -> CapabilityHelp:
        """Формирует справку всех включённых входов и способ выхода."""
        entries = tuple(
            f"`{entry.descriptor.entry_command}` — {entry.descriptor.description}; выход: `/exit`"
            for entry in self._by_id.values()
        )
        sections = (HelpSection("Возможности", entries),) if entries else ()
        instructions: tuple[str, ...] = ()
        for entry in self._by_id.values():
            sections += entry.descriptor.help.sections
            instructions += entry.descriptor.help.instructions
        return CapabilityHelp(sections=sections, instructions=instructions)

    def disabled_by_command(self, text: str) -> CapabilityDescriptor | None:
        """Возвращает метаданные явно отключённого входа, сохраняя аргументы."""
        parts = text.strip().split(maxsplit=1)
        return self._disabled_by_command.get(parts[0]) if parts else None

    def entry_hint(self) -> str:
        """Предлагает доступные команды входа без предметного анализа сообщения."""
        commands = ", ".join(f"`{command}`" for command in self._by_command)
        if not commands:
            return "Сейчас нет включённых возможностей. Справка: `/help`."
        return f"Выбери возможность: {commands}. Справка: `/help`."
