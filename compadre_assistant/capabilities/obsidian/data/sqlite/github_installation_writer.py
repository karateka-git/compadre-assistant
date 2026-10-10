from __future__ import annotations

from pathlib import Path

from compadre_assistant.capabilities.obsidian.domain.vaults.ports import GitHubAccountAccessWriter
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import GitHubAccount, GitHubInstallation


class SQLiteGitHubAccountAccessWriter(GitHubAccountAccessWriter):
    """Атомарно заменяет GitHub-аккаунт и доступы из background thread."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def replace_for_user(
        self,
        *,
        app_user_id: int,
        github_user_id: int,
        login: str,
        installation_ids: set[int],
    ) -> None:
        """Сохраняет account и installations отдельным SQLite-соединением."""
        GitHubAccount(
            app_user_id=app_user_id,
            github_user_id=github_user_id,
            login=login,
        )
        for installation_id in installation_ids:
            GitHubInstallation(
                app_user_id=app_user_id,
                installation_id=installation_id,
            )
        with connect_database(self._database_path) as connection:
            with connection:
                current = connection.execute(
                    "SELECT github_user_id FROM github_accounts WHERE app_user_id = ?",
                    (app_user_id,),
                ).fetchone()
                if current is not None and current["github_user_id"] != github_user_id:
                    connection.execute(
                        "DELETE FROM github_installations WHERE app_user_id = ?",
                        (app_user_id,),
                    )
                connection.execute(
                    """
                    INSERT INTO github_accounts (
                        app_user_id, github_user_id, login
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT (app_user_id) DO UPDATE SET
                        github_user_id = excluded.github_user_id,
                        login = excluded.login,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (app_user_id, github_user_id, login),
                )
                if installation_ids:
                    ordered_ids = sorted(installation_ids)
                    placeholders = ", ".join("?" for _ in ordered_ids)
                    connection.execute(
                        f"""
                        DELETE FROM github_installations
                        WHERE app_user_id = ?
                            AND installation_id NOT IN ({placeholders})
                        """,
                        (app_user_id, *ordered_ids),
                    )
                else:
                    connection.execute(
                        "DELETE FROM github_installations WHERE app_user_id = ?",
                        (app_user_id,),
                    )
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO github_installations (
                        app_user_id, installation_id
                    )
                    VALUES (?, ?)
                    """,
                    (
                        (app_user_id, installation_id)
                        for installation_id in sorted(installation_ids)
                    ),
                )
