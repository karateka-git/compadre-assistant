"""Переносит записи списка покупок из SQLite в предметную модель."""

from dataclasses import dataclass
from datetime import datetime

from compadre_assistant.capabilities.family.domain.shopping import ShoppingItem, ShoppingItemStatus


@dataclass(frozen=True, slots=True)
class ShoppingItemDto:
    """Хранит состояние и даты покупки в формате SQLite."""

    number: int
    name: str
    status: str
    created_by_app_user_id: int | None
    created_at: str
    bought_by_app_user_id: int | None
    bought_at: str | None


def shopping_item_from_dto(dto: ShoppingItemDto) -> ShoppingItem:
    """Читает даты и сохраняет незнакомый статус как UNKNOWN."""
    try:
        status = ShoppingItemStatus(dto.status)
    except ValueError:
        status = ShoppingItemStatus.UNKNOWN
    return ShoppingItem(
        dto.number, dto.name, status,
        dto.created_by_app_user_id, datetime.fromisoformat(dto.created_at),
        dto.bought_by_app_user_id, datetime.fromisoformat(dto.bought_at) if dto.bought_at is not None else None,
    )
