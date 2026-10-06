from __future__ import annotations

import sqlite3
from datetime import datetime

from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import VaultActionConfirmationRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import (
    format_utc_timestamp,
    vault_confirmation_from_row,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultActionConfirmation


class SQLiteVaultActionConfirmationRepository(VaultActionConfirmationRepository):
    """Хранит ожидающее подтверждение действия над vault в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, confirmation: VaultActionConfirmation) -> None:
        """Сохраняет последнее ожидающее действие пользователя."""
        replacement = confirmation.replacement
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO obsidian_vault_confirmations (
                    app_user_id,
                    action,
                    installation_id,
                    repository_id,
                    owner,
                    repository,
                    branch,
                    root_path,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_user_id) DO UPDATE SET
                    action = excluded.action,
                    installation_id = excluded.installation_id,
                    repository_id = excluded.repository_id,
                    owner = excluded.owner,
                    repository = excluded.repository,
                    branch = excluded.branch,
                    root_path = excluded.root_path,
                    expires_at = excluded.expires_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    confirmation.app_user_id,
                    confirmation.action.value,
                    replacement.installation_id if replacement is not None else None,
                    replacement.repository_id if replacement is not None else None,
                    replacement.owner if replacement is not None else None,
                    replacement.repository if replacement is not None else None,
                    replacement.branch if replacement is not None else None,
                    replacement.root_path if replacement is not None else None,
                    format_utc_timestamp(confirmation.expires_at),
                ),
            )

    def find_active(
        self,
        *,
        app_user_id: int,
        now: datetime,
    ) -> VaultActionConfirmation | None:
        """Возвращает неистёкшее подтверждение пользователя."""
        row = self._connection.execute(
            """
            SELECT
                app_user_id,
                action,
                installation_id,
                repository_id,
                owner,
                repository,
                branch,
                root_path,
                expires_at,
                created_at
            FROM obsidian_vault_confirmations
            WHERE app_user_id = ? AND expires_at > ?
            """,
            (app_user_id, format_utc_timestamp(now)),
        ).fetchone()
        return vault_confirmation_from_row(row) if row is not None else None

    def delete(self, app_user_id: int) -> None:
        """Удаляет ожидающее подтверждение пользователя."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM obsidian_vault_confirmations WHERE app_user_id = ?",
                (app_user_id,),
            )
