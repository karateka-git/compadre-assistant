from __future__ import annotations

from datetime import datetime
from pathlib import Path

from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import GitHubConnectionStateStore
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import format_utc_timestamp


class SQLiteGitHubConnectionStateStore(GitHubConnectionStateStore):
    """Хранит межпроцессный Device Flow claim через короткие SQLite-соединения."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def acquire_attempt(
        self,
        *,
        app_user_id: int,
        owner: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        """Атомарно захватывает или отказывает в межпроцессном claim."""
        with connect_database(self._database_path) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM github_connection_attempts WHERE expires_at <= ?",
                    (format_utc_timestamp(now),),
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO github_connection_attempts (
                        app_user_id, owner, acquired_at, expires_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        app_user_id,
                        owner,
                        format_utc_timestamp(now),
                        format_utc_timestamp(expires_at),
                    ),
                )
        return cursor.rowcount == 1

    def has_active_attempt(self, *, app_user_id: int, now: datetime) -> bool:
        """Проверяет shared claim, попутно удаляя истёкшие claims."""
        with connect_database(self._database_path) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM github_connection_attempts WHERE expires_at <= ?",
                    (format_utc_timestamp(now),),
                )
                row = connection.execute(
                    """
                    SELECT 1 FROM github_connection_attempts WHERE app_user_id = ?
                    """,
                    (app_user_id,),
                ).fetchone()
        return row is not None

    def release_attempt(self, *, app_user_id: int, owner: str) -> None:
        """Освобождает shared claim только совпадающим владельцем."""
        with connect_database(self._database_path) as connection:
            with connection:
                connection.execute(
                    """
                    DELETE FROM github_connection_attempts
                    WHERE app_user_id = ? AND owner = ?
                    """,
                    (app_user_id, owner),
                )
