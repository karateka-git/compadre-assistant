from __future__ import annotations

import sqlite3
from datetime import datetime

from obs_chat_bot.capabilities.obsidian.application.vaults.ports import VaultSyncLeaseRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import (
    format_utc_timestamp,
    vault_sync_lease_from_row,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultSyncLease


class SQLiteVaultSyncLeaseRepository(VaultSyncLeaseRepository):
    """Координирует синхронизацию Telegram/VK процессов через SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def acquire(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        owner: str,
        now: datetime,
        expires_at: datetime,
    ) -> VaultSyncLease | None:
        """Атомарно захватывает свободный, свой или истёкший lease."""
        requested = VaultSyncLease(
            app_user_id=app_user_id,
            vault_id=vault_id,
            owner=owner,
            acquired_at=now,
            expires_at=expires_at,
        )
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO obsidian_vault_sync_leases (
                    app_user_id,
                    vault_id,
                    owner,
                    acquired_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(vault_id) DO UPDATE SET
                    owner = excluded.owner,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                WHERE
                    obsidian_vault_sync_leases.app_user_id = excluded.app_user_id
                    AND (
                        obsidian_vault_sync_leases.owner = excluded.owner
                        OR obsidian_vault_sync_leases.expires_at
                            <= excluded.acquired_at
                    )
                """,
                (
                    requested.app_user_id,
                    requested.vault_id,
                    requested.owner,
                    format_utc_timestamp(now),
                    format_utc_timestamp(expires_at),
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self.get(app_user_id=app_user_id, vault_id=vault_id)

    def get(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> VaultSyncLease | None:
        """Возвращает текущий lease vault, включая истёкший."""
        row = self._connection.execute(
            """
            SELECT app_user_id, vault_id, owner, acquired_at, expires_at
            FROM obsidian_vault_sync_leases
            WHERE app_user_id = ? AND vault_id = ?
            """,
            (app_user_id, vault_id),
        ).fetchone()
        return vault_sync_lease_from_row(row) if row is not None else None

    def release(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        owner: str,
    ) -> bool:
        """Освобождает lease только при совпадении пользователя и владельца."""
        with self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM obsidian_vault_sync_leases
                WHERE app_user_id = ? AND vault_id = ? AND owner = ?
                """,
                (app_user_id, vault_id, owner),
            )
        return cursor.rowcount == 1
