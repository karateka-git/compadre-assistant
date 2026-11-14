"""Проверяет запись метаданных Ask на отдельной временной SQLite."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

from compadre_assistant.capabilities.ask.data.sqlite.usage_repository import SQLiteUsageRepository
from compadre_assistant.capabilities.ask.domain.usage import MAX_USAGE_INTEGER, TokenUsage, UsageObservation
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations


_START = datetime(2026, 11, 13, 15, 37, 17, 345678, tzinfo=UTC)


def _observation() -> UsageObservation:
    return UsageObservation(uuid4(), _START, "timeweb", "fallback", TokenUsage(
        prompt_tokens=MAX_USAGE_INTEGER, completion_tokens=0, total_tokens=MAX_USAGE_INTEGER,
        prompt_text_tokens=5, image_tokens=7, completion_text_tokens=0, reasoning_tokens=0,
    ))


class SQLiteUsageRepositoryTests(unittest.TestCase):
    """Проверяет единственную публичную операцию repository на актуальной схеме."""

    def setUp(self) -> None:
        """Создаёт БД с актуальными миграциями и без профилей пользователей."""
        directory = self.enterContext(TemporaryDirectory(prefix="ask-usage-"))
        self.database_path = Path(directory) / "usage.db"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.repository = SQLiteUsageRepository(self.database_path)

    def test_store_keeps_first_observation_and_distinguishes_zero_from_missing(self) -> None:
        """Сохраняет счётчики и UTC без данных чата, профиля пользователя или расчёта."""
        first = _observation()
        unavailable = replace(first, call_id=uuid4(), actual_model=None, tokens=None)
        self.assertTrue(self.repository.store(first))
        self.assertFalse(self.repository.store(replace(first, provider="different", actual_model=None)))
        self.assertTrue(self.repository.store(unavailable))
        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT * FROM ask_usage WHERE call_id = ?", (str(first.call_id),)).fetchone()
            self.assertEqual(row.keys(), ["call_id", "started_at_us", "provider", "actual_model", "tokens_json"])
            self.assertEqual(tuple(row)[:4], (str(first.call_id), 1_794_584_237_345_678, "timeweb", "fallback"))
            self.assertEqual(json.loads(row["tokens_json"]), {
                "version": 1, "prompt_tokens": MAX_USAGE_INTEGER, "completion_tokens": 0,
                "total_tokens": MAX_USAGE_INTEGER, "prompt_text_tokens": 5, "image_tokens": 7,
                "cached_tokens": None, "completion_text_tokens": 0, "reasoning_tokens": 0,
            })
            self.assertEqual(tuple(connection.execute(
                "SELECT actual_model, tokens_json FROM ask_usage WHERE call_id = ?", (str(unavailable.call_id),),
            ).fetchone()), (None, None))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_list('ask_usage')").fetchall(), [])

    def test_concurrent_duplicate_stores_one_observation(self) -> None:
        """Конкурирующие вызовы через разные соединения сохраняют первого победителя."""
        first = _observation()
        observations = (first, replace(first, actual_model="other"))
        barrier = Barrier(2, timeout=5)

        def store(observation: UsageObservation) -> bool:
            barrier.wait()
            return SQLiteUsageRepository(self.database_path).store(observation)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(store, observations))
        self.assertCountEqual(results, (True, False))
        with connect_database(self.database_path) as connection:
            saved = [row[0] for row in connection.execute("SELECT actual_model FROM ask_usage")]
        self.assertEqual(saved, [observations[results.index(True)].actual_model])

    def test_failed_write_rolls_back_and_can_retry(self) -> None:
        """Ошибка после вставки откатывается, а повтор с тем же ключом проходит."""
        observation = _observation()
        with connect_database(self.database_path) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_usage_write AFTER INSERT ON ask_usage BEGIN "
                "SELECT RAISE(FAIL, 'forced failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.store(observation)
        with connect_database(self.database_path) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ask_usage").fetchone()[0], 0)
            connection.execute("DROP TRIGGER fail_usage_write")
        self.assertTrue(self.repository.store(observation))

    def test_schema_rejects_invalid_observation_fields(self) -> None:
        """SQLite проверяет ключ, время, профиль и фактическую модель."""
        key = str(uuid4())
        with connect_database(self.database_path) as connection:
            for fields in (("short", 0, "timeweb", None), (key, "invalid", "timeweb", None),
                           (key, 0, " ", None), (key, 0, "timeweb", " ")):
                with self.subTest(fields=fields), self.assertRaises(sqlite3.IntegrityError), connection:
                    connection.execute(
                        "INSERT INTO ask_usage (call_id, started_at_us, provider, actual_model) VALUES (?, ?, ?, ?)",
                        fields,
                    )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ask_usage").fetchone()[0], 0)
