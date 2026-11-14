"""Явный реестр подключённых точек входа без поиска плагинов."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import re
from dataclasses import dataclass

from compadre_assistant.core.domain.capability_preferences import UserCapabilitySelection

from compadre_assistant.core.presentation.incoming.commands import ChatCommand
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityDescriptor,
    IncomingCapabilityPort,
)


class CapabilityRegistry:
    """Проверяет описание включённых возможностей при сборке приложения.

    Args:
        capabilities: Явно собранные composition root обработчики.
        disabled_capabilities: Метаданные отключённых входов без их сборки.
        profile_access: Проверки допуска по внутреннему ID для ограниченных
            возможностей. Отсутствующая проверка закрывает доступ.

    Raises:
        ValueError: ID или команда некорректны, повторяются либо зарезервированы.
    """

    def __init__(
        self,
        capabilities: Iterable[IncomingCapabilityPort] = (),
        disabled_capabilities: Iterable[CapabilityDescriptor] = (),
        *,
        profile_access: Mapping[str, Callable[[int], bool]] | None = None,
    ) -> None:
        """Проверяет и индексирует явно переданные обработчики."""
        self._by_id: dict[str, IncomingCapabilityPort] = {}
        self._by_command: dict[str, IncomingCapabilityPort] = {}
        self._disabled_by_command: dict[str, CapabilityDescriptor] = {}
        self._mode_command_owners: dict[str, CapabilityDescriptor] = {}
        self._profile_access = dict(profile_access or {})
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
            mode_commands = tuple(
                command for section in descriptor.help.sections for command in section.commands
            )
            for command in (descriptor.entry, *mode_commands):
                if re.fullmatch(r"/[a-z][a-z0-9_]*", command.name) is None:
                    raise ValueError(f"Некорректная команда возможности {identifier}: {command.name!r}")
                if command.name in command_names:
                    raise ValueError(f"Конфликт команды возможностей: {command.name}")
                if not command.description.strip():
                    raise ValueError(f"Нет описания команды: {command.name}")
                command_names.add(command.name)
            for command in mode_commands:
                self._mode_command_owners[command.name] = descriptor

        for capability in capabilities:
            descriptor = capability.descriptor
            register(descriptor)
            self._by_id[descriptor.capability_id] = capability
            self._by_command[descriptor.entry.name] = capability
        for descriptor in disabled_capabilities:
            register(descriptor)
            self._disabled_by_command[descriptor.entry.name] = descriptor
        restricted = {item.capability_id for item in self.descriptors() if item.requires_profile_access}
        if self._profile_access.keys() - restricted:
            raise ValueError("Проверка профиля задана для возможности без ограничения доступа")
        self._restricted_ids = restricted

    @property
    def requires_private_profile_link(self) -> bool:
        """Возвращает необходимость личного чата для операций передачи доступа к профилю."""
        return bool(self._restricted_ids)

    def requires_profile_access(self, capability_id: str) -> bool:
        """Определяет, требует ли известная возможность доверенного профиля."""
        return capability_id in self._restricted_ids

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
        self, selections: tuple[UserCapabilitySelection, ...], *, app_user_id: int | None = None,
    ) -> PersonalCapabilityRegistry:
        """Создаёт снимок допуска и личного выбора для одного события.

        Args:
            selections: Личный выбор профиля.
            app_user_id: ID AppUser из события с явным автором, если он известен.

        Returns:
            Снимок серверной доступности, доверия к профилю и личного выбора.
        """
        selected = {entry.capability_id: entry.enabled for entry in selections}
        return PersonalCapabilityRegistry(
            self,
            tuple(
                CapabilityAccess(
                    descriptor,
                    descriptor.capability_id in self._by_id,
                    selected.get(descriptor.capability_id, descriptor.enabled_by_default),
                    self._profile_allows(descriptor, app_user_id),
                )
                for descriptor in self.descriptors()
            ),
        )

    def _profile_allows(self, descriptor: CapabilityDescriptor, app_user_id: int | None) -> bool:
        if not descriptor.requires_profile_access:
            return True
        check = self._profile_access.get(descriptor.capability_id)
        return app_user_id is not None and check is not None and check(app_user_id)

    def by_command(self, text: str) -> IncomingCapabilityPort | None:
        """Распознаёт точную команду входа, оставляя аргументы обработчику."""
        parts = text.strip().split(maxsplit=1)
        return self._by_command.get(parts[0]) if parts else None

    def disabled_by_command(self, text: str) -> CapabilityDescriptor | None:
        """Возвращает метаданные явно отключённого входа, сохраняя аргументы."""
        parts = text.strip().split(maxsplit=1)
        return self._disabled_by_command.get(parts[0]) if parts else None

    def mode_command_owner(self, command: str) -> CapabilityDescriptor | None:
        """Находит владельца точного имени команды режима.

        Args:
            command: Имя команды без аргументов.

        Returns:
            Описание владельца, в том числе отключённого на сервере, либо
            None для входов, общих и неизвестных команд.
        """
        return self._mode_command_owners.get(command)

    def entry_hint(self) -> str:
        """Предлагает публичные серверные входы при отсутствии контекста профиля."""
        commands = ", ".join(
            command for command, capability in self._by_command.items()
            if not capability.descriptor.requires_profile_access
        )
        if not commands:
            return "Сейчас нет включённых возможностей. Выбор: /capabilities. Справка: /help."
        return f"Выбери возможность: {commands}. Выбор: /capabilities. Справка: /help."


@dataclass(frozen=True, slots=True)
class CapabilityAccess:
    """Показывает серверную доступность, личный выбор и допуск профиля."""

    descriptor: CapabilityDescriptor
    server_available: bool
    user_enabled: bool
    profile_allowed: bool

    @property
    def effective(self) -> bool:
        """Возвращает допуск только при выполнении всех условий."""
        return self.server_available and self.user_enabled and self.profile_allowed


@dataclass(frozen=True, slots=True)
class PersonalCapabilityRegistry:
    """Хранит снимок допуска для одного события без изменения общего реестра."""

    registry: CapabilityRegistry
    access: tuple[CapabilityAccess, ...]

    @property
    def visible_access(self) -> tuple[CapabilityAccess, ...]:
        """Возвращает каталог без возможностей, скрытых от текущего профиля."""
        return tuple(entry for entry in self.access if entry.profile_allowed)

    def visible_mode(self, mode_id: str | None) -> str | None:
        """Скрывает сохранённый режим при отзыве доверенного доступа."""
        if mode_id is None:
            return None
        status = self.status(mode_id)
        return None if status is not None and not status.profile_allowed else mode_id

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

    def entry_hint(self) -> str:
        """Предлагает только эффективно доступные команды входа."""
        commands = ", ".join(
            f"{entry.descriptor.entry.name}" for entry in self.access if entry.effective
        )
        if not commands:
            return "Сейчас нет включённых возможностей. Выбор: /capabilities. Справка: /help."
        return f"Выбери возможность: {commands}. Выбор: /capabilities. Справка: /help."
