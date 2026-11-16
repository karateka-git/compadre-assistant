"""Хранит раздел покупок в контексте чата и показывает общий список."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from compadre_assistant.capabilities.family.domain.shopping import ShoppingItem, ShoppingItemStatus
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse, ReplyAction, ResponseView
from compadre_assistant.core.presentation.message_content import TextSpan, code_span, compose_content


SHOPPING_COMMAND = "/family_shopping"  # Вход в покупки и перемещение по списку.
_LIST_KEY = "shopping"  # Общее представление для просмотра и последовательного выбора.
_PAGE_SIZE = 5  # Оставляет место для групп, навигации и команд на одном сообщении.
_TITLE_PREVIEW = 100  # При HTML-экранировании пять превью всё ещё помещаются в Telegram.
_MAX_SECTIONS = 4096  # Разделы давно не использованных чатов вытесняются из памяти.


@dataclass(frozen=True, slots=True)
class ShoppingSection:
    """Связывает открытый раздел с ревизией режима и текущим членством."""

    revision: int
    family_id: int
    joined_at: datetime


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


def shopping_list(items: tuple[ShoppingItem, ...], page: int = 1, *, update: bool = False) -> IncomingResponse:
    """Показывает страницу по группам статусов и команды доступных переходов.

    Args:
        items: Текущий снимок списка с постоянными номерами.
        page: Запрошенная страница, начиная с единицы.
        update: Когда True, обновляет уже доставленное сообщение.

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
    for label, destination in (("Предыдущая страница", page - 1), ("Следующая страница", page + 1)):
        if 1 <= destination <= last_page:
            command = f"{SHOPPING_COMMAND} page {destination}"
            parts.extend(("\n", label, " · ", code_span(command)))
            actions.append(ReplyAction(label, command))
    home = f"{SHOPPING_COMMAND} home"
    parts.extend(("\n\nК семье · ", code_span(home)))
    actions.append(ReplyAction("К семье", home))
    unavailable = compose_content("Не удалось обновить список. Открой его снова через ", code_span(SHOPPING_COMMAND), ".")
    return IncomingResponse(
        compose_content(*parts), suggested_actions=tuple(actions),
        view=ResponseView(_LIST_KEY, update=update, unavailable=unavailable if update else None),
    )
