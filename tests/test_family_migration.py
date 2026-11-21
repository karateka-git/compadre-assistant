"""Проверяет миграцию семейных таблиц и ограничения связей."""

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations


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
