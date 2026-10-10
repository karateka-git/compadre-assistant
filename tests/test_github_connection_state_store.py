"""Интеграционные тесты общего SQLite-состояния подключения GitHub."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.capabilities.obsidian.data.sqlite.github_connection_state_store import (
    SQLiteGitHubConnectionStateStore,
)
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from tests.sqlite_helpers import ensure_app_user


class GitHubConnectionStateStoreTest(unittest.TestCase):
    """Проверяет подтверждение и межпроцессный claim через разные соединения."""

    def test_connection_attempt_is_shared(self) -> None:
        """Два экземпляра видят один межпроцессный claim Device Flow."""
        now = datetime.now(UTC)
        with TemporaryDirectory(prefix="compadre-assistant-github-state-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
                ensure_app_user(connection)
            first = SQLiteGitHubConnectionStateStore(database_path)
            second = SQLiteGitHubConnectionStateStore(database_path)

            acquired = first.acquire_attempt(
                app_user_id=1,
                owner="tg-catcher",
                now=now,
                expires_at=now + timedelta(minutes=20),
            )
            blocked = second.acquire_attempt(
                app_user_id=1,
                owner="vk-catcher",
                now=now,
                expires_at=now + timedelta(minutes=20),
            )
            second.release_attempt(app_user_id=1, owner="vk-catcher")

            self.assertTrue(acquired)
            self.assertFalse(blocked)
            self.assertTrue(second.has_active_attempt(app_user_id=1, now=now))

            first.release_attempt(app_user_id=1, owner="tg-catcher")
            self.assertFalse(second.has_active_attempt(app_user_id=1, now=now))

    def test_expired_attempt_can_be_restarted_after_process_loss(self) -> None:
        """Новый процесс повторно захватывает claim после истечения старого."""
        now = datetime.now(UTC)
        with TemporaryDirectory(prefix="compadre-assistant-github-restart-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
                ensure_app_user(connection)

            previous_process = SQLiteGitHubConnectionStateStore(database_path)
            self.assertTrue(
                previous_process.acquire_attempt(
                    app_user_id=1,
                    owner="old-process",
                    now=now,
                    expires_at=now + timedelta(minutes=20),
                )
            )

            restarted_process = SQLiteGitHubConnectionStateStore(database_path)
            self.assertFalse(
                restarted_process.acquire_attempt(
                    app_user_id=1,
                    owner="new-process",
                    now=now + timedelta(minutes=19),
                    expires_at=now + timedelta(minutes=39),
                )
            )
            self.assertTrue(
                restarted_process.acquire_attempt(
                    app_user_id=1,
                    owner="new-process",
                    now=now + timedelta(minutes=20),
                    expires_at=now + timedelta(minutes=40),
                )
            )
            previous_process.release_attempt(app_user_id=1, owner="old-process")
            self.assertTrue(
                restarted_process.has_active_attempt(
                    app_user_id=1,
                    now=now + timedelta(minutes=20),
                )
            )


if __name__ == "__main__":
    unittest.main()
