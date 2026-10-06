from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import GitHubInstallationRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import github_installation_from_row
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import GitHubInstallation


class SQLiteGitHubInstallationRepository(GitHubInstallationRepository):
    """Хранит разрешённые пользователю installation IDs в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def replace_for_user(
        self,
        *,
        app_user_id: int,
        installation_ids: set[int],
    ) -> list[GitHubInstallation]:
        """Синхронизирует разрешённые installation IDs пользователя.

        Существующие разрешённые строки не пересоздаются, чтобы не удалить
        каскадно vault, если его installation по-прежнему доступна.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        for installation_id in installation_ids:
            GitHubInstallation(
                app_user_id=app_user_id,
                installation_id=installation_id,
            )

        with self._connection:
            if installation_ids:
                ordered_ids = sorted(installation_ids)
                placeholders = ", ".join("?" for _ in ordered_ids)
                self._connection.execute(
                    f"""
                    DELETE FROM github_installations
                    WHERE app_user_id = ?
                        AND installation_id NOT IN ({placeholders})
                    """,
                    (app_user_id, *ordered_ids),
                )
            else:
                self._connection.execute(
                    "DELETE FROM github_installations WHERE app_user_id = ?",
                    (app_user_id,),
                )
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO github_installations (
                    app_user_id,
                    installation_id
                )
                VALUES (?, ?)
                """,
                (
                    (app_user_id, installation_id)
                    for installation_id in sorted(installation_ids)
                ),
            )

        return self.list_for_user(app_user_id)

    def list_for_user(self, app_user_id: int) -> list[GitHubInstallation]:
        """Возвращает разрешённые установки пользователя по возрастанию ID."""
        rows = self._connection.execute(
            """
            SELECT app_user_id, installation_id, created_at
            FROM github_installations
            WHERE app_user_id = ?
            ORDER BY installation_id
            """,
            (app_user_id,),
        ).fetchall()
        return [github_installation_from_row(row) for row in rows]

    def contains(self, *, app_user_id: int, installation_id: int) -> bool:
        """Проверяет доступ пользователя к installation ID."""
        row = self._connection.execute(
            """
            SELECT 1
            FROM github_installations
            WHERE app_user_id = ? AND installation_id = ?
            """,
            (app_user_id, installation_id),
        ).fetchone()
        return row is not None

    def delete_for_user(self, app_user_id: int) -> None:
        """Удаляет установки пользователя и связанные vault-данные."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM github_installations WHERE app_user_id = ?",
                (app_user_id,),
            )
