"""Тесты атомарного запуска SQLite migrations несколькими adapters."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
import compadre_assistant.core.data.sqlite.migration_runner as migration_runner


class MigrationRunnerTest(unittest.TestCase):
    """Проверяет межпроцессный алгоритм миграций на отдельных соединениях."""

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
