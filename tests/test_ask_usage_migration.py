"""Проверяет переход с выпущенной 0004 к метаданным вызовов Ask."""

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from compadre_assistant.capabilities.ask.data.sqlite.usage_repository import SQLiteUsageRepository
from compadre_assistant.capabilities.ask.domain.usage import UsageObservation
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, MigrationError, apply_migrations
from compadre_assistant.core.data.sqlite.runtime_schema import (
    migration_sql_fingerprint, schema_fingerprint, validate_compatible_schema,
)


_MIGRATION = "0005_ask_usage.sql"
_EXISTING_TABLES = ("app_users", "active_chat_modes", "user_capability_preferences", "articles")


def _journal(connection: sqlite3.Connection) -> list[list]:
    return [list(row) for row in connection.execute("SELECT version, name FROM schema_migrations ORDER BY version")]


def _existing_rows(connection: sqlite3.Connection) -> list[tuple]:
    return [tuple(connection.execute(f"SELECT * FROM {table}").fetchone()) for table in _EXISTING_TABLES]


class AskUsageMigrationTest(unittest.TestCase):
    """Применяет SQL из репозитория на изолированных временных БД."""

    def setUp(self) -> None:
        """Применяет префикс из четырёх выпущенных миграций и добавляет данные."""
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="ask-usage-migration-")))
        self.previous_sql = self.root / "previous-sql"
        self.previous_sql.mkdir()
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))[:4]:
            (self.previous_sql / path.name).write_bytes(path.read_bytes())
        self.database = self.root / "previous.db"
        with connect_database(self.database) as db:
            self.assertEqual([item.version for item in apply_migrations(db, self.previous_sql)], [1, 2, 3, 4])
            with db:
                db.execute("INSERT INTO app_users (id, display_name) VALUES (1, 'Fixture')")
                db.execute("INSERT INTO active_chat_modes VALUES (1, 'vk', '42', 'ask', 2, 'timestamp')")
                db.execute("INSERT INTO user_capability_preferences VALUES (1, 'ask', 0)")
                db.execute("INSERT INTO articles (app_user_id, source_url, normalized_url) "
                           "VALUES (1, 'https://example.com', 'https://example.com')")
            self.base_journal = _journal(db)
            self.base_fingerprint = schema_fingerprint(db)
            self.base_rows = _existing_rows(db)

    def test_upgrade_preserves_existing_data_and_matches_fresh_schema(self) -> None:
        """Обе установки дают идентичный журнал и пять полей метаданных."""
        observation = UsageObservation(uuid4(), datetime(2026, 11, 14, tzinfo=UTC), "timeweb", None, None)
        with connect_database(self.root / "fresh.db") as fresh:
            self.assertEqual([item.version for item in apply_migrations(fresh)], [1, 2, 3, 4, 5])
            fresh_fingerprint = schema_fingerprint(fresh)
            fresh_journal = _journal(fresh)
        with connect_database(self.database) as db:
            self.assertEqual([item.name for item in apply_migrations(db)], [_MIGRATION])
            self.assertEqual(_journal(db), fresh_journal)
            self.assertEqual(schema_fingerprint(db), fresh_fingerprint)
            self.assertEqual(_existing_rows(db), self.base_rows)
            self.assertEqual([row[1] for row in db.execute("PRAGMA table_info('ask_usage')")],
                             ["call_id", "started_at_us", "provider", "actual_model", "tokens_json"])
            self.assertEqual(db.execute("PRAGMA index_info('idx_ask_usage_started_at')").fetchone()[2], "started_at_us")
            self.assertTrue(SQLiteUsageRepository(self.database).store(observation))
            self.assertEqual(apply_migrations(db), [])
            self.assertEqual(db.execute("SELECT call_id FROM ask_usage").fetchone()[0], str(observation.call_id))
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failure_after_ddl_rolls_back_schema_data_and_journal(self) -> None:
        """Ошибка после DDL не закрепляет ни одну часть добавления таблицы."""
        migration = self.previous_sql / _MIGRATION
        migration.write_bytes((MIGRATIONS_DIR / _MIGRATION).read_bytes() + b"\nINSERT INTO missing_table VALUES (1);\n")
        with connect_database(self.database) as db:
            with self.assertRaisesRegex(MigrationError, "missing_table"):
                apply_migrations(db, self.previous_sql)
            self.assertFalse(db.in_transaction)
            self.assertEqual(_journal(db), self.base_journal)
            self.assertEqual(schema_fingerprint(db), self.base_fingerprint)
            self.assertEqual(_existing_rows(db), self.base_rows)
            self.assertEqual([item.name for item in apply_migrations(db)], [_MIGRATION])

    def test_previous_runtime_requires_normal_compatibility_admission(self) -> None:
        """Выпущенный префикс строго отклоняет новую схему без точного допуска."""
        context = dict(base_sha="a" * 40, base_image_id="sha256:" + "b" * 64,
                       candidate_sha="c" * 40, candidate_image_id="sha256:" + "d" * 64,
                       controller_sha="e" * 40, verification_id="f" * 64)
        with connect_database(self.database) as db:
            apply_migrations(db)
            target_journal = _journal(db)
            admission = dict(context, format_version=1, base_journal=self.base_journal,
                             target_journal=target_journal, base_sql_sha256=migration_sql_fingerprint(self.previous_sql),
                             schema_sha256=schema_fingerprint(db))
            with self.assertRaisesRegex(MigrationError, _MIGRATION):
                apply_migrations(db, self.previous_sql)
            self.assertEqual(validate_compatible_schema(db, admission, context, self.previous_sql), [])
            self.assertEqual(_journal(db), target_journal)
            self.assertEqual(_existing_rows(db), self.base_rows)
