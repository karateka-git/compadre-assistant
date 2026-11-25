"""Транзакции покупки и семейного часового пояса в SQLite."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.expenses import ExpenseAccess, ExpenseCard, ExpenseCategoryUnavailable, ExpensePurchase, ExpenseRepository, InvalidExpense
from ..domain.expense_categories import ExpenseCategory, InvalidCategory, StaleCategory, category_name
from ..domain.membership import FamilyAccessEnded
from .expense_dtos import ExpenseCardDto, ExpenseCategoryDto, ExpenseItemDto
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

    def categories(self, access: ExpenseAccess, *, hidden: bool = False) -> tuple[ExpenseCategory, ...]:
        """Читает видимую или скрытую часть каталога в порядке добавления."""
        with self._access(access):
            rows = self._db.execute("SELECT id, name, hidden, revision FROM family_expense_categories "
                                    "WHERE family_id=? AND hidden=? ORDER BY id", (access.family_id, hidden))
            return tuple(ExpenseCategoryDto(**row).to_domain() for row in rows)

    def create_category(self, access: ExpenseAccess, name: str) -> ExpenseCategory:
        """Создаёт категорию внутри семьи с защитой от дубля и возврата скрытой категории."""
        with self._access(access, write=True):
            return self._create_category(access.family_id, name)

    def _create_category(self, family_id: int, name: str) -> ExpenseCategory:
        title = category_name(name)
        key = title.casefold()
        self._db.execute("INSERT INTO family_expense_categories (family_id,name,name_key) VALUES (?,?,?) "
                         "ON CONFLICT(family_id,name_key) DO NOTHING", (family_id, title, key))
        row = self._db.execute("SELECT id, name, hidden, revision FROM family_expense_categories "
                               "WHERE family_id=? AND name_key=?", (family_id, key)).fetchone()
        category = ExpenseCategoryDto(**row).to_domain()
        if category.hidden:
            raise InvalidCategory("Категория уже есть в скрытых. Её можно вернуть в настройках.")
        return category

    def _current_category(self, family_id: int, category: ExpenseCategory) -> ExpenseCategory:
        row = self._db.execute("SELECT id, name, hidden, revision FROM family_expense_categories "
                               "WHERE family_id=? AND id=? AND revision=?",
                               (family_id, category.id, category.revision)).fetchone()
        if row is None:
            raise StaleCategory("Категорию уже изменили. Выбери её заново.")
        return ExpenseCategoryDto(**row).to_domain()

    def rename_category(self, access: ExpenseAccess, category: ExpenseCategory, name: str) -> ExpenseCategory:
        """Обновляет одно название, сохраняя ссылки в чеках; конфликт откатывает операцию."""
        with self._access(access, write=True):
            current = self._current_category(access.family_id, category)
            title = category_name(name)
            if current.name == title:
                return current
            if current.is_fallback:
                raise InvalidCategory("«Прочее» используется при распознавании и сохраняет своё название.")
            try:
                row = self._db.execute("UPDATE family_expense_categories SET name=?, name_key=?, revision=revision+1 "
                                       "WHERE family_id=? AND id=? RETURNING id, name, hidden, revision",
                                       (title, title.casefold(), access.family_id, current.id)).fetchone()
            except sqlite3.IntegrityError as error:
                raise InvalidCategory("В семье уже есть категория с таким названием.") from error
            return ExpenseCategoryDto(**row).to_domain()

    def set_category_hidden(self, access: ExpenseAccess, category: ExpenseCategory, hidden: bool) -> ExpenseCategory:
        """Меняет видимость текущей редакции категории, сохраняя исторические покупки."""
        with self._access(access, write=True):
            current = self._current_category(access.family_id, category)
            if hidden == current.hidden:
                return current
            if current.is_fallback:
                raise InvalidCategory("«Прочее» всегда доступно при вводе расходов.")
            row = self._db.execute("UPDATE family_expense_categories SET hidden=?, revision=revision+1 "
                                   "WHERE family_id=? AND id=? RETURNING id, name, hidden, revision",
                                   (hidden, access.family_id, current.id)).fetchone()
            return ExpenseCategoryDto(**row).to_domain()

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
                category = item.category_id
                if category is not None:
                    if self._db.execute("SELECT 1 FROM family_expense_categories WHERE id=? AND family_id=? AND hidden=0",
                                        (category, access.family_id)).fetchone() is None:
                        raise ExpenseCategoryUnavailable(position)
                elif dto.category is not None:
                    try:
                        category = self._create_category(access.family_id, dto.category).id
                    except InvalidCategory as error:
                        raise ExpenseCategoryUnavailable(position) from error
                product = self._product_id(access.family_id, dto.product)
                self._db.execute(
                    """INSERT INTO family_expense_items
                       (family_id, expense_id, position, category_id, product_id, amount, quantity, unit,
                        unit_price, package_size, amount_before_discount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (access.family_id, expense_id, position, category, product, dto.amount,
                     dto.quantity, dto.unit, dto.unit_price, dto.package_size, dto.amount_before_discount),
                )
            return expense_id

    def _product_id(self, family_id: int, name: str | None) -> int | None:
        if name is None:
            return None
        title = " ".join(name.split())
        key = title.casefold()
        self._db.execute("INSERT INTO family_expense_products (family_id, name, name_key) VALUES (?, ?, ?) "
                         "ON CONFLICT(family_id, name_key) DO NOTHING", (family_id, title, key))
        return self._db.execute("SELECT id FROM family_expense_products WHERE family_id = ? AND name_key = ?",
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
