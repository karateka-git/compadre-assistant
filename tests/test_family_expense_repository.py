"""Контроль записи покупок и настроек семьи на временной SQLite."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from compadre_assistant.capabilities.family.data.sqlite_expenses import SQLiteExpenseRepository
from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.domain.expenses import (
    ExpenseAccess, ExpenseItem, ExpensePurchase, ExpenseSource, InvalidExpense,
)
from compadre_assistant.capabilities.family.domain.membership import FamilyAccessEnded
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations


class FamilyExpenseRepositoryTest(unittest.TestCase):
    """Проверяет действующее членство, состав покупки и защиту от повторов."""

    def setUp(self) -> None:
        """Готовит две семьи на отдельной файловой БД для каждого примера."""
        self.path = Path(self.enterContext(TemporaryDirectory())) / "expenses.db"
        self.db = self.enterContext(connect_database(self.path))
        apply_migrations(self.db)
        with self.db:
            self.db.execute("INSERT INTO app_users (id) VALUES (1), (2), (3)")
        self.members = SQLiteFamilyRepository(self.db)
        now = datetime.now(UTC)
        home = self.members.create(1, "Семья", now).id
        other = self.members.create(3, "Другая", now).id
        with self.db:
            self.db.execute("INSERT INTO family_members VALUES (?, 2, ?)", (home, now.isoformat()))
        self.owner = ExpenseAccess(home, 1, now)
        self.author = ExpenseAccess(home, 2, now)
        self.other = ExpenseAccess(other, 3, now)
        self.repo = SQLiteExpenseRepository(self.db)
        self.purchase = ExpensePurchase(date(2026, 11, 23), ExpenseSource.PHOTO, (
            ExpenseItem("Продукты", "Яблоки", 7500, Decimal("0.375"), "кг", 20000),
            ExpenseItem("Питомцы", None, 10000),
        ), "Лента", 17000)

    def test_card_shares_items_only_inside_family_and_preserves_date(self) -> None:
        """Карточка содержит вес, категорию без товара и единственную разницу."""
        key = self.repo.save(self.author, "a", 1, self.purchase)
        card = self.repo.card(self.owner, key)
        self.assertEqual(card.items, (*self.purchase.items, ExpenseItem(None, None, -500)))
        self.assertEqual((card.purchase_date, card.source, card.receipt_total, card.author_id, card.description),
                         (self.purchase.purchase_date, ExpenseSource.PHOTO, 17000, 2, "Лента"))
        self.assertIsNone(self.repo.card(self.other, key))
        second = replace(self.purchase, receipt_total=18000, items=(
            replace(self.purchase.items[0], category="продукты", product="ЯБЛОКИ"), self.purchase.items[1],
        ))
        key2 = self.repo.save(self.author, "b", 1, second)
        self.assertEqual(self.repo.card(self.author, key2).items, (*self.purchase.items, ExpenseItem(None, None, 500)))
        with self.db:
            self.db.execute("UPDATE family_expenses SET source='future' WHERE id=?", (key,))
        self.assertEqual(self.repo.card(self.owner, key).source, ExpenseSource.UNKNOWN)

    def test_zone_and_all_operations_require_current_membership(self) -> None:
        """Выход и повторное вступление отзывают прежний доступ, сохраняя покупки."""
        key = self.repo.save(self.author, "a", 1, self.purchase)
        self.assertEqual(self.repo.timezone(self.author), "Asia/Omsk")
        self.repo.set_timezone(self.author, "Europe/Moscow")
        with self.assertRaises(InvalidExpense):
            self.repo.set_timezone(self.owner, "Bad/Timezone")
        with connect_database(self.path) as reopened:
            self.assertEqual(SQLiteExpenseRepository(reopened).timezone(self.owner), "Europe/Moscow")
        self.assertEqual(self.repo.timezone(self.other), "Asia/Omsk")
        self.members.leave(2, self.owner.family_id, actor_joined_at=self.author.joined_at)
        with self.db:
            later = self.author.joined_at + timedelta(days=1)
            self.db.execute("INSERT INTO family_members VALUES (?, 2, ?)", (self.owner.family_id, later.isoformat()))
        for action in (lambda: self.repo.card(self.author, key), lambda: self.repo.timezone(self.author),
                       lambda: self.repo.save(self.author, "a", 1, self.purchase),
                       lambda: self.repo.set_timezone(self.author, "UTC")):
            with self.assertRaises(FamilyAccessEnded):
                action()
        self.assertEqual(self.repo.card(self.owner, key).author_id, 2)

    def test_rollback_allows_retry_after_partial_write_and_rejects_empty_purchase(self) -> None:
        """Сбой второй строки откатывает шапку, каталоги и уже вставленную строку."""
        with self.assertRaises(InvalidExpense):
            self.repo.save(self.author, "a", 1, replace(self.purchase, items=()))
        self.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON family_expense_items "
                        "WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.save(self.author, "a", 1, self.purchase)
        for table in ("family_expenses", "family_expense_products", "family_expense_categories", "family_expense_items"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.db.execute("DROP TRIGGER fail_second")
        for total in (None, 17500):
            key = self.repo.save(self.author, str(total), 1, replace(self.purchase, receipt_total=total))
            self.assertEqual(self.repo.card(self.owner, key).items, self.purchase.items)
        self.assertIsNotNone(self.repo.save(self.author, "a", 1, self.purchase))

    def test_two_connections_confirm_once_and_independent_purchases_survive(self) -> None:
        """Параллельные подтверждения возвращают один ID, разные операции — разные."""
        barrier = Barrier(2)
        def confirm(_: int) -> int:
            with connect_database(self.path) as db:
                barrier.wait(timeout=5)
                return SQLiteExpenseRepository(db).save(self.author, "same", 2, self.purchase)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(confirm, range(2))
        self.assertEqual(first, second)
        self.assertEqual(self.db.execute("SELECT count(*) FROM family_expense_items").fetchone()[0], 3)
        with self.assertRaises(InvalidExpense):
            self.repo.save(self.author, "same", 1, self.purchase)
        self.assertNotEqual(first, self.repo.save(self.author, "another", 2, self.purchase))
