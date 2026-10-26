"""Явный реестр подключённых точек входа без поиска плагинов."""

from __future__ import annotations

from collections.abc import Iterable
import re
from dataclasses import dataclass, replace

from compadre_assistant.core.domain.capability_preferences import UserCapabilitySelection

from compadre_assistant.core.presentation.incoming.commands import ChatCommand
from compadre_assistant.core.presentation.incoming.ports import (
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
        self._disabled_by_command: dict[str, CapabilityDescriptor] = {}
        identifiers: set[str] = set()
        command_names = {item.value for item in ChatCommand}

        def register(descriptor: CapabilityDescriptor) -> None:
            """Проверяет весь каталог, включая команды отключённой возможности."""
            identifier = descriptor.capability_id
            if not identifier.strip() or identifier != identifier.strip():
                raise ValueError(f"Некорректный ID возможности: {identifier!r}")
            if identifier in identifiers:
                raise ValueError(f"Конфликт ID возможностей: {identifier}")
            identifiers.add(identifier)
            commands = (
                descriptor.entry,
                *(command for section in descriptor.help.sections for command in section.commands),
            )
            for command in commands:
                if re.fullmatch(r"/[a-z][a-z0-9_]*", command.name) is None:
                    raise ValueError(f"Некорректная команда возможности {identifier}: {command.name!r}")
                if command.name in command_names:
                    raise ValueError(f"Конфликт команды возможностей: {command.name}")
                if not command.description.strip():
                    raise ValueError(f"Нет описания команды: {command.name}")
                command_names.add(command.name)

        for capability in capabilities:
            descriptor = capability.descriptor
            register(descriptor)
            self._by_id[descriptor.capability_id] = capability
            self._by_command[descriptor.entry.name] = capability
        for descriptor in disabled_capabilities:
            register(descriptor)
            self._disabled_by_command[descriptor.entry.name] = descriptor

    def by_id(self, capability_id: str) -> IncomingCapabilityPort | None:
        """Возвращает включённый обработчик сохранённого ID."""
        return self._by_id.get(capability_id)

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        """Возвращает каталог собранных и отключённых входов без создания клиентов."""
        return (
            *(entry.descriptor for entry in self._by_id.values()),
            *self._disabled_by_command.values(),
        )

    def for_user(
        self, selections: tuple[UserCapabilitySelection, ...]
    ) -> PersonalCapabilityRegistry:
        """Создаёт неизменяемый снимок доступа по личному выбору AppUser."""
        selected = {entry.capability_id: entry.enabled for entry in selections}
        return PersonalCapabilityRegistry(
            self,
            tuple(
                CapabilityAccess(
                    descriptor,
                    descriptor.capability_id in self._by_id,
                    selected.get(descriptor.capability_id, descriptor.enabled_by_default),
                )
                for descriptor in self.descriptors()
            ),
        )

    def by_command(self, text: str) -> IncomingCapabilityPort | None:
        """Распознаёт точную команду входа, оставляя аргументы обработчику."""
        parts = text.strip().split(maxsplit=1)
        return self._by_command.get(parts[0]) if parts else None

    def help(self) -> CapabilityHelp:
        """Формирует справку всех включённых входов и способ выхода."""
        entries = tuple(
            replace(entry.descriptor.entry, description=entry.descriptor.entry.description
                    + ("; выход: /exit" if entry.descriptor.persistent_mode else ""))
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
        commands = ", ".join(f"{command}" for command in self._by_command)
        if not commands:
            return "Сейчас нет включённых возможностей. Выбор: /capabilities. Справка: /help."
        return f"Выбери возможность: {commands}. Выбор: /capabilities. Справка: /help."


@dataclass(frozen=True, slots=True)
class CapabilityAccess:
    """Показывает глобальную доступность и личный выбор одного входа."""

    descriptor: CapabilityDescriptor
    server_available: bool
    user_enabled: bool

    @property
    def effective(self) -> bool:
        """Возвращает допуск только при двух выполненных условиях."""
        return self.server_available and self.user_enabled


@dataclass(frozen=True, slots=True)
class PersonalCapabilityRegistry:
    """Хранит снимок допуска для одного события без изменения общего реестра."""

    registry: CapabilityRegistry
    access: tuple[CapabilityAccess, ...]

    def status(self, capability_id: str) -> CapabilityAccess | None:
        """Возвращает состояние известного ID или None для неизвестного."""
        return next(
            (entry for entry in self.access if entry.descriptor.capability_id == capability_id),
            None,
        )

    def by_id(self, capability_id: str) -> IncomingCapabilityPort | None:
        """Возвращает обработчик только эффективно доступного ID."""
        status = self.status(capability_id)
        return self.registry.by_id(capability_id) if status is not None and status.effective else None

    def by_command(self, text: str) -> IncomingCapabilityPort | None:
        """Возвращает доступный обработчик команды входа."""
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        return next(
            (self.by_id(entry.descriptor.capability_id) for entry in self.access
             if entry.descriptor.entry.name == parts[0]),
            None,
        )

    def blocked_by_command(self, text: str) -> CapabilityAccess | None:
        """Возвращает недоступный вход по точной команде без вызова обработчика."""
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        return next(
            (entry for entry in self.access
             if entry.descriptor.entry.name == parts[0] and not entry.effective),
            None,
        )

    def help(self) -> CapabilityHelp:
        """Формирует справку только эффективно доступных возможностей."""
        entries = tuple(entry for entry in self.access if entry.effective)
        commands = tuple(
            replace(entry.descriptor.entry, description=entry.descriptor.entry.description
                    + ("; выход: /exit" if entry.descriptor.persistent_mode else ""))
            for entry in entries
        )
        sections = (HelpSection("Возможности", commands),) if commands else ()
        instructions: tuple[str, ...] = ()
        for entry in entries:
            sections += entry.descriptor.help.sections
            instructions += entry.descriptor.help.instructions
        return CapabilityHelp(sections, instructions)

    def entry_hint(self) -> str:
        """Предлагает только эффективно доступные команды входа."""
        commands = ", ".join(
            f"{entry.descriptor.entry.name}" for entry in self.access if entry.effective
        )
        if not commands:
            return "Сейчас нет включённых возможностей. Выбор: /capabilities. Справка: /help."
        return f"Выбери возможность: {commands}. Выбор: /capabilities. Справка: /help."
