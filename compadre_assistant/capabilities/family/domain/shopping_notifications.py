"""Определяет срок и видимый состав семейной сводки для одного получателя."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from compadre_assistant.capabilities.family.domain.shopping import ShoppingItem, ShoppingItemStatus


_QUIET_PERIOD = timedelta(minutes=2)  # Ждём следующих позиций из той же серии добавлений.
_MAX_WAIT = timedelta(minutes=10)  # Продолжающийся ввод не должен отодвигать сводку дольше.


@dataclass(frozen=True, slots=True)
class ShoppingBatchWindow:
    """Окно от первого чужого добавления до срока отправки сводки."""

    started_at: datetime
    due_at: datetime

    @classmethod
    def start(cls, now: datetime) -> "ShoppingBatchWindow":
        """Открывает подборку в момент первого добавления для получателя."""
        return cls(now, now + _QUIET_PERIOD)

    def extend(self, now: datetime) -> "ShoppingBatchWindow | None":
        """Возвращает продлённое окно либо None, если нужна новая подборка.

        Args:
            now: Время следующего добавления для этого получателя.

        Returns:
            Обновлённое окно до наступления срока. В сам момент срока
            прежняя подборка уже закрыта, даже если доставка задержалась.
        """
        if now >= self.due_at:
            return None
        # Транзакции могут получить блокировку в ином порядке, чем запросили время.
        due_at = min(self.started_at + _MAX_WAIT, max(self.due_at, now + _QUIET_PERIOD))
        return ShoppingBatchWindow(self.started_at, due_at)


def notification_items(items: Iterable[ShoppingItem], recipient_id: int) -> tuple[ShoppingItem, ...]:
    """Возвращает ещё нужные чужие позиции подборки в порядке добавления.

    Args:
        items: Актуальные позиции из подборки, сформированной для получателя.
        recipient_id: Профиль, для которого готовится сообщение.

    Returns:
        Позиции по постоянному номеру в семье, включая одинаковые названия.
        Пустой результат означает, что отправка не нужна.
    """
    return tuple(sorted(
        (item for item in items
         if item.status == ShoppingItemStatus.NEEDED and item.created_by_app_user_id != recipient_id),
        key=lambda item: item.number,
    ))
