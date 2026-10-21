from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.domain.users.entities import AppUser


@dataclass(frozen=True, slots=True)
class HelpSection:
    """Описывает раздел справки без зависимости от конкретной возможности."""

    title: str
    commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityHelp:
    """Дополняет общую справку инструкциями возможности."""

    sections: tuple[HelpSection, ...] = ()
    instructions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfirmationAction:
    """Описывает адресное действие; конкретный сценарий определяет его команду."""

    action_id: str
    label: str
    command: str


@dataclass(frozen=True, slots=True)
class IncomingResponse:
    """Передаёт готовое содержимое и действия без предметных моделей."""

    text: str
    error: Exception | None = None
    actions: tuple[ConfirmationAction, ...] = ()

    def render_text(self) -> str:
        """Добавляет доступные команды действий для текстовых каналов."""
        if not self.actions:
            return self.text
        return "\n".join(
            (self.text, *(f"{action.label}: {action.command}" for action in self.actions))
        )


@dataclass(frozen=True, slots=True)
class BackgroundReply:
    """Связывает последующий общий ответ с сохранённым адресом канала."""

    address: ReplyAddress
    response: IncomingResponse


ChannelCompletionHandler = Callable[[BackgroundReply], None]


class ReplyDeliveryPort(Protocol):
    """Доставляет последующий ответ, не раскрывая SDK обработчику возможности."""

    def send(self, address: ReplyAddress, response: IncomingResponse) -> None:
        """Отправляет готовый ответ по сохранённому адресу канала."""
        ...


@dataclass(frozen=True, slots=True)
class HandlerContext:
    """Передаёт проверенного пользователя и независимую фоновую доставку."""

    app_user: AppUser
    delivery: ReplyDeliveryPort | None = None


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Объявляет устойчивый ID, команду входа и общую справку возможности."""

    capability_id: str
    entry_command: str
    description: str
    help: CapabilityHelp = CapabilityHelp()
    persistent_mode: bool = True  # Длительный вход требует явного выхода из режима.
    enabled_by_default: bool = False  # Новый встроенный ID не получает личный доступ автоматически.


class CapabilityMode(StrEnum):
    """Сообщает ядру, продолжает ли возможность режим конкретного чата."""

    ACTIVE = "active"  # Продолжить или установить длительный режим после успеха.
    COMPLETE = "complete"  # Завершить текущий сценарий; одноразовый вход сохраняет прежний режим.


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """Содержит общий ответ и состояние режима без предметного результата."""

    response: IncomingResponse
    mode: CapabilityMode = CapabilityMode.ACTIVE


class IncomingCapabilityPort(Protocol):
    """Задаёт единый вход подключаемой возможности для любого канала."""

    @property
    def descriptor(self) -> CapabilityDescriptor:
        """Возвращает статическое описание возможности для реестра."""
        ...

    def enter(
        self, context: HandlerContext, event: IncomingEvent, arguments: str
    ) -> CapabilityResult:
        """Входит в возможность и показывает актуальное состояние сценария.

        Args:
            context: Проверенный пользователь и доступная фоновая доставка.
            event: Исходное событие с командой входа.
            arguments: Текст после команды входа без предметного разбора ядром.

        Returns:
            Общий ответ и признак длительного либо завершённого сценария.
        """
        ...

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Обрабатывает сообщение уже выбранного режима.

        Args:
            context: Проверенный пользователь и доступная фоновая доставка.
            event: Исходный текст без предметного анализа в ядре.

        Returns:
            Общий ответ и признак продолжения либо завершения режима.
        """
        ...
