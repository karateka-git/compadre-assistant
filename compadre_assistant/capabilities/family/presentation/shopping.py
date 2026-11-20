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
    ActionLayoutLimits, ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse, ReplyAction, ResponseView
from compadre_assistant.core.presentation.message_content import TextSpan, compose_content, text_content


SHOPPING_COMMAND = "/family_shopping"  # Вход в покупки и перемещение по списку.
ADD_COMMAND = "/add"  # Добавление текста из команды или следующего сообщения.
BUY_COMMAND = "/buy"  # Открывает выбор покупки для отметки.
DELETE_COMMAND = "/delete"  # Открывает выбор покупки для удаления.
CLEAR_BOUGHT_COMMAND = "/family_clear_bought"  # Очистка купленных позиций.
_ORDINARY_PAGE_SIZE = 10  # Лимит группы «Нужно купить» при просмотре.
_SELECTION_PAGE_SIZE = 5  # Предметный предел числа товаров на странице выбора.
_SELECTION_RESERVED_ROWS = 2  # Перелистывание и возврат занимают разные ряды.
_SELECTION_RESERVED_BUTTONS = 3  # Две страницы и возврат доступны одновременно.
_TITLE_PREVIEW = 90  # Предел длины названия перед тремя точками в тексте списка.
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
    needed_visible: bool = field(default=False, compare=False)  # Доступность выбора покупок в меню.
    bought_visible: bool = field(default=False, compare=False)  # Доступность очистки купленного в меню.
    ordinary_page: int = field(default=1, compare=False)
    ordinary_last_page: int = field(default=1, compare=False)
    selection_shown: Event = field(default_factory=Event, compare=False, repr=False)
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
            Состояние обычного экрана с актуальными группами и страницами.
        """
        ordinary = shopping_page(items, page)
        return replace(self, choice=None, add_request_id=None,
                       needed_visible=any(item.status is ShoppingItemStatus.NEEDED for item in items),
                       bought_visible=any(item.status is ShoppingItemStatus.BOUGHT for item in items),
                       ordinary_page=ordinary.number, ordinary_last_page=ordinary.last)


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

    def current(
        self, app_user_id: int, address: ReplyAddress, revision: int, *, touch: bool = True,
    ) -> ShoppingSection | None:
        """Читает раздел проверенной ревизии, при touch=False сохраняя порядок реестра."""
        key = app_user_id, address
        with self._lock:
            section = self._opened.get(key)
            if section is None or section.revision != revision:
                return None
            if touch:
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


def selection_page_size(layout: ActionLayoutLimits) -> int:
    """Определяет размер выбора после резерва навигации, сохраняя текстовый путь.

    Args:
        layout: Вместимость клавиатуры, известная до построения текста.

    Returns:
        Количество позиций на странице выбора.
    """
    reserved_rows = _SELECTION_RESERVED_ROWS + (layout.max_columns == 1)
    capacity = layout.selection_capacity(reserved_rows=reserved_rows,
                                         reserved_buttons=_SELECTION_RESERVED_BUTTONS)
    return min(_SELECTION_PAGE_SIZE, capacity) if capacity else _SELECTION_PAGE_SIZE


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


def shopping_section_actions(section: ShoppingSection, *, from_selection: bool = False) -> tuple[ReplyAction, ...]:
    """Возвращает доступные действия и переходы обычного списка покупок.

    Args:
        section: Состояние обычной страницы с доступными статусами списка.
        from_selection: Действия раздела доступны рядом с уже показанным выбором.

    Returns:
        Действия в порядке показа. Выбор получает исходную страницу списка,
        чтобы определить страницу товаров с учётом вместимости клавиатуры.
    """
    page = section.ordinary_page
    command = f"{SHOPPING_COMMAND} section {section.action_key}"
    actions = [ReplyAction("Добавить", f"{command} add", scope=ActionScope.SECTION)]
    if section.needed_visible:
        for label, selection in (("Купить", ShoppingSelection.BUY), ("Удалить", ShoppingSelection.DELETE)):
            actions.append(ReplyAction(
                label, f"{command} choose {selection} {page}", scope=ActionScope.SECTION,
                purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination(f"shopping-{selection}"),
            ))
    if section.bought_visible:
        actions.append(ReplyAction("Очистить купленное", f"{command} clear", scope=ActionScope.SECTION))
    for label, destination in _shopping_page_links(page, section.ordinary_last_page):
        actions.append(ReplyAction(
            label, f"{command} page {destination}", scope=ActionScope.SECTION,
            purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("shopping-list", destination),
        ))
    back_command, back_destination = (
        ("back", ScreenDestination("shopping-list", page)) if from_selection else
        ("home", ScreenDestination("family-home"))
    )
    actions.append(ReplyAction("Назад", f"{command} {back_command}", scope=ActionScope.SECTION,
                               purpose=ActionPurpose.NAVIGATION, destination=back_destination))
    return tuple(actions)


def _shopping_page_links(page: int, last_page: int) -> tuple[tuple[str, int], ...]:
    return tuple(
        (label, destination)
        for label, destination in (("Предыдущая страница", page - 1), ("Следующая страница", page + 1))
        if 1 <= destination <= last_page
    )


def shopping_list(
    items: tuple[ShoppingItem, ...], section: ShoppingSection, *,
    update: bool = False, selection: ShoppingSelection | None = None,
    selection_page: ShoppingPage | None = None, choice: ShoppingChoice | None = None,
    notice: str | None = None, layout: ActionLayoutLimits = ActionLayoutLimits(),
) -> IncomingResponse:
    """Показывает страницу по группам статусов и кнопки переходов.

    Args:
        items: Текущий снимок списка с постоянными номерами.
        section: Открытый раздел, которому принадлежит сообщение со списком.
        update: Когда True, обновляет уже доставленное сообщение.
        selection: Действие, для которого показаны кнопки товаров.
        selection_page: Тот же срез, из которого сформированы номера ожидаемого ввода.
        choice: Снимок видимых номеров, доступный после доставки.
        notice: Пояснение при отказе изменить выбранный товар.
        layout: Вместимость кнопок и подписей в сообщении выбора.

    Returns:
        Ответ со списком и действиями. Длинные названия в списке сокращаются,
        а последняя страница может занять несколько сообщений.
    """
    groups = _SHOPPING_GROUPS[:1] if selection is not None else _SHOPPING_GROUPS
    snapshot = selection_page if selection_page is not None else shopping_page(items, section.ordinary_page)
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
                    title = title[:_TITLE_PREVIEW] + "..."
                parts.append(f"\n{position}. {title}")

    if selection is None:
        actions = shopping_section_actions(section)
    else:
        instruction = "Выбери, что купил" if selection is ShoppingSelection.BUY else "Выбери, что удалить"
        if visible:
            parts.append(f"\n\n{instruction}, или пришли номер товара. На выбор 10 минут.")
        actions = _selection_actions(section, snapshot, selection, layout)
    if notice:
        parts.extend(("\n\n", notice))

    def on_delivered() -> None:
        if selection is None:
            section.selection_shown.clear()
        else:
            section.selection_shown.set()
            if choice is not None:
                choice.delivered.set()

    unavailable = text_content("Не удалось обновить список. Открой его заново.")
    return IncomingResponse(
        compose_content(*parts), suggested_actions=actions,
        command_menu_delivery=SectionMenuDelivery.PRESERVE if selection is not None else SectionMenuDelivery.RECONCILE,
        view=ResponseView(
            f"shopping:{section.action_key}", update=update, unavailable=unavailable if update else None,
            on_delivered=on_delivered,
            cleanup_previous=True,
            allow_multiple_messages=selection is None,
            action_scope=ActionScope.SCREEN if selection is not None else ActionScope.SECTION,
        ),
    )


def _selection_actions(
    section: ShoppingSection, page: ShoppingPage, selection: ShoppingSelection, layout: ActionLayoutLimits,
) -> tuple[ReplyAction, ...]:
    command = f"{SHOPPING_COMMAND} click {section.action_key}"
    label_limit = layout.max_label_length or _TITLE_PREVIEW
    actions = []
    for row, (position, item) in enumerate(page.visible):
        label = f"{position}. {item.name}"
        if len(label) > label_limit:
            label = label[:label_limit - 1] + "…"
        actions.append(ReplyAction(
            label, f"{command} pick {page.number} {selection} {position} {item.number}", row=row,
            purpose=ActionPurpose.SELECTION,
        ))
    links = _shopping_page_links(page.number, page.last)
    row = len(page.visible)
    for label, destination in links:
        actions.append(ReplyAction(
            label, f"{command} show {destination} {selection}", row=row,
            purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination(f"shopping-{selection}", destination),
        ))
        if layout.max_columns == 1:
            row += 1
    if links and layout.max_columns != 1:
        row += 1
    actions.append(ReplyAction(
        "Назад", f"{command} show {section.ordinary_page} page", row=row,
        purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("shopping-list", section.ordinary_page),
    ))
    return tuple(actions)
