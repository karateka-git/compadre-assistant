"""Тесты настройки общего SQLite-соединения."""

import sqlite3
import unittest
from unittest.mock import Mock, patch

from obs_chat_bot.core.data.sqlite.connection import _enable_wal


class SQLiteConnectionTest(unittest.TestCase):
    """Проверяет устойчивость настройки SQLite при параллельном старте."""

    @patch("obs_chat_bot.core.data.sqlite.connection.time.sleep")
    def test_enable_wal_retries_temporary_lock(self, sleep: Mock) -> None:
        """Повторяет WAL PRAGMA после кратковременной блокировки другой копией."""
        connection = Mock()
        cursor = Mock()
        cursor.fetchone.return_value = ("wal",)
        connection.execute.side_effect = [
            sqlite3.OperationalError("database is locked"),
            cursor,
        ]

        result = _enable_wal(connection)

        self.assertEqual(result, ("wal",))
        self.assertEqual(connection.execute.call_count, 2)
        sleep.assert_called_once()

    @patch("obs_chat_bot.core.data.sqlite.connection.time.sleep")
    def test_enable_wal_does_not_hide_other_errors(self, sleep: Mock) -> None:
        """Не повторяет ошибки SQLite, не связанные с конкурентным lock."""
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("disk I/O error")

        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            _enable_wal(connection)

        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
