"""Контроль записи покупок и настроек семьи на временной SQLite."""

import sqlite3
import shutil
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
from compadre_assistant.capabilities.family.domain.expense_categories import InvalidCategory
from compadre_assistant.capabilities.family.domain.recognition import CATEGORIES
from compadre_assistant.capabilities.family.domain.expense_reports import ExpensePeriod, ProductSelection
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations


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
            ExpenseItem("Продукты", "Яблоки", 7500, Decimal("0.375"), "кг", 20000,
                        amount_before_discount=8000),
            ExpenseItem("Питомцы", None, 10000),
        ), "Лента", 17000)

    def test_upgrade_category_catalog_preserves_custom_names_keys_and_history(self) -> None:
        """Добавляет отсутствующие стартовые категории прежней семье без замены ссылок в чеке."""
        previous = self.path.parent / "old-sql"
        previous.mkdir()
        for migration in MIGRATIONS_DIR.glob("*.sql"):
            if migration.name[:4] <= "0012":
                shutil.copyfile(migration, previous / migration.name)
        with connect_database(self.path.parent / "old.db") as db:
            apply_migrations(db, previous)
            with db:
                db.execute("INSERT INTO app_users (id) VALUES (1)")
            now = datetime.now(UTC)
            family = SQLiteFamilyRepository(db).create(1, "Дом", now)
            access = ExpenseAccess(family.id, 1, now)
            with db:
                db.executemany("INSERT INTO family_expense_categories (id, family_id, name, name_key) "
                               "VALUES (?, ?, ?, ?)", [(21, family.id, "продукты", "продукты"),
                               (22, family.id, "Без детализации", "без детализации")])
                db.execute("INSERT INTO family_expenses (id,family_id,author_id,operation_id,revision,purchase_date,"
                           "created_at,source,receipt_total) VALUES (1,?,1,'legacy',1,'2026-11-23',?,'manual',99)",
                           (family.id, now.isoformat()))
                db.executemany("INSERT INTO family_expense_items (family_id,expense_id,position,category_id,amount) "
                               "VALUES (?,1,?,?,?)", [(family.id, 1, 22, 100), (family.id, 2, None, -1)])
            apply_migrations(db)
            repo = SQLiteExpenseRepository(db)
            migrated = {category.name: category.id for category in repo.categories(access)}
            self.assertEqual((len(migrated), migrated["продукты"], migrated["Без детализации"]), (12, 21, 22))
            summary = repo.summary(access, ExpensePeriod(date(2026, 11, 1), date(2026, 12, 1)))
            self.assertEqual([(row.id, row.amount) for row in summary.categories], [(22, 100), (None, -1)])
            self.assertEqual(apply_migrations(db), [])

    def test_category_lifecycle_is_shared_and_preserves_history(self) -> None:
        """Член семьи управляет каталогом, не меняя ID, сумму или каталог другой семьи."""
        base = self.repo.categories(self.owner)
        self.assertEqual([c.name for c in base], list(CATEGORIES))
        added = self.repo.create_category(self.author, " Для   кота ")
        self.assertEqual(self.repo.create_category(self.owner, "для КОТА"), added)
        self.assertNotIn(added, self.repo.categories(self.other))
        purchase_id = self.repo.save(self.owner, "food", 1, self.purchase)
        changed = self.repo.rename_category(self.author, base[0], "Еда")
        self.assertEqual(self.repo.card(self.owner, purchase_id).items[0].category, "Еда")
        self.assertEqual(changed.id, base[0].id)
        hidden = self.repo.set_category_hidden(self.owner, changed, True)
        self.assertEqual(self.repo.categories(self.author, hidden=True), (hidden,))
        self.assertNotIn(hidden, self.repo.categories(self.author))
        self.assertNotIn("Продукты", [c.name for c in self.repo.categories(self.author)])
        for operation in (lambda: self.repo.create_category(self.author, "еда"),
                          lambda: self.repo.rename_category(self.author, added, "еда"),
                          lambda: self.repo.rename_category(self.author, base[0], "Устарело"),
                          lambda: self.repo.set_category_hidden(self.other, hidden, False),
                          lambda: self.repo.rename_category(self.other, hidden, "Чужая")):
            with self.assertRaises(InvalidCategory):
                operation()
        self.assertEqual(self.repo.categories(self.owner, hidden=True), (hidden,))
        restored = self.repo.set_category_hidden(self.author, hidden, False)
        self.assertEqual((restored.id, restored.name, restored.hidden), (base[0].id, "Еда", False))
        self.assertEqual([c.name for c in self.repo.categories(self.other)], list(CATEGORIES))

    def test_category_protection_and_membership(self) -> None:
        """Отклоняет зарезервированные названия, утрату доступа и подмену запасной категории."""
        fallback = next(c for c in self.repo.categories(self.owner) if c.is_fallback)
        for operation in (lambda: self.repo.create_category(self.owner, "без  ДЕТАЛИЗАЦИИ"),
                          lambda: self.repo.create_category(self.owner, "  "),
                          lambda: self.repo.rename_category(self.owner, replace(fallback, name="Чужая"), "Другая"),
                          lambda: self.repo.set_category_hidden(self.owner, fallback, True)):
            with self.assertRaises(InvalidCategory):
                operation()
        self.members.leave(2, self.author.family_id, actor_joined_at=self.author.joined_at)
        for operation in (lambda: self.repo.categories(self.author),
                          lambda: self.repo.create_category(self.author, "Еда"),
                          lambda: self.repo.rename_category(self.author, fallback, "Новое"),
                          lambda: self.repo.set_category_hidden(self.author, fallback, False)):
            with self.assertRaises(FamilyAccessEnded):
                operation()

    def test_concurrent_category_edits_only_accept_one_revision(self) -> None:
        """Два соединения создают одну категорию и принимают одну из конкурирующих правок."""
        barrier = Barrier(2)

        def edit(name):
            with connect_database(self.path) as db:
                repo = SQLiteExpenseRepository(db)
                category = repo.create_category(self.author, "Для дома")
                barrier.wait(timeout=5)
                try:
                    return repo.rename_category(self.author, category, name)
                except InvalidCategory:
                    return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(edit, ["Ремонт", "Мебель"]))
        self.assertEqual(result.count(None), 1)
        self.assertEqual(len(self.repo.categories(self.owner)), 12)

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

    def test_package_size_belongs_to_the_item_not_the_product(self) -> None:
        """Один товар с разной фасовкой сохраняет обе подписи и общую сумму отчёта."""
        items = tuple(ExpenseItem("Продукты", "Печенье", 7199, package_size=size) for size in ("224 г", "400 г"))
        purchase = replace(self.purchase, items=items, receipt_total=None)
        key = self.repo.save(self.author, "pack", 1, purchase)
        self.assertEqual(self.repo.card(self.owner, key).items, items)
        products = self.repo.search(self.owner, ExpensePeriod(date(2026, 11, 1), date(2026, 12, 1)), "Печенье", 0, 4)
        self.assertEqual([(p.name, p.amount) for p in products.rows], [("Печенье", 14398)])

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
            custom = replace(self.purchase.items[0], category="Новая категория")
            self.repo.save(self.author, "a", 1, replace(self.purchase, items=(custom, self.purchase.items[1])))
        self.assertEqual([c.name for c in self.repo.categories(self.owner)], list(CATEGORIES))
        for table in ("family_expenses", "family_expense_products", "family_expense_items"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.db.execute("DROP TRIGGER fail_second")
        for total in (None, 17500):
            key = self.repo.save(self.author, str(total), 1, replace(self.purchase, receipt_total=total))
            self.assertEqual(self.repo.card(self.owner, key).items, self.purchase.items)
        self.assertIsNotNone(self.repo.save(self.author, "a", 1, self.purchase))

    def test_report_drilldown_counts_each_item_once_and_scopes_each_query(self) -> None:
        """Сводка, поиск и страницы сохраняют знаки, даты, владельца и границы периода."""
        period = ExpensePeriod(date(2026, 11, 1), date(2026, 12, 1))
        mixed = replace(self.purchase, items=(
            ExpenseItem("Продукты", "Молоко", 9500), ExpenseItem("Продукты", "МОЛОКО", 9500),
            ExpenseItem("Продукты", None, 5900), ExpenseItem("Питомцы", "Корм", 44000),
        ), receipt_total=68000)
        key = self.repo.save(self.author, "one", 1, mixed)
        taxi = replace(mixed, items=(ExpenseItem("Транспорт", "Такси", 45000),), receipt_total=None)
        key2 = self.repo.save(self.owner, "two", 1, taxi)
        self.repo.save(self.author, "later", 1, replace(taxi, purchase_date=period.end))
        self.repo.save(self.other, "theirs", 1, taxi)
        summary = self.repo.summary(self.author, period)
        self.assertEqual(summary.total, 113000)
        self.assertEqual([(x.name, x.amount) for x in summary.categories], [
            ("Транспорт", 45000), ("Питомцы", 44000), ("Продукты", 24900), ("Без детализации", -900)])
        category = summary.categories[2].id
        products = self.repo.products(self.author, period, category, 0, 1)
        milk = products.rows[0].id
        self.assertEqual((products.count, products.total, products.rows[0].amount), (2, 24900, 19000))
        unknown = self.repo.products(self.author, period, category, 1, 1).rows[0]
        self.assertEqual((unknown.id, unknown.name, unknown.amount), (None, "Без детализации", 5900))
        self.assertEqual(self.repo.history(self.owner, period, 0, 1, ProductSelection(None, None)).total, -900)
        self.assertEqual(self.repo.history(self.owner, period, 0, 1, ProductSelection(None, category)).total, 5900)
        found = self.repo.search(self.owner, period, "  мОЛо  ", 0, 4)
        self.assertEqual((found.rows[0].id, found.total), (milk, 19000))
        self.assertFalse(self.repo.search(self.owner, period, "%", 0, 4).rows)
        purchases = self.repo.history(self.author, period, 0, 4, ProductSelection(milk, category))
        self.assertEqual([(x.id, x.amount) for x in purchases.rows], [(key, 19000)])
        self.assertEqual([x.id for x in self.repo.history(self.owner, period, 0, 4).rows], [key2, key])
        self.assertEqual(self.repo.history(self.owner, period, 5, 1).count, 2)
        self.assertEqual(self.repo.summary(self.other, period).total, 45000)
        self.assertFalse(self.repo.products(self.other, period, category, 0, 4).rows)
        self.assertFalse(self.repo.search(self.other, period, "Молоко", 0, 4).rows)
        self.assertFalse(self.repo.history(self.other, period, 0, 4, ProductSelection(milk)).rows)
        self.members.leave(2, self.owner.family_id, actor_joined_at=self.author.joined_at)
        for query in (lambda: self.repo.summary(self.author, period),
                      lambda: self.repo.products(self.author, period, category, 0, 4),
                      lambda: self.repo.search(self.author, period, "Молоко", 0, 4),
                      lambda: self.repo.history(self.author, period, 0, 4)):
            with self.assertRaises(FamilyAccessEnded):
                query()

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
