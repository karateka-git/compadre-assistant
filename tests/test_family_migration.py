"""Проверяет миграцию семейных таблиц и ограничения связей."""

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations


class FamilyMigrationTest(unittest.TestCase):
    """Проверяет ограничения семейных таблиц в актуальной схеме."""

    def setUp(self) -> None:
        """Открывает отдельную временную базу для каждого теста."""
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="family-migrations-")))
        self.connection = self.enterContext(connect_database(self.root / "family.db"))

    def test_family_requires_an_owner_from_its_members(self) -> None:
        """Проверяет владельца той же семьи, уникальность членства и защиту профиля."""
        self._apply_fresh_schema()
        with self.assertRaises(sqlite3.IntegrityError), self.connection as db:
            db.execute("INSERT INTO families (name, owner_app_user_id, created_at) VALUES ('Дом', 1, 'fixture')")
        self.assertEqual(self._snapshot(("families", "family_members")), ([], []))

        first = self._create_family(1)
        second = self._create_family(2)
        with self.assertRaises(sqlite3.IntegrityError), self.connection as db:
            db.execute("INSERT INTO family_members VALUES (?, 2, 'fixture')", (first,))
        with self.assertRaises(sqlite3.IntegrityError), self.connection as db:
            db.execute("UPDATE families SET owner_app_user_id = 2 WHERE id = ?", (first,))
        with self.assertRaises(sqlite3.IntegrityError), self.connection as db:
            db.execute("DELETE FROM family_members WHERE family_id = ? AND app_user_id = 1", (first,))
        with self.assertRaises(sqlite3.IntegrityError) as blocked, self.connection as db:
            db.execute("DELETE FROM app_users WHERE id = 1")
        self.assertEqual(blocked.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY)
        self.assertEqual([tuple(row) for row in self.connection.execute(
            "SELECT id, owner_app_user_id FROM families ORDER BY id"
        )], [(first, 1), (second, 2)])
        self._assert_integrity()

    def test_family_delete_cascades_and_history_does_not_protect_former_member(self) -> None:
        """Связь с автором может исчезнуть, а удаление семьи очищает только её данные."""
        self._apply_fresh_schema()
        first = self._create_family(1)
        second = self._create_family(2)
        with self.connection as db:
            for family_id, code in ((first, "one"), (second, "two")):
                db.execute(
                    "INSERT INTO family_invites (family_id, created_by_app_user_id, code_digest, created_at, expires_at) "
                    "VALUES (?, 3, ?, 'fixture', 'expiry')", (family_id, code)
                )
                db.execute(
                    "INSERT INTO family_shopping_items "
                    "(family_id, number, name, status, created_by_app_user_id, created_at, bought_by_app_user_id, bought_at) "
                    "VALUES (?, 1, 'Хлеб', 'bought', 3, 'fixture', 3, 'fixture')", (family_id,)
                )
            db.execute("DELETE FROM app_users WHERE id = 3")
        for table, fields in (
            ("family_invites", "created_by_app_user_id"),
            ("family_shopping_items", "created_by_app_user_id, bought_by_app_user_id"),
        ):
            for row in self.connection.execute(f"SELECT {fields} FROM {table}"):
                self.assertTrue(all(value is None for value in row))
        with self.assertRaises(sqlite3.IntegrityError), self.connection as db:
            db.execute(
                "INSERT INTO family_shopping_items (family_id, number, name, status, created_at) "
                "VALUES (?, 1, 'Яблоки', 'needed', 'fixture')", (first,)
            )
        with self.connection as db:
            db.execute("DELETE FROM families WHERE id = ?", (first,))
        for table in ("family_members", "family_invites", "family_shopping_items"):
            self.assertEqual([row[0] for row in self.connection.execute(f"SELECT family_id FROM {table}")], [second])
        self._assert_integrity()

    def test_expenses_upgrade_preserves_existing_family_and_shopping(self) -> None:
        """Обновление с 0010 даёт прежней семье пояс и сохраняет покупки."""
        previous = self.root / "schema"
        previous.mkdir()
        for path in MIGRATIONS_DIR.glob("*.sql"):
            if path.name[:4] <= "0010":
                (previous / path.name).write_bytes(path.read_bytes())
        apply_migrations(self.connection, previous)
        with self.connection as db:
            db.execute("INSERT INTO app_users (id) VALUES (1)")
        family = self._create_family(1)
        with self.connection as db:
            db.execute("INSERT INTO family_shopping_items (family_id, number, name, status, created_at) "
                       "VALUES (?, 1, 'Хлеб', 'needed', 'fixture')", (family,))
        tables = ("family_members", "family_shopping_items")
        snapshot = self._snapshot(tables)
        apply_migrations(self.connection)
        self.assertEqual(self._snapshot(tables), snapshot)
        self.assertEqual(self.connection.execute("SELECT expense_timezone FROM families WHERE id=?",
                                                (family,)).fetchone()[0], "Asia/Omsk")
        self._assert_integrity()

    def test_expense_lines_check_catalog_ownership_and_signed_difference(self) -> None:
        """Схема принимает отрицательную разницу и отклоняет чужой каталог."""
        self._apply_fresh_schema()
        first, second = self._create_family(1), self._create_family(2)
        with self.connection as db:
            expense = db.execute("INSERT INTO family_expenses (family_id, operation_id, revision, purchase_date, "
                                 "created_at, source) VALUES (?, 'fixture', 1, '2026-11-23', 'now', 'photo')",
                                 (first,)).lastrowid
            category = db.execute("SELECT id FROM family_expense_categories WHERE family_id=? AND name_key='транспорт'",
                                  (second,)).fetchone()[0]
            product = db.execute("INSERT INTO family_expense_products(family_id, name, name_key) "
                                 "VALUES (?, 'Такси', 'такси')", (second,)).lastrowid
            own = db.execute("SELECT id FROM family_expense_categories WHERE family_id=? AND name_key='транспорт'",
                             (first,)).fetchone()[0]
            insert = ("INSERT INTO family_expense_items(family_id, expense_id, position, amount, "
                      "category_id, product_id) VALUES (?, ?, ?, ?, ?, ?)")
            db.execute(insert, (first, expense, 1, -5900, None, None))
        for value, cat, item in ((100, category, None), (100, own, product),
                                 (-1, own, None), (100, None, None), (1.5, own, None)):
            with self.subTest(value=value, category=cat, item=item):
                with self.assertRaises(sqlite3.IntegrityError), self.connection as db:
                    db.execute(insert, (first, expense, 2, value, cat, item))
        self._assert_integrity()

    def _apply_fresh_schema(self) -> None:
        apply_migrations(self.connection)
        with self.connection as db:
            db.execute("INSERT INTO app_users (id) VALUES (1), (2), (3)")

    def _create_family(self, owner_id: int) -> int:
        with self.connection as db:
            family_id = db.execute(
                "INSERT INTO families (name, owner_app_user_id, created_at) VALUES ('Дом', ?, 'fixture')",
                (owner_id,),
            ).lastrowid
            assert family_id is not None
            db.execute("INSERT INTO family_members VALUES (?, ?, 'fixture')", (family_id, owner_id))
        return family_id

    def _snapshot(self, tables: tuple[str, ...]) -> tuple[list[tuple], ...]:
        return tuple([tuple(row) for row in self.connection.execute(f"SELECT * FROM {table}")] for table in tables)

    def _assert_integrity(self) -> None:
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
