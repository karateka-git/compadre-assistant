"""Проверяет запись и чтение учёта Ask на временных SQLite с миграциями."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

from compadre_assistant.capabilities.ask.data.sqlite.usage_repository import SQLiteUsageRepository
from compadre_assistant.capabilities.ask.domain.usage import (
    MAX_USAGE_INTEGER, TokenUsage, UnknownUsageCost, UsageObservation, UsagePeriod, UsageTotals,
)
from compadre_assistant.capabilities.ask.domain.usage_pricing import UsageEstimate, UsagePricing, UsageTariff
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations


_START = datetime(2026, 11, 13, 15, 37, 17, 345678, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_TARIFF = UsageTariff("timeweb", "sample", "Проверка", _START, None, 1, 1)


def _priced_usage(prompt_tokens: int, started_at: datetime = _START) -> UsageEstimate:
    return UsagePricing((_TARIFF,)).estimate(UsageObservation(
        uuid4(), started_at, "timeweb", "sample",
        TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=0),
    ))


class SQLiteUsageRepositoryTests(unittest.TestCase):
    """Проверяет все публичные операции репозитория на действующей схеме."""

    def setUp(self) -> None:
        """Создаёт отдельную БД без профилей и применяет актуальные миграции."""
        directory = self.enterContext(TemporaryDirectory(prefix="ask-usage-"))
        self.database_path = Path(directory) / "usage.db"
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
        self.repository = SQLiteUsageRepository(self.database_path)
        self.period = UsagePeriod(_START, _START + timedelta(seconds=1))

    def test_store_keeps_original_snapshot_and_needs_no_profile(self) -> None:
        """Первый результат с данным ключом сохраняет наблюдение и тариф."""
        first = _priced_usage(2)
        updated_tariff = replace(_TARIFF, prompt_rate=3)
        duplicate = UsagePricing((updated_tariff,)).estimate(first.observation)
        self.assertTrue(self.repository.store(first))
        self.assertFalse(self.repository.store(duplicate))
        self.assertEqual(self.repository.read_totals((self.period,)), (UsageTotals(2, 0),))
        with connect_database(self.database_path) as connection:
            row = connection.execute("SELECT * FROM ask_usage").fetchone()
            self.assertEqual(row["call_id"], str(first.observation.call_id))
            self.assertEqual(row["provider"], "timeweb")
            self.assertEqual(row["actual_model"], "sample")
            self.assertEqual(row["started_at_us"], 1_794_584_237_345_678)
            self.assertEqual(row["cost_nanorubles"], 2)
            tokens = json.loads(row["tokens_json"])
            tariff = json.loads(row["tariff_json"])
            self.assertEqual((tokens["version"], tariff["version"]), (1, 1))
            self.assertEqual(tokens["prompt_tokens"], 2)
            self.assertEqual(tokens["completion_tokens"], 0)
            self.assertIsNone(tokens["cached_tokens"])
            self.assertEqual(tariff["source"], "Проверка")
            self.assertEqual(tariff["prompt_rate"], 1)
            self.assertEqual(tariff["effective_from_us"], row["started_at_us"])
            self.assertIsNone(tariff["effective_until_us"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_list('ask_usage')").fetchall(), [])

    def test_read_totals_preserves_boundaries_zero_and_future_reasons(self) -> None:
        """Ровно начало входит в интервал, конец принадлежит следующему."""
        just_after = _START + _MICROSECOND
        self.assertTrue(self.repository.store(_priced_usage(2)))
        self.assertTrue(self.repository.store(_priced_usage(5, just_after)))
        self.assertTrue(self.repository.store(_priced_usage(0, just_after)))
        no_usage = UsagePricing((_TARIFF,)).estimate(UsageObservation(
            uuid4(), just_after, "timeweb", "sample", None,
        ))
        self.assertEqual(no_usage.unknown_reason, UnknownUsageCost.COUNTERS_MISSING)
        self.assertTrue(self.repository.store(no_usage))
        with connect_database(self.database_path) as connection, connection:
            connection.execute(
                "UPDATE ask_usage SET unknown_reason = 'future_reason' WHERE call_id = ?",
                (str(no_usage.observation.call_id),),
            )
        periods = (UsagePeriod(just_after, just_after + _MICROSECOND), UsagePeriod(_START, just_after),
                   UsagePeriod(just_after, just_after), self.period)
        self.assertEqual(self.repository.read_totals(periods), (
            UsageTotals(5, 1), UsageTotals(2, 0), UsageTotals(0, 0), UsageTotals(7, 1),
        ))
        self.assertEqual(self.repository.read_totals(()), ())

    def test_read_totals_sums_beyond_sqlite_integer_range_exactly(self) -> None:
        """Переполнение SQL SUM не ограничивает итог нескольких обращений."""
        for count in (MAX_USAGE_INTEGER, MAX_USAGE_INTEGER, 1):
            self.assertTrue(self.repository.store(_priced_usage(count)))
        self.assertEqual(
            self.repository.read_totals((self.period,)), (UsageTotals(2 * MAX_USAGE_INTEGER + 1, 0),),
        )

    def test_read_totals_uses_one_snapshot_when_new_call_arrives(self) -> None:
        """Запись между чтением двух интервалов видна только в следующем отчёте."""
        self.assertTrue(self.repository.store(_priced_usage(2)))
        next_usage = _priced_usage(7)
        writer = self.repository

        class InterleavedUsageRepository(SQLiteUsageRepository):
            """Вставляет обращение через второе соединение после первого чтения."""

            def _read_period(self, connection: sqlite3.Connection, period: UsagePeriod) -> UsageTotals:
                result = super()._read_period(connection, period)
                writer.store(next_usage)
                return result

        interleaved = InterleavedUsageRepository(self.database_path)
        self.assertEqual(interleaved.read_totals((self.period, self.period)), (UsageTotals(2, 0), UsageTotals(2, 0)))
        self.assertEqual(self.repository.read_totals((self.period,)), (UsageTotals(9, 0),))

    def test_concurrent_duplicate_stores_one_estimate(self) -> None:
        """Два независимых соединения не удваивают расходы по общему ключу."""
        first = _priced_usage(2)
        second = UsagePricing((replace(_TARIFF, prompt_rate=3),)).estimate(first.observation)
        barrier = Barrier(2, timeout=5)

        def store(estimate: UsageEstimate) -> bool:
            barrier.wait()
            return SQLiteUsageRepository(self.database_path).store(estimate)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(store, (first, second)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        expected = 2 if results[0] else 6
        self.assertEqual(self.repository.read_totals((self.period,)), (UsageTotals(expected, 0),))

    def test_failed_write_rolls_back_and_can_retry(self) -> None:
        """RAISE(FAIL) после вставки оставляет откат транзакции репозиторию."""
        estimate = _priced_usage(9)
        with connect_database(self.database_path) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_usage_write AFTER INSERT ON ask_usage BEGIN "
                "SELECT RAISE(FAIL, 'forced failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.store(estimate)
        self.assertEqual(self.repository.read_totals((self.period,)), (UsageTotals(0, 0),))
        with connect_database(self.database_path) as connection, connection:
            connection.execute("DROP TRIGGER fail_usage_write")
        self.assertTrue(self.repository.store(estimate))
        self.assertEqual(self.repository.read_totals((self.period,)), (UsageTotals(9, 0),))

    def test_schema_rejects_inconsistent_cost_fields(self) -> None:
        """Ограничения базы защищают сумму и причину при внешней записи."""
        with connect_database(self.database_path) as connection:
            for cost, tariff, reason in (
                (-1, "{}", None), (0.5, "{}", None), (1, None, None), (1, "{}", "missing"),
                (None, None, None), (None, None, ""),
            ):
                with self.subTest(cost=cost, tariff=tariff, reason=reason), self.assertRaises(sqlite3.IntegrityError):
                    with connection:
                        connection.execute(
                            "INSERT INTO ask_usage (call_id, started_at_us, provider, "
                            "cost_nanorubles, tariff_json, unknown_reason) VALUES (?, 0, 'timeweb', ?, ?, ?)",
                            (str(uuid4()), cost, tariff, reason),
                        )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ask_usage").fetchone()[0], 0)

    def test_previous_schema_keeps_profiles_articles_modes_and_preferences(self) -> None:
        """Миграция 0005 добавляет учёт без изменения данных прежней версии."""
        with TemporaryDirectory(prefix="ask-usage-upgrade-") as directory:
            root = Path(directory)
            migrations = root / "migrations"
            migrations.mkdir()
            for version in range(1, 5):
                source = next(MIGRATIONS_DIR.glob(f"{version:04d}_*.sql"))
                (migrations / source.name).write_bytes(source.read_bytes())
            database_path = root / "previous.db"
            with connect_database(database_path) as connection:
                self.assertEqual([migration.version for migration in apply_migrations(connection, migrations)], [1, 2, 3, 4])
                with connection:
                    connection.execute("INSERT INTO app_users (id, display_name) VALUES (1, 'Fixture')")
                    connection.execute("INSERT INTO active_chat_modes VALUES (1, 'vk', '42', 'ask', 2, 'timestamp')")
                    connection.execute("INSERT INTO user_capability_preferences VALUES (1, 'ask', 0)")
                    connection.execute(
                        "INSERT INTO articles (app_user_id, source_url, normalized_url) "
                        "VALUES (1, 'https://example.com', 'https://example.com')"
                    )
                tables = ("app_users", "active_chat_modes", "user_capability_preferences", "articles")
                before = [tuple(connection.execute(f"SELECT * FROM {table}").fetchone()) for table in tables]
                self.assertEqual([migration.version for migration in apply_migrations(connection)], [5])
                self.assertEqual(apply_migrations(connection), [])
                self.assertEqual(before, [tuple(connection.execute(f"SELECT * FROM {table}").fetchone()) for table in tables])
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            repository = SQLiteUsageRepository(database_path)
            self.assertEqual(repository.read_totals((self.period,)), (UsageTotals(0, 0),))
            self.assertTrue(repository.store(_priced_usage(2)))
            self.assertEqual(repository.read_totals((self.period,)), (UsageTotals(2, 0),))
