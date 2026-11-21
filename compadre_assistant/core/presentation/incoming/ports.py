from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionLayoutLimits, ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.commands import ChatCommand
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
    instructions: tuple[MessageContent, ...] = ()
    menu_help_only: bool = False  # В общем меню раздел представлен командой /help.


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
    # None — нет проекции раздела для этого контекста; () — раздел без действий.
    section_actions: tuple[ReplyAction, ...] | None = None


@dataclass(frozen=True, slots=True)
class CommandMenuUpdate:
    """Связывает новое меню с адресом чата для адаптера канала."""

    address: ReplyAddress
    menu: CommandMenu


# False означает, что канал оставил меню без публикации.
CommandMenuUpdateHandler = Callable[[CommandMenuUpdate], bool | None]


@dataclass(frozen=True, slots=True)
class ReplyAction:
    """Описывает один доступный шаг экрана или раздела для обоих каналов.

    command выполняется через обычный вход с повторной проверкой доступа.
    url открывает внешнюю страницу и не исполняет команду. Владелец возвращает
    только доступные действия; адаптер выбирает место показа по scope.
    Адресные подтверждения остаются ConfirmationAction с проверкой ожидания.
    purpose и destination описывают смысл, но не заменяют предметную проверку.
    """

    label: str
    command: str = ""
    row: int | None = None  # Номер ряда, если ответу нужна явная раскладка.
    scope: ActionScope = ActionScope.SCREEN
    purpose: ActionPurpose = ActionPurpose.ACTION
    destination: ScreenDestination | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        """Проверяет единственность цели и явный контекст нового перехода.

        Raises:
            ValueError: Цель отсутствует, ссылка неоднозначна или не является HTTP(S), либо
                контекст перехода противоречит назначению действия.
        """
        if self.url is None and not self.command.strip():
            raise ValueError("action must have a command or URL target")
        if self.url is not None:
            parsed = urlsplit(self.url)
            if self.command or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("link action must have an HTTP(S) URL and no command")
        if self.purpose == ActionPurpose.NAVIGATION and self.destination is None:
            raise ValueError("navigation action must name its screen destination")
        if self.destination is not None and self.purpose != ActionPurpose.NAVIGATION:
            raise ValueError("screen destination is only valid for navigation")
        if self.purpose == ActionPurpose.SELECTION and self.scope != ActionScope.SCREEN:
            raise ValueError("an addressed selection must belong to its screen")


# Закрывает ожидание, когда доступна только отмена ввода.
CANCEL_SECTION_ACTION = ReplyAction("Отмена", "/cancel", scope=ActionScope.SECTION, purpose=ActionPurpose.CANCEL)


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
    При url токен пуст и действие только открывает проверенную ссылку.
    response_id позволяет сопоставить кнопки с отправленным сообщением канала.
    """

    label: str
    token: str
    response_id: str
    row: int | None = None
    scope: ActionScope = ActionScope.SCREEN
    purpose: ActionPurpose = ActionPurpose.ACTION
    destination: ScreenDestination | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ResponseView:
    """Просит опубликовать или обновить одно именованное представление чата.

    Args:
        key: Имя представления внутри активной возможности.
        update: При True изменить последнюю подтверждённую доставку
            представления, с кнопки только сообщение, на котором её нажали.
        unavailable: Ответ для случая, когда обновление не состоялось.
            Владелец указывает, как заново открыть представление.
        on_delivered: Уведомление владельца после подтверждённой доставки
            сообщения. Порядок предметных состояний контролирует владелец.
        cleanup_previous: После доставки снять клавиатуры прежних отслеживаемых
            представлений этой возможности в исходном чате.
        allow_multiple_messages: Разрешает адаптеру опубликовать представление
            несколькими сообщениями с кнопками на последнем.
        action_scope: Основная область действий представления. Канал с отдельным
            меню может публиковать новый ответ при переходе между областями.
    """

    key: str
    update: bool = False
    unavailable: MessageContent | None = None
    on_delivered: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    cleanup_previous: bool = False
    allow_multiple_messages: bool = False
    action_scope: ActionScope | None = None

    def __post_init__(self) -> None:
        """Требует устойчивое имя и подсказку для недоступного обновления."""
        if not self.key or self.key.strip() != self.key or len(self.key) > 64:
            raise ValueError("response view key must contain 1–64 characters without surrounding spaces")
        if self.update and self.unavailable is None:
            raise ValueError("response view update must supply a recovery message")


@dataclass(frozen=True, slots=True)
class ResponseViewDelivery:
    """Диспетчер выдаёт адаптеру одну попытку доставки представления.

    Args:
        target_message_id: Подтверждённый ID редактируемого сообщения канала.
            None означает публикацию нового сообщения.
        unavailable: Текст подсказки при невозможности обновить сообщение.
        start: Проверяет, что попытка ещё актуальна, и резервирует доставку.
            True допускает одну доставку, включая разрешённые части и повторы.
        finish: После допуска фиксирует ID сообщения только при успехе.
            При редактировании передаётся тот же ID, при сбое или отмене None.
            Возвращает ID снятых с учёта сообщений для очистки их клавиатур.
        allow_multiple_messages: Разрешает последовательную доставку частей.
        target_has_multiple_messages: Прежняя доставка состоит из частей,
            поэтому обновление требует нового сообщения вместо редактирования.
        finish_replacement: Подтверждает ID последнего нового сообщения после
            успешной доставки всех частей и принимает признак нескольких частей.
            При сбое принимает None, сохраняя прежнюю подтверждённую доставку.
        section_transition: Действия переходят между разделом и экраном.
        unavailable_actions: Проверенные кнопки для отдельной подсказки после сбоя.
    """

    target_message_id: str | None
    unavailable: MessageContent | None
    start: Callable[[], bool] = field(repr=False, compare=False)
    finish: Callable[[str | None], tuple[str, ...]] = field(repr=False, compare=False)
    allow_multiple_messages: bool = False
    target_has_multiple_messages: bool = False
    finish_replacement: Callable[[str | None, bool], tuple[str, ...]] | None = field(
        default=None, repr=False, compare=False,
    )
    section_transition: bool = False
    unavailable_actions: tuple[BoundReplyAction, ...] = ()


@dataclass(frozen=True, slots=True)
class IncomingResponse:
    """Передаёт содержимое и явно выбранные владельцем действия ответа.

    actions содержит точные адресные подтверждения; suggested_actions —
    другие следующие шаги. Полное меню не копируется в ответ автоматически.
    Справка содержит ручной путь для текущего контекста.
    """

    content: MessageContent
    error: Exception | None = None
    actions: tuple[ConfirmationAction, ...] = ()
    suggested_actions: tuple[ReplyAction, ...] = ()
    bound_actions: tuple[BoundReplyAction, ...] = ()
    # Адаптер исходного ответа сообщает результат отправки ровно один раз.
    after_delivery: Callable[[bool], None] | None = field(default=None, repr=False, compare=False)
    # Адаптер оставляет клавиатуру до управляемой замены или после отклонённого выбора.
    preserve_source_actions: bool = False
    view: ResponseView | None = None  # Намерение владельца ответа до привязки.
    view_delivery: ResponseViewDelivery | None = field(default=None, repr=False, compare=False)
    # Режим доставки нижнего меню при отправке содержательного ответа.
    command_menu_delivery: SectionMenuDelivery = SectionMenuDelivery.RECONCILE
    close_managed_views: bool = False  # Ответ заменяет отслеживаемый экран без редактирования.
    retire_views_after_delivery: Callable[[], tuple[str, ...]] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class BackgroundReply:
    """Связывает последующий общий ответ с сохранённым адресом канала."""

    address: ReplyAddress
    response: IncomingResponse


ChannelCompletionHandler = Callable[[BackgroundReply], None]
# Доставляет ответ с меню. ID ожидания связывает повторы отправки.
PendingInputExpiryHandler = Callable[[str, IncomingResponse, CommandMenuUpdate], bool]


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
    # Привязано к текущему событию и выдаётся зарегистрированному владельцу команд.
    profile_operation: Callable[[ChatCommand, str], IncomingResponse] | None = None
    # Ревизия режима владельца, уже выбранного в этом чате. При входе её ещё нет.
    mode_revision: int | None = None
    action_layout: ActionLayoutLimits = ActionLayoutLimits()


@runtime_checkable
class NotificationEntryPort(Protocol):
    """Разрешает адресную кнопку фонового уведомления в команду раздела."""

    def notification_destination(self, app_user_id: int, address: ReplyAddress, reference: str) -> str | None:
        """Возвращает команду своего режима после проверки получателя и ссылки."""
        ...


@runtime_checkable
class SectionActionsQueryPort(Protocol):
    """Читает действия раздела из того же описания, что использует его экран."""

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
        pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Возвращает актуальные действия без отправки ответа и исполнения команд.

        Args:
            app_user: Профиль с проверенным действующим доступом.
            address: Исходный чат с активным режимом.
            mode_revision: Текущая ревизия этого режима.
            pending: Проверенное ожидание ввода, если оно существует.

        Returns:
            Действия с областью SECTION. Пустой кортеж означает отсутствие
            действий, а не просьбу сохранить прежнее меню. Для сохранения
            используется SectionMenuDelivery.PRESERVE у ответа. Ошибка чтения
            не должна превращаться в достоверный пустой набор.
        """
        ...


@runtime_checkable
class ContextualCommandQueryPort(Protocol):
    """Отбирает объявленные команды активной возможности для справки и меню."""

    def visible_mode_commands(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
    ) -> frozenset[str]:
        """Читает видимые команды раздела без исполнения действий.

        Args:
            app_user: Проверенный профиль с действующим доступом к режиму.
            address: Адрес чата, где выбран этот режим.
            mode_revision: Ревизия активного режима в указанном чате.

        Returns:
            Имена команд из статического каталога возможности. Пустое
            множество оставляет только общие команды и другие доступные входы.
        """
        ...


@runtime_checkable
class ReplyActionRecoveryPort(Protocol):
    """Предлагает открыть актуальный экран после недоступного действия."""

    def reply_action_recovery(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int | None,
    ) -> ReplyAction:
        """Возвращает действие по проверенному профилю и текущему разделу.

        Args:
            app_user: Профиль с действующим доступом к возможности.
            address: Личный чат, из которого пришёл callback.
            mode_revision: Ревизия активного режима или None после выхода.

        Returns:
            Кнопка, которая открывает доступный экран.
        """
        ...


@runtime_checkable
class PendingConfirmationQueryPort(Protocol):
    """Предоставляет актуальные подтверждения активной возможности только для чтения."""

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Читает действия пользователя в исходном чате без выбора решения.

        Args:
            context: Проверенный пользователь и ревизия активного режима в исходном чате.
            origin: Канал и чат, для которых запрошены действия.

        Returns:
            Полный список либо неполный снимок при недоступности источника.
        """
        ...


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Объявляет ID, команду входа, общую справку и отображаемое название."""

    capability_id: str
    entry: CapabilityCommand
    help: CapabilityHelp = CapabilityHelp()
    persistent_mode: bool = True  # Длительный вход требует явного выхода из режима.
    enabled_by_default: bool = False  # Новый встроенный ID не получает личный доступ автоматически.
    accepts_images: bool = False  # Presentation возможности умеет обрабатывать события с фото.
    requires_profile_access: bool = False  # Только профили, допущенные оператором.
    display_name: str | None = None  # Публичное название для ответов о доступе.
    personally_configurable: bool = True  # Пользователь может менять личный выбор.
    profile_commands: frozenset[ChatCommand] = frozenset()  # Разрешённые операции профиля ядра.
    private_chat_hint: str | None = None  # Пояснение для входа, доступного только в личном чате.


class CapabilityMode(StrEnum):
    """Сообщает ядру, продолжает ли возможность режим конкретного чата."""

    ACTIVE = "active"  # Продолжить или установить длительный режим после успеха.
    COMPLETE = "complete"  # Завершить текущий сценарий; одноразовый вход сохраняет прежний режим.


# Исполнитель получает предикат отмены текущего поколения.
CancellableAction = Callable[[Callable[[], bool]], IncomingResponse]


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """Содержит общий ответ и состояние режима без предметного результата.

    Для обработчика активного режима ответ с cancellable_action служит
    квитанцией. Канал без фоновой доставки получает результат исполнителя.
    """

    response: IncomingResponse
    mode: CapabilityMode = CapabilityMode.ACTIVE
    cancellable_action: CancellableAction | None = field(default=None, repr=False)


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
            event: Исходный ввод без предметного анализа в ядре.

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
    # Возвращает ответ исполнителя; переданный предикат сообщает об отмене.
    cancellable_action: CancellableAction | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Запрещает внешнее действие до окончательного принятия значения."""
        if (self.action is not None or self.cancellable_action is not None) and self.outcome is not PendingInputOutcome.COMPLETE:
            raise ValueError("Отложенное действие требует complete")
        if self.action is not None and self.cancellable_action is not None:
            raise ValueError("Допускается только один исполнитель принятого действия")


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
            Долгую операцию нужно вернуть в action для выполнения вне
            блокировки чата или в cancellable_action для фонового выполнения
            после подтверждённой доставки квитанции. При недоступной фоновой
            доставке ядро выполняет её синхронно после очистки ожидания.
        """
        ...
