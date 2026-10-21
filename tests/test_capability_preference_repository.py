"""Проверяет SQLite-настройки через независимые соединения."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import sqlite3
import unittest

from compadre_assistant.core.data.sqlite.capability_preference_repository import (
    SQLiteUserCapabilityPreferenceRepository,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.capability_preferences import (
    CapabilityDefault,
    GetUserCapabilitySelections,
    SetUserCapabilityPreference,
    UserCapabilityPreference,
)


class CapabilityPreferenceRepositoryTest(unittest.TestCase):
    """Проверяет устойчивость выбора и атомарность отдельного ключа."""

    def test_concurrent_distinct_ids_and_new_connection(self) -> None:
        """Конкурентные записи разных ID сохраняются независимо после reconnect."""
        with TemporaryDirectory(prefix="capability-preferences-") as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as connection:
                apply_migrations(connection)
                connection.execute("INSERT INTO app_users (id) VALUES (1)")
                connection.execute("INSERT INTO app_users (id) VALUES (2)")
                connection.commit()

            barrier = Barrier(2)

            def save(capability_id: str, enabled: bool) -> bool | None:
                """Открывает собственное соединение перед синхронным стартом записи."""
                with connect_database(path) as connection:
                    repository = SQLiteUserCapabilityPreferenceRepository(connection)
                    barrier.wait(timeout=5)
                    return repository.upsert(UserCapabilityPreference(1, capability_id, enabled))

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(save, "ask", False),
                    executor.submit(save, "obsidian", True),
                )
                self.assertEqual([future.result(timeout=10) for future in futures], [None, None])

            with connect_database(path) as connection:
                repository = SQLiteUserCapabilityPreferenceRepository(connection)
                defaults = (CapabilityDefault("ask", True), CapabilityDefault("obsidian"))
                selected = GetUserCapabilitySelections(repository, defaults).execute(1)
                self.assertEqual(
                    [(item.capability_id, item.enabled, item.explicit) for item in selected],
                    [("ask", False, True), ("obsidian", True, True)],
                )
                self.assertEqual(repository.get_for_user(2), ())
                self.assertEqual(
                    [(item.enabled, item.explicit) for item in GetUserCapabilitySelections(repository, defaults).execute(2)],
                    [(True, False), (False, False)],
                )

    def test_same_id_last_commit_and_write_error(self) -> None:
        """Последний commit одного ID действует; отказ SQLite не выглядит успехом."""
        with TemporaryDirectory(prefix="capability-preferences-") as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as connection:
                apply_migrations(connection)
                connection.execute("INSERT INTO app_users (id) VALUES (1)")
                connection.commit()
                repository = SQLiteUserCapabilityPreferenceRepository(connection)
                writer = SetUserCapabilityPreference(repository, (CapabilityDefault("ask", True),))
                self.assertTrue(writer.execute(1, "ask", False))
                self.assertFalse(writer.execute(1, "ask", False))
            with connect_database(path) as connection:
                repository = SQLiteUserCapabilityPreferenceRepository(connection)
                self.assertFalse(repository.get_for_user(1)[0].enabled)
                self.assertEqual(repository.upsert(UserCapabilityPreference(1, "ask", True)), False)
                with self.assertRaises(sqlite3.IntegrityError):
                    repository.upsert(UserCapabilityPreference(999, "ask", True))
            with connect_database(path) as connection:
                repository = SQLiteUserCapabilityPreferenceRepository(connection)
                self.assertTrue(repository.get_for_user(1)[0].enabled)
                self.assertEqual(len(repository.get_for_user(1)), 1)


if __name__ == "__main__":
    unittest.main()
