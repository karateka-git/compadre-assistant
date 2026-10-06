from __future__ import annotations

import sqlite3
from datetime import datetime

from obs_chat_bot.capabilities.obsidian.application.vaults.ports import ObsidianVaultRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import (
    format_utc_timestamp,
    obsidian_vault_from_row,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault


VAULT_COLUMNS = """
    id,
    app_user_id,
    installation_id,
    repository_id,
    owner,
    repository,
    branch,
    root_path,
    head_commit_sha,
    tree_sha,
    head_etag,
    last_checked_at,
    last_synced_at,
    created_at,
    updated_at
"""


class SQLiteObsidianVaultRepository(ObsidianVaultRepository):
    """Хранит единственное активное подключение Obsidian vault в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create_if_absent(self, vault: ObsidianVault) -> ObsidianVault | None:
        """Атомарно создаёт первый vault пользователя без замены существующего."""
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO obsidian_vaults (
                    app_user_id,
                    installation_id,
                    repository_id,
                    owner,
                    repository,
                    branch,
                    root_path,
                    head_commit_sha,
                    tree_sha,
                    head_etag,
                    last_checked_at,
                    last_synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _vault_values(vault),
            )
        if cursor.rowcount == 0:
            return None
        saved = self.get_for_user(vault.app_user_id)
        if saved is None:
            raise RuntimeError("Saved Obsidian vault could not be read")
        return saved

    def replace(self, vault: ObsidianVault) -> ObsidianVault:
        """Заменяет vault пользователя в одной SQLite-транзакции."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM obsidian_vaults WHERE app_user_id = ?",
                (vault.app_user_id,),
            )
            cursor = self._connection.execute(
                """
                INSERT INTO obsidian_vaults (
                    app_user_id,
                    installation_id,
                    repository_id,
                    owner,
                    repository,
                    branch,
                    root_path,
                    head_commit_sha,
                    tree_sha,
                    head_etag,
                    last_checked_at,
                    last_synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _vault_values(vault),
            )
            vault_id = cursor.lastrowid

        saved = self.get_by_id(app_user_id=vault.app_user_id, vault_id=vault_id)
        if saved is None:
            raise RuntimeError("Saved Obsidian vault could not be read")
        return saved

    def get_by_id(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> ObsidianVault | None:
        """Возвращает vault по ID только внутри области пользователя."""
        row = self._connection.execute(
            f"""
            SELECT {VAULT_COLUMNS}
            FROM obsidian_vaults
            WHERE app_user_id = ? AND id = ?
            """,
            (app_user_id, vault_id),
        ).fetchone()
        return obsidian_vault_from_row(row) if row is not None else None

    def get_for_user(self, app_user_id: int) -> ObsidianVault | None:
        """Возвращает активный vault пользователя или `None`."""
        row = self._connection.execute(
            f"""
            SELECT {VAULT_COLUMNS}
            FROM obsidian_vaults
            WHERE app_user_id = ?
            """,
            (app_user_id,),
        ).fetchone()
        return obsidian_vault_from_row(row) if row is not None else None

    def update_sync_state(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        head_commit_sha: str | None,
        tree_sha: str | None,
        head_etag: str | None,
        last_checked_at: datetime,
        last_synced_at: datetime | None,
    ) -> ObsidianVault | None:
        """Обновляет source SHA, ETag и timestamps проверки vault.

        Значение `None` сохраняет предыдущее source-состояние. Это позволяет
        записать не изменившую vault проверку, не очищая последний успешный SHA.
        """
        for value, name in (
            (head_commit_sha, "head_commit_sha"),
            (tree_sha, "tree_sha"),
            (head_etag, "head_etag"),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be empty")

        with self._connection:
            self._connection.execute(
                """
                UPDATE obsidian_vaults
                SET
                    head_commit_sha = COALESCE(?, head_commit_sha),
                    tree_sha = COALESCE(?, tree_sha),
                    head_etag = COALESCE(?, head_etag),
                    last_checked_at = ?,
                    last_synced_at = COALESCE(?, last_synced_at),
                    updated_at = CURRENT_TIMESTAMP
                WHERE app_user_id = ? AND id = ?
                """,
                (
                    head_commit_sha,
                    tree_sha,
                    head_etag,
                    format_utc_timestamp(last_checked_at),
                    _format_optional_timestamp(last_synced_at),
                    app_user_id,
                    vault_id,
                ),
            )
        return self.get_by_id(app_user_id=app_user_id, vault_id=vault_id)

    def delete_for_user(self, app_user_id: int) -> None:
        """Удаляет vault пользователя и все зависимые заметки и lease."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM obsidian_vaults WHERE app_user_id = ?",
                (app_user_id,),
            )


def _format_optional_timestamp(value: datetime | None) -> str | None:
    return format_utc_timestamp(value) if value is not None else None


def _vault_values(vault: ObsidianVault) -> tuple[object, ...]:
    return (
        vault.app_user_id,
        vault.installation_id,
        vault.repository_id,
        vault.owner,
        vault.repository,
        vault.branch,
        vault.root_path,
        vault.head_commit_sha,
        vault.tree_sha,
        vault.head_etag,
        _format_optional_timestamp(vault.last_checked_at),
        _format_optional_timestamp(vault.last_synced_at),
    )
