"""Хранит раздел покупок в контексте чата и показывает общий список."""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock

from compadre_assistant.capabilities.family.domain.shopping import (
    MAX_ITEM_NUMBER, ItemChange, ItemChangeStatus, ShoppingItem, ShoppingItemStatus,
)
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse, ReplyAction, ResponseView
from compadre_assistant.core.presentation.message_content import TextSpan, code_span, compose_content


SHOPPING_COMMAND = "/family_shopping"  # Вход в покупки и перемещение по списку.
ADD_COMMAND = "/add"  # Добавление текста из команды или следующего сообщения.
BUY_COMMAND = "/buy"  # Отметка покупок по номерам.
DELETE_COMMAND = "/delete"  # Удаление покупок по номерам.
CLEAR_BOUGHT_COMMAND = "/family_clear_bought"  # Очистка купленных позиций.
_LIST_KEY = "shopping"  # Общее представление для просмотра и последовательного выбора.
_PAGE_SIZE = 5  # Оставляет место для групп, навигации и команд на одном сообщении.
_TITLE_PREVIEW = 100  # При HTML-экранировании пять превью всё ещё помещаются в Telegram.
_MAX_SECTIONS = 4096  # Разделы давно не использованных чатов вытесняются из памяти.
_INLINE_NOTICES_LIMIT = 500  # Оставляет место в сообщении списка при HTML-экранировании названий.
_NOTICES_PER_PAGE = 25  # Отдельная страница результата помещается в сообщение обоих каналов.


@dataclass(frozen=True, slots=True)
class ShoppingSection:
    """Связывает открытый раздел с ревизией режима и текущим членством."""

    revision: int
    family_id: int
    joined_at: datetime
    add_request_id: str | None = field(default=None, compare=False)
    report: tuple[str, ...] = field(default=(), compare=False)


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
    ) -> None:
        """Сохраняет состояние открытого раздела, если этот раздел ещё действует."""
        key = app_user_id, address
        with self._lock:
            if self._opened.get(key) is current:
                self._opened[key] = section


def parse_item_number(value: str) -> int | None:
    """Разбирает положительный номер в диапазоне счётчика списка."""
    if len(value) > 19 or not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if 0 < number <= MAX_ITEM_NUMBER else None


def shopping_notices(changes: tuple[ItemChange, ...]) -> tuple[str, ...]:
    """Возвращает объяснения для номеров, оставшихся без изменения."""
    descriptions = {
        ItemChangeStatus.ALREADY_BOUGHT: "уже куплен.",
        ItemChangeStatus.NOT_FOUND: "нет в списке.",
        ItemChangeStatus.UNKNOWN: "не изменён из-за неизвестного состояния.",
    }
    return tuple(f"№{change.number} {descriptions[change.status]}"
                 for change in changes if change.status in descriptions)


def shopping_report(notices: tuple[str, ...], page: int) -> IncomingResponse:
    """Показывает страницу замечаний к последней длинной операции в чате."""
    last_page = max(1, (len(notices) + _NOTICES_PER_PAGE - 1) // _NOTICES_PER_PAGE)
    page = min(page, last_page)
    offset = (page - 1) * _NOTICES_PER_PAGE
    parts: list[str | TextSpan] = [f"Результат операции · {page}/{last_page}\n\n",
                                  "\n".join(notices[offset:offset + _NOTICES_PER_PAGE])]
    for label, destination in (("Предыдущая страница", page - 1), ("Следующая страница", page + 1)):
        if 1 <= destination <= last_page:
            parts.extend(("\n", label, " · ", code_span(f"{SHOPPING_COMMAND} result {destination}")))
    return IncomingResponse(compose_content(*parts))


def shopping_list(
    items: tuple[ShoppingItem, ...], page: int = 1, *, update: bool = False, notices: tuple[str, ...] = (),
) -> IncomingResponse:
    """Показывает страницу по группам статусов и команды доступных переходов.

    Args:
        items: Текущий снимок списка с постоянными номерами.
        page: Запрошенная страница, начиная с единицы.
        update: Когда True, обновляет уже доставленное сообщение.
        notices: Пояснения к номерам, которые не изменила команда.

    Returns:
        Ответ с ограниченным числом строк. Длинный текст товара доступен
        отдельной обычной командой.
    """
    groups = (
        (ShoppingItemStatus.NEEDED, "Нужно купить"),
        (ShoppingItemStatus.BOUGHT, "Купленные товары"),
        (ShoppingItemStatus.UNKNOWN, "Покупки с неизвестным состоянием"),
    )
    totals = {status: 0 for status, _ in groups}
    for item in items:
        totals[item.status] += 1
    ordered = tuple(item for status, _ in groups for item in items if item.status == status)
    last_page = max(1, (len(ordered) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, last_page)
    offset = (page - 1) * _PAGE_SIZE
    visible = ordered[offset:offset + _PAGE_SIZE]
    parts: list[str | TextSpan] = ["Список покупок"]
    if last_page > 1:
        parts.append(f" · {page}/{last_page}")
    for status, label in groups:
        if status != ShoppingItemStatus.NEEDED and not totals[status]:
            continue
        parts.extend(("\n\n", label))
        if not totals[status]:
            parts.append("\nСписок пуст." if not items else "\nВ этой группе пока ничего нет.")
        elif not any(item.status == status for item in visible):
            parts.append("\nСмотри другие страницы.")
        for item in visible:
            if item.status != status:
                continue
            title = item.name
            if len(title) > _TITLE_PREVIEW:
                title = title[:_TITLE_PREVIEW] + "…"
                parts.extend((f"\n{item.number}. {title} · ", code_span(f"{SHOPPING_COMMAND} item {item.number}")))
            else:
                parts.append(f"\n{item.number}. {title}")

    actions = []
    if totals[ShoppingItemStatus.BOUGHT]:
        parts.extend(("\n\nОчистить купленное · ", code_span(CLEAR_BOUGHT_COMMAND)))
    for label, destination in (("Предыдущая страница", page - 1), ("Следующая страница", page + 1)):
        if 1 <= destination <= last_page:
            command = f"{SHOPPING_COMMAND} page {destination}"
            parts.extend(("\n", label, " · ", code_span(command)))
            actions.append(ReplyAction(label, command))
    home = f"{SHOPPING_COMMAND} home"
    parts.extend(("\n\nК семье · ", code_span(home)))
    actions.append(ReplyAction("К семье", home))
    if notices:
        if needs_notice_pages(notices):
            parts.extend((f"\n\nБез изменений: {len(notices)}. Подробности · ",
                          code_span(f"{SHOPPING_COMMAND} result 1")))
        else:
            parts.extend(("\n\n", "\n".join(notices)))
    unavailable = compose_content("Не удалось обновить список. Открой его снова через ", code_span(SHOPPING_COMMAND), ".")
    return IncomingResponse(
        compose_content(*parts), suggested_actions=tuple(actions),
        view=ResponseView(_LIST_KEY, update=update, unavailable=unavailable if update else None),
    )


def needs_notice_pages(notices: tuple[str, ...]) -> bool:
    """Определяет, помещаются ли пояснения рядом с редактируемым списком."""
    return sum(len(notice) + 1 for notice in notices) > _INLINE_NOTICES_LIMIT
