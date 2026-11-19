"""Хранит раздел покупок в контексте чата и показывает общий список."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from secrets import token_urlsafe
from threading import Event, Lock

from compadre_assistant.capabilities.family.domain.shopping import (
    MAX_ITEM_NUMBER, ItemChange, ItemChangeStatus, ShoppingItem, ShoppingItemStatus,
)
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionLayoutLimits, ActionPurpose, ActionScope, ScreenDestination,
)
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse, ReplyAction, ResponseView
from compadre_assistant.core.presentation.message_content import TextSpan, code_span, compose_content


SHOPPING_COMMAND = "/family_shopping"  # Вход в покупки и перемещение по списку.
ADD_COMMAND = "/add"  # Добавление текста из команды или следующего сообщения.
BUY_COMMAND = "/buy"  # Открывает выбор покупки для отметки.
DELETE_COMMAND = "/delete"  # Открывает выбор покупки для удаления.
CLEAR_BOUGHT_COMMAND = "/family_clear_bought"  # Очистка купленных позиций.
_ORDINARY_PAGE_SIZE = 10  # Лимит группы «Нужно купить» при просмотре.
_SELECTION_PAGE_SIZE = 5  # Предметный предел числа товаров на странице выбора.
_SELECTION_RESERVED_ROWS = 2  # Перелистывание и возврат занимают разные ряды.
_SELECTION_RESERVED_BUTTONS = 3  # Две страницы и возврат доступны одновременно.
_TITLE_PREVIEW = 90  # Оставляет место для текста команд при HTML-экранировании.
_BUTTON_LABEL_LIMIT = 40  # Ограничение длины подписи VK применяется для обоих каналов.
_MAX_SECTIONS = 4096  # Разделы давно не использованных чатов вытесняются из памяти.
_RECENT_NUMBER_MESSAGES = 128  # Повторное событие канала не меняет следующий товар.

_SHOPPING_GROUPS = (
    (ShoppingItemStatus.NEEDED, "Нужно купить"),
    (ShoppingItemStatus.BOUGHT, "Куплено"),
    (ShoppingItemStatus.UNKNOWN, "Покупки с неизвестным состоянием"),
)


class ShoppingSelection(StrEnum):
    """Различает действия при последовательном выборе товара."""

    BUY = "buy"  # Можно выбрать только покупку из группы «Нужно купить».
    DELETE = "delete"  # Можно удалить только покупку из «Нужно купить».


@dataclass(frozen=True, slots=True)
class ShoppingChoice:
    """Связывает ожидаемый номер с одной подтверждённой страницей выбора."""

    request_id: str
    selection: ShoppingSelection
    page: int
    numbers: tuple[tuple[int, int], ...]  # Видимые и постоянные номера.
    delivered: Event = field(default_factory=Event, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ShoppingPage:
    """Содержит видимые позиции и границы одной логической страницы."""

    number: int
    last: int
    visible: tuple[tuple[int, ShoppingItem], ...]


@dataclass(frozen=True, slots=True)
class ShoppingSection:
    """Связывает открытый раздел с ревизией режима и текущим членством."""

    revision: int
    family_id: int
    joined_at: datetime
    inline_commands: bool = False  # Команды обычного списка видны под сообщением.
    vk_menu: bool = False  # Вместо кнопок обычного списка используется нижнее меню.
    needed_visible: bool = field(default=False, compare=False)  # Доступность выбора покупок в меню.
    bought_visible: bool = field(default=False, compare=False)  # Доступность очистки купленного в меню.
    ordinary_page: int = field(default=1, compare=False)
    ordinary_last_page: int = field(default=1, compare=False)
    selection_open: bool = field(default=False, compare=False)
    add_request_id: str | None = field(default=None, compare=False)
    choice: ShoppingChoice | None = field(default=None, compare=False)
    number_message_ids: tuple[str, ...] = field(default=(), compare=False)
    # После переоткрытия прежнее сообщение не станет целевым даже при сбое новой отправки.
    action_key: str = field(default_factory=lambda: token_urlsafe(12), compare=False)

    def matches(self, family_id: int, joined_at: datetime) -> bool:
        """Проверяет, что раздел открыт для того же членства."""
        return self.family_id == family_id and self.joined_at == joined_at

    def record_number_message(self, message_id: str) -> ShoppingSection:
        """Учитывает принятое сообщение в пределах истории открытого списка."""
        return replace(self, number_message_ids=(*self.number_message_ids, message_id)[-_RECENT_NUMBER_MESSAGES:])

    def ordinary_view(self, items: tuple[ShoppingItem, ...], page: int) -> ShoppingSection:
        """Завершает выбор и сохраняет фактическую страницу обычного списка.

        Args:
            items: Снимок, отображённый на обычном экране.
            page: Запрошенная страница, начиная с 1.

        Returns:
            Состояние обычного экрана. В VK смена ключа закрывает прежний выбор.
        """
        key = token_urlsafe(12) if self.vk_menu and self.selection_open else self.action_key
        ordinary = shopping_page(items, page)
        return replace(self, selection_open=False, choice=None, add_request_id=None,
                       needed_visible=any(item.status is ShoppingItemStatus.NEEDED for item in items),
                       bought_visible=any(item.status is ShoppingItemStatus.BOUGHT for item in items),
                       ordinary_page=ordinary.number, ordinary_last_page=ordinary.last, action_key=key)


class ShoppingSections:
    """Запоминает открытые разделы отдельно для каждого профиля и чата."""

    def __init__(self) -> None:
        """Готовит ограниченный процессный реестр разделов."""
        self._opened: OrderedDict[tuple[int, ReplyAddress], ShoppingSection] = OrderedDict()
        self._lock = Lock()

    def open(self, app_user_id: int, address: ReplyAddress, section: ShoppingSection) -> None:
        """Запоминает раздел и вытесняет наименее недавно использованный чат."""
        key = app_user_id, address
        with self._lock:
            self._opened[key] = section
            self._opened.move_to_end(key)
            if len(self._opened) > _MAX_SECTIONS:
                self._opened.popitem(last=False)

    def current(self, app_user_id: int, address: ReplyAddress, revision: int) -> ShoppingSection | None:
        """Возвращает раздел только для проверенной ревизии режима чата."""
        key = app_user_id, address
        with self._lock:
            section = self._opened.get(key)
            if section is None or section.revision != revision:
                return None
            self._opened.move_to_end(key)
            return section

    def close(self, app_user_id: int, address: ReplyAddress) -> None:
        """Закрывает раздел для указанного профиля в одном чате."""
        with self._lock:
            self._opened.pop((app_user_id, address), None)

    def replace(
        self, app_user_id: int, address: ReplyAddress, current: ShoppingSection, section: ShoppingSection,
    ) -> bool:
        """Возвращает True, если действующую версию раздела удалось заменить."""
        key = app_user_id, address
        with self._lock:
            if self._opened.get(key) is not current:
                return False
            self._opened[key] = section
            return True


def parse_item_number(value: str) -> int | None:
    """Разбирает положительный номер в диапазоне счётчика списка."""
    if len(value) > 19 or not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if 0 < number <= MAX_ITEM_NUMBER else None


def shopping_notice(change: ItemChange, position: int) -> str | None:
    """Возвращает причину отказа для номера, который видел пользователь."""
    if change.status is ItemChangeStatus.POSITION_CHANGED:
        return "Нумерация изменилась. Выбери товар заново."
    descriptions = {
        ItemChangeStatus.ALREADY_BOUGHT: "уже куплен.",
        ItemChangeStatus.NOT_FOUND: "нет в списке.",
        ItemChangeStatus.UNKNOWN: "не изменён из-за неизвестного состояния.",
    }
    if change.status in descriptions:
        return f"Товар №{position} {descriptions[change.status]}"
    return None


def selection_page_size(layout: ActionLayoutLimits, *, text_page_size: int = _SELECTION_PAGE_SIZE) -> int:
    """Определяет размер выбора после резерва навигации, сохраняя текстовый путь.

    Args:
        layout: Вместимость клавиатуры, известная до построения текста.
        text_page_size: Размер страницы при недоступной клавиатуре.

    Returns:
        Количество позиций на странице выбора.
    """
    capacity = layout.selection_capacity(reserved_rows=_SELECTION_RESERVED_ROWS,
                                         reserved_buttons=_SELECTION_RESERVED_BUTTONS)
    return min(_SELECTION_PAGE_SIZE, capacity) if capacity else text_page_size


def choice_page_for_list(ordinary_page: int, page_size: int) -> int:
    """Возвращает страницу выбора, на которой видна первая позиция списка.

    Args:
        ordinary_page: Страница обычного списка, начиная с единицы.
        page_size: Вместимость страницы выбора.

    Returns:
        Номер страницы выбора, начиная с единицы.
    """
    return (ordinary_page - 1) * _ORDINARY_PAGE_SIZE // page_size + 1


def shopping_page(
    items: tuple[ShoppingItem, ...], page: int, *, selection_size: int | None = None,
) -> ShoppingPage:
    """Разбивает «Нужно купить» на страницы с нумерацией по всему списку.

    Args:
        items: Текущий список в порядке постоянных номеров.
        page: Запрошенная страница, начиная с 1.
        selection_size: Размер страницы выбора только из группы «Нужно купить».
            None использует размер обычного списка.

    Returns:
        Фактическая страница, число страниц и пары видимого номера с товаром.
        В обычном списке остальные группы целиком идут на последнюю страницу.
    """
    needed = tuple(enumerate((item for item in items if item.status is ShoppingItemStatus.NEEDED), 1))
    page_size = _ORDINARY_PAGE_SIZE if selection_size is None else selection_size
    last_page = max(1, (len(needed) + page_size - 1) // page_size)
    page = min(page, last_page)
    start = (page - 1) * page_size
    visible = needed[start:start + page_size]
    if selection_size is None and page == last_page:
        visible += tuple(
            pair
            for status, _ in _SHOPPING_GROUPS[1:]
            for pair in enumerate((item for item in items if item.status is status), 1)
        )
    return ShoppingPage(page, last_page, visible)


def shopping_section_actions(section: ShoppingSection) -> tuple[ReplyAction, ...]:
    """Возвращает доступные действия и переходы обычного списка покупок.

    Args:
        section: Состояние обычной страницы с доступными статусами списка.

    Returns:
        Действия в порядке показа. Выбор получает исходную страницу списка,
        чтобы определить страницу товаров с учётом вместимости клавиатуры.
    """
    page = section.ordinary_page
    actions = [ReplyAction("Добавить", ADD_COMMAND, scope=ActionScope.SECTION)]
    if section.needed_visible:
        for label, selection in (("Купить", ShoppingSelection.BUY), ("Удалить", ShoppingSelection.DELETE)):
            actions.append(ReplyAction(
                label, f"{SHOPPING_COMMAND} choose {selection} {page}", scope=ActionScope.SECTION,
                purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination(f"shopping-{selection}"),
            ))
    if section.bought_visible:
        actions.append(ReplyAction("Очистить купленное", CLEAR_BOUGHT_COMMAND, scope=ActionScope.SECTION))
    for label, destination in _shopping_page_links(page, section.ordinary_last_page):
        actions.append(ReplyAction(
            label, f"{SHOPPING_COMMAND} page {destination}", scope=ActionScope.SECTION,
            purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("shopping-list", destination),
        ))
    actions.append(ReplyAction(
        "Назад", f"{SHOPPING_COMMAND} home", scope=ActionScope.SECTION,
        purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("family-home"),
    ))
    return tuple(actions)


def _shopping_page_links(page: int, last_page: int) -> tuple[tuple[str, int], ...]:
    return tuple(
        (label, destination)
        for label, destination in (("Предыдущая страница", page - 1), ("Следующая страница", page + 1))
        if 1 <= destination <= last_page
    )


def shopping_list(
    items: tuple[ShoppingItem, ...], section: ShoppingSection, page: int = 1, *,
    update: bool = False, selection: ShoppingSelection | None = None,
    selection_page: ShoppingPage | None = None, choice: ShoppingChoice | None = None,
    notice: str | None = None,
) -> IncomingResponse:
    """Показывает страницу по группам статусов и кнопки переходов.

    Args:
        items: Текущий снимок списка с постоянными номерами.
        section: Открытый раздел, которому принадлежит сообщение со списком.
        page: Запрошенная страница, начиная с единицы.
        update: Когда True, обновляет уже доставленное сообщение.
        selection: Действие, для которого показаны кнопки товаров.
        selection_page: Тот же срез, из которого сформированы номера ожидаемого ввода.
        choice: Снимок видимых номеров, доступный после доставки.
        notice: Пояснение при отказе изменить выбранный товар.

    Returns:
        Ответ со списком и действиями. Длинный текст товара доступен
        отдельной обычной командой, а последняя страница может занять несколько сообщений.
    """
    groups = _SHOPPING_GROUPS[:1] if selection is not None else _SHOPPING_GROUPS
    snapshot = selection_page if selection_page is not None else shopping_page(items, page)
    page, last_page, visible = snapshot.number, snapshot.last, snapshot.visible
    empty_list = not items and selection is None
    parts: list[str | TextSpan] = ["Список покупок пуст." if empty_list else "Список покупок"]
    if last_page > 1:
        parts.append(f" · {page}/{last_page}")
    if selection is not None and not visible:
        parts.append("\n\nВ «Нужно купить» пока ничего нет.")
    elif not empty_list:
        for status, label in groups:
            group = tuple((position, item) for position, item in visible if item.status is status)
            if not group and status is not ShoppingItemStatus.NEEDED:
                continue
            parts.extend(("\n\n", label))
            if not group:
                parts.append("\nСписок пуст.")
            for position, item in group:
                title = item.name
                if len(title) > _TITLE_PREVIEW:
                    title = title[:_TITLE_PREVIEW] + "…"
                    parts.extend((f"\n{position}. {title} · ", code_span(f"{SHOPPING_COMMAND} item {item.number}")))
                else:
                    parts.append(f"\n{position}. {title}")

    actions: list[ReplyAction] = []
    if selection is not None:
        instruction = "Выбери, что купил" if selection is ShoppingSelection.BUY else "Выбери, что удалить"
        if visible:
            parts.append(f"\n\n{instruction}, или пришли номер товара. На выбор 10 минут.")
        for row, (position, item) in enumerate(visible):
            label = f"{position}. {item.name}"
            if len(label) > _BUTTON_LABEL_LIMIT:
                label = label[:_BUTTON_LABEL_LIMIT - 1] + "…"
            actions.append(_shopping_button(
                section, label, f"pick {page} {selection} {position} {item.number}",
                row=row if section.vk_menu else None,
            ))
        for label, destination in _shopping_page_links(page, last_page):
            actions.append(_shopping_button(
                section, label, f"show {destination} {selection}",
                row=len(visible) if section.vk_menu else None,
            ))
        actions.append(_shopping_button(
            section, "Назад", f"show {section.ordinary_page} page",
            row=len(visible) + 1 if section.vk_menu else None,
        ))
    elif not section.vk_menu:
        actions.extend(_legacy_inline_list_actions(section.ordinary_view(items, page)))
    if notice:
        parts.extend(("\n\n", notice))
    if section.vk_menu and selection is None:
        return IncomingResponse(compose_content(*parts), close_managed_views=True)
    unavailable = compose_content("Не удалось обновить список. Открой его снова через ", code_span(SHOPPING_COMMAND), ".")
    return IncomingResponse(
        compose_content(*parts), suggested_actions=tuple(actions),
        command_menu_delivery="preserve" if section.vk_menu else None,
        view=ResponseView(
            f"shopping:{section.action_key}", update=update, unavailable=unavailable if update else None,
            on_delivered=choice.delivered.set if choice is not None else None,
            cleanup_previous=True,
            allow_multiple_messages=selection is None,
        ),
    )


def _shopping_button(section: ShoppingSection, label: str, action: str, *, row: int | None = None) -> ReplyAction:
    return ReplyAction(label, f"{SHOPPING_COMMAND} click {section.action_key} {action}", row=row)


def _legacy_inline_list_actions(section: ShoppingSection) -> tuple[ReplyAction, ...]:
    # До подключения общего запроса список сохраняет текущий путь исполнения кнопок.
    page = section.ordinary_page
    actions = []
    for action in shopping_section_actions(section):
        match action.command.split():
            case ["/add"] if section.inline_commands:
                command = f"add {page}"
            case ["/family_clear_bought"]:
                command = f"clear {page}"
            case [_, "choose", selection, _] if section.inline_commands:
                command = f"choose {page} {selection}"
            case [_, "page", destination]:
                command = f"show {destination} page"
            case [_, "home"]:
                command = "home"
            case _:
                continue
        actions.append(_shopping_button(section, action.label, command))
    return tuple(actions)
