"""Сохраняет окна сводок и читает их состав из семейных таблиц SQLite."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from compadre_assistant.capabilities.family.data.shopping_dtos import ShoppingItemDto, shopping_item_from_dto
from compadre_assistant.capabilities.family.domain.shopping import ShoppingItem
from compadre_assistant.capabilities.family.domain.shopping_notifications import (
    ShoppingBatch, ShoppingBatchRepository, ShoppingBatchWindow, notification_items,
)


@dataclass(frozen=True, slots=True)
class ShoppingBatchDto:
    """Хранит идентификаторы и время вступления в представлении SQLite."""

    id: int
    family_id: int
    recipient_id: int
    joined_at: str


def _batch_from_dto(dto: ShoppingBatchDto) -> ShoppingBatch:
    return ShoppingBatch(dto.id, dto.family_id, dto.recipient_id, datetime.fromisoformat(dto.joined_at))


def append_shopping_batches(
    connection: sqlite3.Connection, family_id: int, author_id: int,
    first_number: int, last_number: int, now: datetime,
) -> None:
    """Присоединяет номера к подборкам в открытой транзакции добавления покупок.

    Args:
        connection: Соединение, в котором уже записаны товары.
        family_id: Семья автора.
        author_id: Профиль, которому собственные покупки не рассылаются.
        first_number: Первый номер нового пакета товаров.
        last_number: Последний номер того же пакета включительно.
        now: Время добавления товаров в UTC.
    """
    rows = connection.execute(
        """SELECT m.app_user_id, b.id, b.started_at, b.due_at FROM family_members m
           LEFT JOIN family_shopping_batches b ON b.id = (
               SELECT id FROM family_shopping_batches WHERE family_id = m.family_id
               AND recipient_id = m.app_user_id ORDER BY id DESC LIMIT 1)
           WHERE m.family_id = ? AND m.app_user_id <> ?""", (family_id, author_id),
    ).fetchall()
    for row in rows:
        window = None
        if row["id"] is not None:
            previous = ShoppingBatchWindow(datetime.fromisoformat(row["started_at"]), datetime.fromisoformat(row["due_at"]))
            window = previous.extend(now)
        if window is None:
            window = ShoppingBatchWindow.start(now)
            connection.execute(
                """INSERT INTO family_shopping_batches
                   (family_id, recipient_id, started_at, due_at, first_number, last_number)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (family_id, row["app_user_id"], now.isoformat(), window.due_at.isoformat(), first_number, last_number),
            )
        else:
            connection.execute(
                "UPDATE family_shopping_batches SET last_number = ?, due_at = ? WHERE id = ?",
                (last_number, window.due_at.isoformat(), row["id"]),
            )


class SQLiteShoppingBatchRepository(ShoppingBatchRepository):
    """Предоставляет отправителю ожидающие подборки и актуальные позиции."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Принимает соединение с актуальными миграциями."""
        self._connection = connection

    def due_batches(self, now: datetime) -> tuple[ShoppingBatch, ...]:
        """Читает подборки с наступившим сроком и действующим адресатом в порядке срока."""
        rows = self._connection.execute(
            """SELECT b.id, b.family_id, b.recipient_id, m.joined_at FROM family_shopping_batches b
               JOIN family_members m ON m.family_id = b.family_id AND m.app_user_id = b.recipient_id
               WHERE b.due_at <= ? ORDER BY b.due_at, b.id""", (now.astimezone(UTC).isoformat(),),
        )
        return tuple(_batch_from_dto(ShoppingBatchDto(**dict(row))) for row in rows)

    def current_items(self, batch: ShoppingBatch) -> tuple[ShoppingItem, ...]:
        """Проверяет все поля адреса и возвращает только актуальную часть подборки."""
        rows = self._connection.execute(
            """SELECT i.number, i.name, i.status, i.created_by_app_user_id, i.created_at,
                      i.bought_by_app_user_id, i.bought_at
               FROM family_shopping_batches b JOIN family_members m
                   ON m.family_id = b.family_id AND m.app_user_id = b.recipient_id
               JOIN family_shopping_items i ON i.family_id = b.family_id
                   AND i.number BETWEEN b.first_number AND b.last_number
               WHERE b.id = ? AND b.family_id = ? AND b.recipient_id = ? AND m.joined_at = ?""",
            (batch.id, batch.family_id, batch.recipient_id, batch.joined_at.isoformat()),
        )
        items = (shopping_item_from_dto(ShoppingItemDto(**dict(row))) for row in rows)
        return notification_items(items, batch.recipient_id)
