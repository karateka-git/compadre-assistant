"""Тесты атомарного запуска SQLite migrations несколькими adapters."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import sqlite3
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
                versions = [row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )]

        self.assertEqual(sum(results), 4)
        self.assertTrue(all(0 <= result <= 4 for result in results))
        self.assertEqual(migration_count, 4)
        self.assertEqual(versions, [1, 2, 3, 4])

    def test_existing_profiles_receive_default_name_without_losing_data(self) -> None:
        """0003 обновляет NULL-имена после 0001 и 0002 без потери данных."""
        for applied_prefix in (1, 2):
            with self.subTest(applied_prefix=applied_prefix), TemporaryDirectory(
                prefix="user-name-migration-"
            ) as directory:
                root = Path(directory)
                database_path = root / "test.db"
                migrations = root / "migrations"
                migrations.mkdir()
                for version in range(1, applied_prefix + 1):
                    source = next(MIGRATIONS_DIR.glob(f"{version:04d}_*.sql"))
                    (migrations / source.name).write_bytes(source.read_bytes())

                with connect_database(database_path) as connection:
                    self.assertEqual(
                        [item.version for item in apply_migrations(connection, migrations)],
                        list(range(1, applied_prefix + 1)),
                    )
                    connection.execute("INSERT INTO app_users (id, display_name) VALUES (1, NULL)")
                    connection.execute("INSERT INTO app_users (id, display_name) VALUES (2, 'Existing')")
                    connection.execute(
                        "INSERT INTO external_identities "
                        "(id, app_user_id, channel, external_user_id, external_chat_id) "
                        "VALUES (10, 1, 'telegram', 'user-1', 'chat-1')"
                    )
                    connection.execute(
                        "INSERT INTO active_chat_modes "
                        "(app_user_id, channel, chat_id, capability_id) "
                        "VALUES (1, 'telegram', 'chat-1', 'obsidian')"
                    )
                    connection.execute(
                        "INSERT INTO articles "
                        "(id, app_user_id, source_url, normalized_url, title, cleaned_text) "
                        "VALUES (20, 1, 'https://example.com/a', 'https://example.com/a', 'Article', 'Body')"
                    )
                    if applied_prefix == 2:
                        connection.execute(
                            "INSERT INTO user_capability_preferences VALUES (1, 'ask', 0)"
                        )
                    connection.commit()

                    before_identity = tuple(connection.execute("SELECT * FROM external_identities").fetchone())
                    before_mode = tuple(connection.execute("SELECT * FROM active_chat_modes").fetchone())
                    before_article = tuple(connection.execute("SELECT * FROM articles").fetchone())
                    before_preferences = [
                        tuple(row) for row in connection.execute(
                            "SELECT * FROM user_capability_preferences ORDER BY app_user_id, capability_id"
                        )
                    ] if applied_prefix == 2 else None

                    if applied_prefix == 1:
                        source = next(MIGRATIONS_DIR.glob("0002_*.sql"))
                        (migrations / source.name).write_bytes(source.read_bytes())
                        self.assertEqual([item.version for item in apply_migrations(connection, migrations)], [2])
                        connection.execute(
                            "INSERT INTO user_capability_preferences VALUES (1, 'ask', 0)"
                        )
                        connection.commit()
                        before_preferences = [
                            tuple(row) for row in connection.execute(
                                "SELECT * FROM user_capability_preferences ORDER BY app_user_id, capability_id"
                            )
                        ]

                    source = MIGRATIONS_DIR / "0003_backfill_existing_user_names.sql"
                    (migrations / source.name).write_bytes(source.read_bytes())
                    self.assertEqual([item.version for item in apply_migrations(connection, migrations)], [3])
                    self.assertEqual(
                        [tuple(row) for row in connection.execute("SELECT id, display_name FROM app_users ORDER BY id")],
                        [(1, "Анатолий"), (2, "Existing")],
                    )
                    self.assertEqual(tuple(connection.execute("SELECT * FROM external_identities").fetchone()), before_identity)
                    self.assertEqual(tuple(connection.execute("SELECT * FROM active_chat_modes").fetchone()), before_mode)
                    self.assertEqual(tuple(connection.execute("SELECT * FROM articles").fetchone()), before_article)
                    self.assertEqual(
                        [tuple(row) for row in connection.execute(
                            "SELECT * FROM user_capability_preferences ORDER BY app_user_id, capability_id"
                        )],
                        before_preferences,
                    )
                    self.assertEqual(apply_migrations(connection, migrations), [])
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_clean_install_applies_all_migrations_without_creating_profiles(self) -> None:
        """Чистая установка применяет 0001–0004, оставляя каталог пользователей пустым."""
        with TemporaryDirectory(prefix="user-name-clean-install-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                self.assertEqual([item.version for item in apply_migrations(connection)], [1, 2, 3, 4])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 0)
                self.assertEqual(apply_migrations(connection), [])

    def test_private_chat_index_migrates_existing_rows(self) -> None:
        """Новый индекс применяется к старой БД без изменения identity."""
        with TemporaryDirectory(prefix="private-chat-index-") as directory:
            root = Path(directory)
            migrations = root / "migrations"
            migrations.mkdir()
            for version in (1, 2, 3):
                source = next(MIGRATIONS_DIR.glob(f"{version:04d}_*.sql"))
                (migrations / source.name).write_bytes(source.read_bytes())
            with connect_database(root / "test.db") as connection:
                self.assertEqual([item.version for item in apply_migrations(connection, migrations)], [1, 2, 3])
                connection.execute("INSERT INTO app_users (id, display_name) VALUES (1, 'Fixture')")
                connection.execute(
                    "INSERT INTO external_identities (app_user_id, channel, external_user_id, external_chat_id) "
                    "VALUES (1, 'telegram', '1001', '1001')"
                )
                connection.commit()
                source = next(MIGRATIONS_DIR.glob("0004_*.sql"))
                (migrations / source.name).write_bytes(source.read_bytes())
                self.assertEqual([item.version for item in apply_migrations(connection, migrations)], [4])
                self.assertEqual(connection.execute(
                    "SELECT external_user_id, external_chat_id FROM external_identities"
                ).fetchone()[:], ("1001", "1001"))
                indexes = {row[1] for row in connection.execute("PRAGMA index_list('external_identities')")}
                self.assertIn("idx_external_identities_channel_chat", indexes)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_capability_preferences_migrate_fresh_and_previous_database(self) -> None:
        """Новая таблица появляется без backfill и сохраняет данные схемы 0001."""
        for previous in (False, True):
            with self.subTest(previous=previous), TemporaryDirectory(prefix="capability-migration-") as directory:
                root = Path(directory)
                database_path = root / "test.db"
                migrations = root / "migrations"
                migrations.mkdir()
                initial = MIGRATIONS_DIR / "0001_initial_schema.sql"
                (migrations / initial.name).write_bytes(initial.read_bytes())
                with connect_database(database_path) as connection:
                    if previous:
                        self.assertEqual([item.version for item in apply_migrations(connection, migrations)], [1])
                        connection.execute("INSERT INTO app_users (id, display_name) VALUES (1, 'Fixture')")
                        connection.execute(
                            "INSERT INTO active_chat_modes (app_user_id, channel, chat_id, capability_id) "
                            "VALUES (1, 'telegram', 'chat', 'obsidian')"
                        )
                        connection.commit()
                    next_file = MIGRATIONS_DIR / "0002_user_capability_preferences.sql"
                    (migrations / next_file.name).write_bytes(next_file.read_bytes())
                    self.assertEqual(
                        [item.version for item in apply_migrations(connection, migrations)],
                        [2] if previous else [1, 2],
                    )
                    self.assertEqual(apply_migrations(connection, migrations), [])
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM user_capability_preferences").fetchone()[0], 0
                    )
                    if previous:
                        self.assertEqual(
                            tuple(connection.execute("SELECT id, display_name FROM app_users").fetchone()),
                            (1, "Fixture"),
                        )
                        self.assertEqual(
                            connection.execute("SELECT capability_id FROM active_chat_modes").fetchone()[0],
                            "obsidian",
                        )
                    connection.execute("INSERT OR IGNORE INTO app_users (id) VALUES (1)")
                    connection.execute(
                        "INSERT INTO user_capability_preferences VALUES (1, 'ask', 0)"
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("INSERT INTO user_capability_preferences VALUES (1, 'ask', 1)")
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("INSERT INTO user_capability_preferences VALUES (1, 'obsidian', 2)")
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("INSERT INTO user_capability_preferences VALUES (999, 'ask', 1)")
                    connection.execute("DELETE FROM app_users WHERE id = 1")
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM user_capability_preferences").fetchone()[0], 0
                    )
                    connection.commit()
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
