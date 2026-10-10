"""Тесты постоянного SQLite smoke-сценария."""

import unittest

from compadre_assistant.capabilities.obsidian.smoke import run_sqlite_smoke


class SQLiteSmokeTest(unittest.TestCase):
    """Проверяет SQLite-контур на временной базе."""

    def test_sqlite_smoke_succeeds(self) -> None:
        """Smoke-сценарий завершается без ошибок."""
        run_sqlite_smoke()


if __name__ == "__main__":
    unittest.main()
