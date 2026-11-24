"""Транзакции покупки и семейного часового пояса в SQLite."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.expenses import ExpenseAccess, ExpenseCard, ExpensePurchase, ExpenseRepository, InvalidExpense
from ..domain.membership import FamilyAccessEnded
from .expense_dtos import ExpenseCardDto, ExpenseItemDto
from ..domain.expense_reports import ExpensePeriod, ExpenseSummary, ProductSelection, ReportPage
from .expense_report_queries import query_rows


class SQLiteExpenseRepository(ExpenseRepository):
    """Проверяет членство и читает или изменяет данные в той же транзакции."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Принимает соединение, открытое общей фабрикой SQLite."""
        self._db = connection

    @contextmanager
    def _access(self, actor: ExpenseAccess, *, write: bool = False) -> Iterator[str]:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            row = self._db.execute(
                """SELECT f.expense_timezone, m.joined_at
                   FROM families f JOIN family_members m ON m.family_id = f.id
                   WHERE f.id = ? AND m.app_user_id = ?""", (actor.family_id, actor.app_user_id),
            ).fetchone()
            if row is None or datetime.fromisoformat(row["joined_at"]) != actor.joined_at:
                raise FamilyAccessEnded("Доступ к расходам семьи завершён")
            yield row["expense_timezone"]

    def summary(self, access: ExpenseAccess, period: ExpensePeriod) -> ExpenseSummary:
        """Читает все категории и знаковую разницу чеков одним запросом."""
        with self._access(access):
            return ExpenseSummary(query_rows(self._db, access.family_id, period, group="category").rows)

    def products(self, access: ExpenseAccess, period: ExpensePeriod, category_id: int | None,
                 offset: int, limit: int) -> ReportPage:
        """Группирует товары выбранной категории внутри семейного периода."""
        with self._access(access):
            return query_rows(self._db, access.family_id, period, group="product",
                              category_id=category_id, offset=offset, limit=limit)

    def search(self, access: ExpenseAccess, period: ExpensePeriod, query: str,
               offset: int, limit: int) -> ReportPage:
        """Ищет подстроку русского или иного названия без учёта регистра."""
        with self._access(access):
            return query_rows(self._db, access.family_id, period, group="product",
                              search=query, offset=offset, limit=limit)

    def history(self, access: ExpenseAccess, period: ExpensePeriod, offset: int, limit: int,
                product: ProductSelection | None = None) -> ReportPage:
        """Выдаёт покупки или вклад выбранного товара в каждую покупку."""
        with self._access(access):
            return query_rows(self._db, access.family_id, period, group="purchase",
                              product=product, offset=offset, limit=limit)

    def timezone(self, access: ExpenseAccess) -> str:
        """Читает текущий пояс при действующем членстве."""
        with self._access(access) as zone:
            return zone

    def set_timezone(self, access: ExpenseAccess, zone: str) -> None:
        """Проверяет название IANA и сохраняет его для всей семьи."""
        with self._access(access, write=True):
            try:
                ZoneInfo(zone)
            except (ValueError, ZoneInfoNotFoundError) as error:
                raise InvalidExpense("Часовой пояс не найден") from error
            self._db.execute("UPDATE families SET expense_timezone = ? WHERE id = ?", (zone, access.family_id))

    def save(self, access: ExpenseAccess, operation_id: str, revision: int, purchase: ExpensePurchase) -> int:
        """Фиксирует согласованные строки и справочники одной транзакцией.

        Args:
            access: Проверяемое членство автора.
            operation_id: Общий ключ всех редакций одного черновика.
            revision: Номер предъявленной редакции.
            purchase: Значения, принятые пользователем на экране проверки.

        Returns:
            ID покупки, в том числе при повторе принятой редакции.

        Raises:
            InvalidExpense: Другая редакция уже принята или покупка некорректна.
            FamilyAccessEnded: Членство автора перестало действовать.
        """
        with self._access(access, write=True):
            found = self._db.execute(
                """SELECT id, author_id, revision FROM family_expenses
                   WHERE family_id = ? AND operation_id = ?""", (access.family_id, operation_id),
            ).fetchone()
            if found is not None:
                if found["revision"] != revision or found["author_id"] != access.app_user_id:
                    raise InvalidExpense("Подтверждение устарело")
                return found["id"]
            items = purchase.reconciled_items()
            cursor = self._db.execute(
                """INSERT INTO family_expenses
                   (family_id, author_id, operation_id, revision, purchase_date, created_at, source, description, receipt_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (access.family_id, access.app_user_id, operation_id, revision, purchase.purchase_date.isoformat(),
                 datetime.now(UTC).isoformat(), purchase.source.value, purchase.description, purchase.receipt_total),
            )
            expense_id = cursor.lastrowid
            assert expense_id is not None
            for position, item in enumerate(items, 1):
                dto = ExpenseItemDto.from_domain(item)
                category = self._catalog_id("family_expense_categories", access.family_id, dto.category)
                product = self._catalog_id("family_expense_products", access.family_id, dto.product)
                self._db.execute(
                    """INSERT INTO family_expense_items
                       (family_id, expense_id, position, category_id, product_id, amount, quantity, unit,
                        unit_price, package_size, amount_before_discount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (access.family_id, expense_id, position, category, product, dto.amount,
                     dto.quantity, dto.unit, dto.unit_price, dto.package_size, dto.amount_before_discount),
                )
            return expense_id

    def _catalog_id(self, table: str, family_id: int, name: str | None) -> int | None:
        if name is None:
            return None
        title = " ".join(name.split())
        key = title.casefold()
        # Имена таблиц задаёт только save; ключи Unicode обрабатываются одинаково для обоих справочников.
        self._db.execute(f"INSERT INTO {table} (family_id, name, name_key) VALUES (?, ?, ?) "
                         "ON CONFLICT(family_id, name_key) DO NOTHING", (family_id, title, key))
        return self._db.execute(f"SELECT id FROM {table} WHERE family_id = ? AND name_key = ?",
                                (family_id, key)).fetchone()[0]

    def card(self, access: ExpenseAccess, expense_id: int) -> ExpenseCard | None:
        """Читает карточку и упорядоченные строки из одного снимка БД."""
        with self._access(access):
            row = self._db.execute(
                """SELECT id, author_id, purchase_date, source, description, receipt_total FROM family_expenses
                   WHERE family_id = ? AND id = ?""", (access.family_id, expense_id),
            ).fetchone()
            if row is None:
                return None
            lines = self._db.execute(
                """SELECT c.name AS category, p.name AS product, i.amount, i.quantity, i.unit, i.unit_price,
                          i.package_size, i.amount_before_discount
                   FROM family_expense_items i
                   LEFT JOIN family_expense_categories c ON i.category_id = c.id AND i.family_id = c.family_id
                   LEFT JOIN family_expense_products p ON i.product_id = p.id AND i.family_id = p.family_id
                   WHERE i.family_id = ? AND i.expense_id = ? ORDER BY i.position""", (access.family_id, expense_id),
            )
            return ExpenseCardDto(**row).to_domain(tuple(ExpenseItemDto(**line).to_domain() for line in lines))
