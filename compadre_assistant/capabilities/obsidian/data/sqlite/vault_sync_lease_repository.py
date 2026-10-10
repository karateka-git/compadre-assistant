from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from collections.abc import Callable

from compadre_assistant.capabilities.obsidian.domain.vaults.ports import VaultSyncLeaseRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_mappers import (
    format_utc_timestamp,
    vault_sync_lease_from_row,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultSyncLease
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import GitHubVaultCommitResult
from compadre_assistant.capabilities.obsidian.domain.vaults.lease_scope import VaultLeaseLostError
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_process_lock import vault_process_lock


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
        """Атомарно захватывает lease под межпроцессным lock vault.

        Args:
            app_user_id: Владелец vault.
            vault_id: Общий ресурс sync/write-back.
            owner: Уникальный идентификатор текущей операции.
            now: Время проверки срока.
            expires_at: Новый срок владения.

        Returns:
            Захваченный lease либо `None` при занятом ресурсе.
        """
        requested = VaultSyncLease(
            app_user_id=app_user_id,
            vault_id=vault_id,
            owner=owner,
            acquired_at=now,
            expires_at=expires_at,
        )
        with vault_process_lock(self._connection, vault_id, blocking=False) as locked:
            if not locked:
                return None
            try:
                with self._connection:
                    cursor = self._connection.execute(
                        """
                        INSERT INTO obsidian_vault_sync_leases (
                            app_user_id, vault_id, owner, acquired_at, expires_at
                        )
                        SELECT ?, ?, ?, ?, ? WHERE EXISTS (
                            SELECT 1 FROM obsidian_vaults WHERE app_user_id = ? AND id = ?
                        )
                        ON CONFLICT(vault_id) DO UPDATE SET
                            owner = excluded.owner,
                            acquired_at = excluded.acquired_at,
                            expires_at = excluded.expires_at
                        WHERE obsidian_vault_sync_leases.app_user_id = excluded.app_user_id
                          AND (obsidian_vault_sync_leases.owner = excluded.owner
                               OR obsidian_vault_sync_leases.expires_at <= excluded.acquired_at)
                        """,
                        (
                            requested.app_user_id, requested.vault_id, requested.owner,
                            format_utc_timestamp(now), format_utc_timestamp(expires_at),
                            app_user_id, vault_id,
                        ),
                    )
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower():
                    raise
                return None
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

    def run_exclusive(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        owner: str,
        clock: Callable[[], datetime],
        duration: timedelta,
        operation: Callable[[], GitHubVaultCommitResult],
    ) -> GitHubVaultCommitResult:
        """Удерживает lock конкретного vault до завершения удалённой записи.

        Захват другого worker невозможен между последней проверкой и HTTP commit.
        После сетевого вызова срок продлевается до освобождения OS lock.

        Args:
            app_user_id: Владелец vault.
            vault_id: Ресурс, который исключается между процессами.
            owner: Идентификатор ранее захваченного lease.
            clock: Часы для проверки и продления срока.
            duration: Срок следующего окна владения.
            operation: Единственный внешний commit внутри lock.

        Returns:
            Результат внешнего commit.

        Raises:
            VaultLeaseLostError: Lease уже утрачен до начала внешней записи.
        """
        with vault_process_lock(self._connection, vault_id):
            with self._connection:
                current = clock()
                cursor = self._connection.execute(
                    """UPDATE obsidian_vault_sync_leases SET expires_at = ?
                       WHERE app_user_id = ? AND vault_id = ? AND owner = ?
                         AND expires_at > ?""",
                    (
                        format_utc_timestamp(current + duration), app_user_id,
                        vault_id, owner, format_utc_timestamp(current),
                    ),
                )
                if cursor.rowcount != 1:
                    raise VaultLeaseLostError("Vault sync lease expired or changed before commit")
            result = operation()
            with self._connection:
                cursor = self._connection.execute(
                    """UPDATE obsidian_vault_sync_leases SET expires_at = ?
                       WHERE app_user_id = ? AND vault_id = ? AND owner = ?""",
                    (format_utc_timestamp(clock() + duration), app_user_id, vault_id, owner),
                )
                if cursor.rowcount != 1:
                    raise VaultLeaseLostError("Vault sync lease changed during commit")
            return result
