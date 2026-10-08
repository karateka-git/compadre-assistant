"""Атомарная проверка и продление vault lease перед публикацией SQLite-данных."""

from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.vaults.lease_scope import (
    VaultLeaseLostError,
    current_vault_lease,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import format_utc_timestamp


def fence_vault_write(connection: sqlite3.Connection, app_user_id: int, vault_id: int) -> None:
    """Проверяет и продлевает lease внутри той же транзакции, что и запись.

    Args:
        connection: Соединение транзакции, которая публикует состояние vault.
        app_user_id: Владелец vault.
        vault_id: Идентификатор vault.

    Raises:
        VaultLeaseLostError: Lease истёк, захвачен другим worker или scope не совпал.
    """
    scope = current_vault_lease()
    if scope is None:
        return
    if scope.app_user_id != app_user_id or scope.vault_id != vault_id:
        raise VaultLeaseLostError("Vault write escaped active lease scope")
    now = scope.clock()
    cursor = connection.execute(
        """UPDATE obsidian_vault_sync_leases SET expires_at = ?
           WHERE app_user_id = ? AND vault_id = ? AND owner = ? AND expires_at > ?""",
        (
            format_utc_timestamp(now + scope.duration),
            app_user_id,
            vault_id,
            scope.owner,
            format_utc_timestamp(now),
        ),
    )
    if cursor.rowcount != 1:
        raise VaultLeaseLostError("Vault sync lease expired or changed")
