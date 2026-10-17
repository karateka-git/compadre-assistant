"""Тесты атомарного запуска SQLite migrations несколькими adapters."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import (
    MIGRATIONS_DIR, MigrationError, apply_migrations,
)
import compadre_assistant.core.data.sqlite.migration_runner as migration_runner


class MigrationRunnerTest(unittest.TestCase):
    """Проверяет межпроцессный алгоритм миграций на отдельных соединениях."""

    def test_pending_migrations_preserve_rows_and_run_in_order(self) -> None:
        """Искусственные 0002/0003 обновляют применённую 0001 без потери строк."""
        with TemporaryDirectory(prefix="migration-upgrade-") as directory:
            root = Path(directory)
            migrations = root / "migrations"
            migrations.mkdir()
            initial = MIGRATIONS_DIR / "0001_initial_schema.sql"
            (migrations / initial.name).write_bytes(initial.read_bytes())
            with connect_database(root / "test.db") as connection:
                apply_migrations(connection, migrations)
                connection.execute("INSERT INTO app_users (display_name) VALUES ('Fixture')")
                connection.commit()
                # Создаём файлы в обратном порядке, чтобы проверить сортировку runner.
                (migrations / "0003_fill_flag.sql").write_text(
                    "UPDATE app_users SET test_flag = 7;\n", encoding="utf-8",
                )
                (migrations / "0002_add_flag.sql").write_text(
                    "ALTER TABLE app_users ADD COLUMN test_flag INTEGER NOT NULL DEFAULT 0;\n",
                    encoding="utf-8",
                )
                completed = apply_migrations(connection, migrations)
                self.assertEqual([migration.version for migration in completed], [2, 3])
                self.assertEqual(tuple(connection.execute("SELECT display_name, test_flag FROM app_users").fetchone()), ("Fixture", 7))
                before = [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")]
                self.assertEqual(apply_migrations(connection, migrations), [])
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")], before)
                self.assertEqual([row[0] for row in before], [1, 2, 3])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_invalid_file_sequences_fail_before_schema_creation(self) -> None:
        """Пустой каталог, неверные имена, повторы и пропуски не создают журнал."""
        for names in (
            (), ("bad.sql",), ("0000_zero.sql",), ("0002_only.sql",),
            ("0001_first.sql", "0001_duplicate.sql"),
            ("0001_first.sql", "0003_gap.sql"),
        ):
            with self.subTest(names=names), TemporaryDirectory(prefix="migration-files-") as directory:
                root = Path(directory)
                migrations = root / "migrations"
                migrations.mkdir()
                for name in names:
                    (migrations / name).write_text("CREATE TABLE fixture (id INTEGER);\n", encoding="utf-8")
                with connect_database(root / "test.db") as connection:
                    with self.assertRaises(MigrationError):
                        apply_migrations(connection, migrations)
                    self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_unknown_renamed_or_incomplete_applied_history_is_rejected(self) -> None:
        """Неизвестная схема и повреждённый журнал отклоняются до ожидающего SQL."""
        for history in (
            [(1, "0001_renamed.sql")],
            [(1, "0001_first.sql"), (2, "0002_second.sql"), (3, "0003_newer.sql")],
            [(2, "0002_second.sql")],
        ):
            with self.subTest(history=history), TemporaryDirectory(prefix="migration-history-") as directory:
                root = Path(directory)
                migrations = root / "migrations"
                migrations.mkdir()
                for name in ("0001_first.sql", "0002_second.sql"):
                    (migrations / name).write_text("CREATE TABLE unexpected (id INTEGER);\n", encoding="utf-8")
                with connect_database(root / "test.db") as connection:
                    migration_runner._ensure_migrations_table(connection)
                    connection.executemany("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", history)
                    connection.commit()
                    with self.assertRaises(MigrationError):
                        apply_migrations(connection, migrations)
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'unexpected'").fetchone())
                    self.assertEqual([tuple(row) for row in connection.execute("SELECT version, name FROM schema_migrations ORDER BY version")], history)

    def test_later_failure_keeps_prior_migration_and_allows_retry(self) -> None:
        """Успешная 0002 сохраняется при сбое 0003; повтор выполняет только 0003."""
        with TemporaryDirectory(prefix="migration-retry-") as directory:
            root = Path(directory)
            migrations = root / "migrations"
            migrations.mkdir()
            for name, sql in (
                ("0001_first.sql", "CREATE TABLE fixture (id INTEGER);\n"),
                ("0002_second.sql", "INSERT INTO fixture VALUES (2);\n"),
                ("0003_third.sql", "INSERT INTO fixture VALUES (3);\nINSERT INTO absent VALUES (1);\n"),
            ):
                (migrations / name).write_text(sql, encoding="utf-8")
            with connect_database(root / "test.db") as connection:
                with self.assertRaises(MigrationError):
                    apply_migrations(connection, migrations)
                self.assertEqual([row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")], [1, 2])
                self.assertEqual([row[0] for row in connection.execute("SELECT id FROM fixture")], [2])
                # Устраняем внешнюю предпосылку сбоя без изменения SQL-файла.
                connection.execute("CREATE TABLE absent (id INTEGER)")
                connection.commit()
                self.assertEqual([migration.version for migration in apply_migrations(connection, migrations)], [3])
                self.assertEqual([row[0] for row in connection.execute("SELECT id FROM fixture ORDER BY id")], [2, 3])
                self.assertFalse(connection.in_transaction)

    def test_two_adapters_can_initialize_one_empty_database(self) -> None:
        """Второй adapter повторно проверяет журнал после захвата SQLite lock."""
        with TemporaryDirectory(prefix="compadre-assistant-migration-race-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as connection:
                migration_runner._ensure_migrations_table(connection)
            barrier = Barrier(2)
            original_get_applied = migration_runner._get_applied_migrations

            def synchronized_get_applied(connection):
                applied = original_get_applied(connection)
                barrier.wait(timeout=2)
                return applied

            def initialize() -> int:
                with connect_database(database_path) as connection:
                    return len(apply_migrations(connection))

            with patch.object(
                migration_runner,
                "_get_applied_migrations",
                side_effect=synchronized_get_applied,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _index: initialize(), range(2)))

            with connect_database(database_path) as connection:
                migration_count = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations"
                ).fetchone()[0]

        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(migration_count, 1)


if __name__ == "__main__":
    unittest.main()
