from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

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
class CommandSuggestion:
    """Описывает пункт меню с выполнимой командой без заполнителей аргументов."""

    command: str
    description: str


@dataclass(frozen=True, slots=True)
class CommandMenu:
    """Передаёт меню текущего чата отдельно от действий конкретного ответа."""

    items: tuple[CommandSuggestion, ...] = ()
    pending_input_label: str | None = None  # Тип ожидаемого значения поверх базового режима.
    pending_choices: tuple[str, ...] = ()  # Временные текстовые значения для каналов с клавиатурой.


@dataclass(frozen=True, slots=True)
class CommandMenuUpdate:
    """Связывает новое меню с адресом чата для адаптера канала."""

    address: ReplyAddress
    menu: CommandMenu


CommandMenuUpdateHandler = Callable[[CommandMenuUpdate], None]


@dataclass(frozen=True, slots=True)
class ReplyAction:
    """Предлагает следующий шаг ответа с точной командой для ручного ввода.

    Подпись предназначена для отображения, а command — для выполнения через
    обычный вход с повторной проверкой доступа. Адресные подтверждения
    передаются только через ConfirmationAction, без дублирования здесь.
    """

    label: str
    command: str


@dataclass(frozen=True, slots=True)
class ConfirmationAction:
    """Описывает адресное действие; конкретный сценарий определяет его команду."""

    action_id: str
    label: str
    command: str


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """Передаёт адресное ожидание владельца без его внутренних моделей."""

    owner_id: str
    operation: str
    confirmation_id: int
    version: int
    origin: ReplyAddress
    actions: tuple[ConfirmationAction, ...]


@dataclass(frozen=True, slots=True)
class ConfirmationQueryResult:
    """Отличает проверенный список от неполноты, сохраняя известные действия."""

    status: Literal["complete", "unavailable"]  # Ошибка источника не означает пустой список.
    confirmations: tuple[PendingConfirmation, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundReplyAction:
    """Передаёт адаптеру подпись, opaque token и ID конкретного ответа.

    Только token отправляется назад через select_action; подпись не исполняется.
    response_id позволяет сопоставить кнопки с отправленным сообщением канала.
    """

    label: str
    token: str
    response_id: str


@dataclass(frozen=True, slots=True)
class IncomingResponse:
    """Передаёт содержимое и явно выбранные владельцем действия ответа.

    actions содержит точные адресные подтверждения; suggested_actions —
    другие следующие шаги. Полное меню не копируется в ответ автоматически.
    Текст содержимого сохраняет ручной путь независимо от поддержки кнопок.
    """

    content: MessageContent
    error: Exception | None = None
    actions: tuple[ConfirmationAction, ...] = ()
    suggested_actions: tuple[ReplyAction, ...] = ()
    bound_actions: tuple[BoundReplyAction, ...] = ()


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


@runtime_checkable
class PendingConfirmationQueryPort(Protocol):
    """Предоставляет актуальные подтверждения активной возможности только для чтения."""

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Читает действия пользователя в исходном чате без выбора решения.

        Args:
            context: Проверенный пользователь текущего события.
            origin: Канал и чат, для которых запрошены действия.

        Returns:
            Полный список либо неполный снимок при недоступности источника.
        """
        ...


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
    action: Callable[[], IncomingResponse] | None = None  # Долгое действие после атомарного принятия ввода.

    def __post_init__(self) -> None:
        """Запрещает внешнее действие до окончательного принятия значения."""
        if self.action is not None and self.outcome is not PendingInputOutcome.COMPLETE:
            raise ValueError("Отложенное действие требует complete")


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
            Долгую операцию нужно вернуть в action: ядро выполнит её вне
            блокировки чата после атомарной очистки ожидания.
        """
        ...
