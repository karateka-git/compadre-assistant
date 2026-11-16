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
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse, ReplyAction, ResponseView
from compadre_assistant.core.presentation.message_content import TextSpan, code_span, compose_content


SHOPPING_COMMAND = "/family_shopping"  # Вход в покупки и перемещение по списку.
ADD_COMMAND = "/add"  # Добавление текста из команды или следующего сообщения.
BUY_COMMAND = "/buy"  # Открывает выбор покупки для отметки.
DELETE_COMMAND = "/delete"  # Открывает выбор покупки для удаления.
CLEAR_BOUGHT_COMMAND = "/family_clear_bought"  # Очистка купленных позиций.
_PAGE_SIZE = 5  # Оставляет место для групп, навигации и команд на одном сообщении.
_TITLE_PREVIEW = 90  # Оставляет место для текста команд при HTML-экранировании.
_BUTTON_LABEL_LIMIT = 40  # Ограничение длины подписи VK применяется для обоих каналов.
_MAX_SECTIONS = 4096  # Разделы давно не использованных чатов вытесняются из памяти.
_RECENT_NUMBER_MESSAGES = 128  # Повторное событие канала не меняет следующий товар.

_SHOPPING_GROUPS = (
    (ShoppingItemStatus.NEEDED, "Нужно купить"),
    (ShoppingItemStatus.BOUGHT, "Купленные товары"),
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
class ShoppingSection:
    """Связывает открытый раздел с ревизией режима и текущим членством."""

    revision: int
    family_id: int
    joined_at: datetime
    inline_commands: bool = False  # Команды обычного списка видны под сообщением.
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


def shopping_page(
    items: tuple[ShoppingItem, ...], page: int, *, selecting: bool,
) -> tuple[int, int, tuple[tuple[int, ShoppingItem], ...]]:
    """Нумерует группы и возвращает запрошенный срез.

    Args:
        items: Текущий список в порядке постоянных номеров.
        page: Запрошенная страница, начиная с 1.
        selecting: Ограничить срез группой «Нужно купить».

    Returns:
        Фактическая страница, число страниц и пары видимого номера с товаром.
    """
    groups = _SHOPPING_GROUPS[:1] if selecting else _SHOPPING_GROUPS
    numbered = tuple(
        (position, item)
        for status, _ in groups
        for position, item in enumerate((item for item in items if item.status is status), 1)
    )
    last_page = max(1, (len(numbered) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, last_page)
    start = (page - 1) * _PAGE_SIZE
    return page, last_page, numbered[start:start + _PAGE_SIZE]


def shopping_list(
    items: tuple[ShoppingItem, ...], section: ShoppingSection, page: int = 1, *,
    update: bool = False, selection: ShoppingSelection | None = None,
    choice: ShoppingChoice | None = None, notice: str | None = None,
) -> IncomingResponse:
    """Показывает страницу по группам статусов и команды доступных переходов.

    Args:
        items: Текущий снимок списка с постоянными номерами.
        section: Открытый раздел, которому принадлежит сообщение со списком.
        page: Запрошенная страница, начиная с единицы.
        update: Когда True, обновляет уже доставленное сообщение.
        selection: Действие, для которого показаны кнопки товаров.
        choice: Снимок видимых номеров, доступный после доставки.
        notice: Пояснение при отказе изменить выбранный товар.

    Returns:
        Ответ с ограниченным числом строк. Длинный текст товара доступен
        отдельной обычной командой.
    """
    groups = _SHOPPING_GROUPS[:1] if selection is not None else _SHOPPING_GROUPS
    totals = {status: 0 for status, _ in _SHOPPING_GROUPS}
    for item in items:
        totals[item.status] += 1
    page, last_page, visible = shopping_page(items, page, selecting=selection is not None)
    parts: list[str | TextSpan] = ["Список покупок"]
    if last_page > 1:
        parts.append(f" · {page}/{last_page}")
    for status, label in groups:
        if status != ShoppingItemStatus.NEEDED and not totals[status]:
            continue
        parts.extend(("\n\n", label))
        if not totals[status]:
            parts.append("\nСписок пуст." if not items else "\nВ этой группе пока ничего нет.")
        elif not any(item.status == status for _, item in visible):
            parts.append("\nСмотри другие страницы.")
        for position, item in visible:
            if item.status != status:
                continue
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
            example = visible[1][0] if len(visible) > 1 else visible[0][0]
            parts.extend((f"\n\n{instruction}, или отправь номер следующим сообщением. Например, ",
                          code_span(str(example)), ". Выбор действует 10 минут. Для отмены введи ",
                          code_span("/cancel"), "."))
        else:
            parts.extend(("\n\nВ «Нужно купить» пока ничего нет. Добавь покупку через ", code_span(ADD_COMMAND), "."))
        for position, item in visible:
            label = f"{position}. {item.name}"
            if len(label) > _BUTTON_LABEL_LIMIT:
                label = label[:_BUTTON_LABEL_LIMIT - 1] + "…"
            actions.append(_shopping_button(section, label, f"pick {page} {selection} {position} {item.number}"))
    else:
        if section.inline_commands:
            parts.extend(("\n\nДобавить · ", code_span(ADD_COMMAND)))
            actions.append(_shopping_button(section, "Добавить", f"add {page}"))
            if totals[ShoppingItemStatus.NEEDED]:
                for label, command, view in (
                    ("Купить", BUY_COMMAND, ShoppingSelection.BUY),
                    ("Удалить", DELETE_COMMAND, ShoppingSelection.DELETE),
                ):
                    parts.extend((f"\n{label} · ", code_span(command)))
                    actions.append(_shopping_button(section, label, f"show {page} {view}"))
        if totals[ShoppingItemStatus.BOUGHT]:
            parts.extend(("\n\nОчистить купленное · ", code_span(CLEAR_BOUGHT_COMMAND)))
            actions.append(_shopping_button(section, "Очистить купленное", f"clear {page}"))

    view_name = str(selection) if selection is not None else "page"
    for label, destination in (("Предыдущая страница", page - 1), ("Следующая страница", page + 1)):
        if 1 <= destination <= last_page:
            command = f"{SHOPPING_COMMAND} {view_name} {destination}"
            parts.extend(("\n", label, " · ", code_span(command)))
            actions.append(_shopping_button(section, label, f"show {destination} {view_name}"))
    if selection is None:
        parts.extend(("\n\nНазад · ", code_span(f"{SHOPPING_COMMAND} home")))
        actions.append(_shopping_button(section, "Назад", "home"))
    else:
        parts.extend(("\n\nНазад · ", code_span(f"{SHOPPING_COMMAND} back")))
        actions.append(_shopping_button(section, "Назад", f"show {page} page"))
    if notice:
        parts.extend(("\n\n", notice))
    unavailable = compose_content("Не удалось обновить список. Открой его снова через ", code_span(SHOPPING_COMMAND), ".")
    return IncomingResponse(
        compose_content(*parts), suggested_actions=tuple(actions),
        view=ResponseView(
            f"shopping:{section.action_key}", update=update, unavailable=unavailable if update else None,
            on_delivered=choice.delivered.set if choice is not None else None,
        ),
    )


def _shopping_button(section: ShoppingSection, label: str, action: str) -> ReplyAction:
    return ReplyAction(label, f"{SHOPPING_COMMAND} click {section.action_key} {action}")
