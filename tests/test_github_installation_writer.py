"""Тест потокобезопасной записи GitHub installations."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_writer import (
    SQLiteGitHubAccountAccessWriter,
)
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from tests.sqlite_helpers import ensure_app_user


class GitHubInstallationWriterTest(unittest.TestCase):
    """Проверяет отдельное SQLite-соединение background worker."""

    def test_writer_opens_fresh_connection_and_persists_only_ids(self) -> None:
        """Writer не переносит user token в SQLite repository."""
        with TemporaryDirectory(prefix="compadre-assistant-github-writer-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
                ensure_app_user(connection)

            SQLiteGitHubAccountAccessWriter(database_path).replace_for_user(
                app_user_id=1,
                github_user_id=777,
                login="octocat",
                installation_ids={101, 102},
            )

            with connect_database(database_path) as connection:
                installations = SQLiteGitHubInstallationRepository(
                    connection
                ).list_for_user(1)
                account = connection.execute(
                    "SELECT github_user_id, login FROM github_accounts "
                    "WHERE app_user_id = 1"
                ).fetchone()

        self.assertEqual(
            [installation.installation_id for installation in installations],
            [101, 102],
        )
        self.assertEqual(tuple(account), (777, "octocat"))


if __name__ == "__main__":
    unittest.main()
