from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol, runtime_checkable

from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput, PendingInputStore
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.message_content import MessageContent


@dataclass(frozen=True, slots=True)
class CommandParameter:
    """Описывает имя, обозначение и обязательность параметра команды."""

    name: str
    syntax: str
    required: bool  # False допускает вызов без значения по сценарию владельца.


@dataclass(frozen=True, slots=True)
class CapabilityCommand:
    """Передаёт точное имя команды, её параметры и краткое назначение."""

    name: str
    parameters: tuple[CommandParameter, ...]
    description: str


@dataclass(frozen=True, slots=True)
class HelpSection:
    """Описывает раздел справки без зависимости от конкретной возможности."""

    title: str
    commands: tuple[CapabilityCommand, ...]


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

    content: MessageContent
    error: Exception | None = None
    actions: tuple[ConfirmationAction, ...] = ()


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
    pending_inputs: PendingInputStore | None = None


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Объявляет устойчивый ID, команду входа и общую справку возможности."""

    capability_id: str
    entry: CapabilityCommand
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


class PendingInputOutcome(StrEnum):
    """Определяет дальнейшую жизнь ожидания независимо от режима чата."""

    RETRY = "retry"  # Неверное значение оставляет исходный срок.
    COMPLETE = "complete"  # Принятый ввод не выполняется повторно автоматически.


@dataclass(frozen=True, slots=True)
class PendingInputResult:
    """Передаёт ответ владельца и решение о завершении ввода."""

    response: IncomingResponse
    outcome: PendingInputOutcome


@runtime_checkable
class PendingInputContinuationPort(Protocol):
    """Дополняет вход возможности публичным продолжением ожидаемого ввода."""

    def continue_input(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent
    ) -> PendingInputResult:
        """Проверяет сырой ввод и сообщает retry либо complete.

        Args:
            context: Повторно проверенный пользователь и общие сервисы входа.
            pending: Исходная запись с типом значения и ID запроса.
            event: Полное событие без преобразования в синтетическую команду.

        Returns:
            Ответ с решением о сохранении исходного ожидания. После принятия
            значения даже ошибка внешнего действия возвращается как complete.
        """
        ...
