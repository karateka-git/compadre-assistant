"""Атомарные операции общей конфигурации vault в SQLite."""

from datetime import datetime
import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.pending import (
    PendingOperationError,
    VaultResourceBusyError,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import VaultActionConfirmationRepository
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import (
    VaultActionConfirmation,
    VaultConfirmationAction,
    ObsidianVault,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import (
    format_utc_timestamp,
    vault_confirmation_from_row,
    vault_confirmation_dto_from_domain,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
    _vault_values,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_process_lock import vault_process_lock


class SQLiteVaultActionConfirmationRepository(VaultActionConfirmationRepository):
    """Резервирует конфигурацию и атомарно применяет адресные решения."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(
        self, confirmation: VaultActionConfirmation, *, now: datetime
    ) -> VaultActionConfirmation:
        """Сохраняет новый ID, исключая подмену любого ещё активного ожидания.

        Args:
            confirmation: Новая операция с исходным адресом и ID активного vault.
            now: UTC-время для проверки TTL прежнего ожидания.

        Returns:
            Ожидание с неизменным ID, назначенным SQLite.

        Raises:
            VaultResourceBusyError: Другая операция уже резервирует конфигурацию.
            PendingOperationError: Исходная конфигурация vault уже изменилась.
            ValueError: Передана сохранённая или завершённая операция.
        """
        if (
            confirmation.id is not None
            or confirmation.version != 1
            or confirmation.status != "pending"
        ):
            raise ValueError("new configuration operation must be pending without id")
        dto = vault_confirmation_dto_from_domain(confirmation)
        try:
            with self._connection:
                self._connection.execute(
                    "UPDATE obsidian_vault_confirmations SET status = 'cancelled', version = version + 1 WHERE app_user_id = ? AND status = 'pending' AND expires_at <= ?",
                    (confirmation.app_user_id, format_utc_timestamp(now)),
                )
                current = SQLiteObsidianVaultRepository(self._connection).get_for_user(
                    confirmation.app_user_id
                )
                if current is None or current.id != confirmation.base_vault_id:
                    raise PendingOperationError("Конфигурация vault изменилась. Повтори запрос.")
                cursor = self._connection.execute(
                    """INSERT INTO obsidian_vault_confirmations
                    (app_user_id, origin_channel, origin_chat_id, base_vault_id, action,
                     installation_id, repository_id, owner, repository, branch, root_path, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dto.app_user_id,
                        dto.origin_channel,
                        dto.origin_chat_id,
                        dto.base_vault_id,
                        dto.action,
                        dto.installation_id,
                        dto.repository_id,
                        dto.owner,
                        dto.repository,
                        dto.branch,
                        dto.root_path,
                        dto.expires_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if self.find_active(app_user_id=confirmation.app_user_id, now=now) is not None:
                raise VaultResourceBusyError(
                    "Конфигурация vault уже ожидает решения в исходном чате."
                ) from error
            raise
        saved = self.get_by_id(cursor.lastrowid, confirmation.app_user_id)
        if saved is None:
            raise RuntimeError("Saved vault operation disappeared")
        return saved

    def find_active(self, *, app_user_id: int, now: datetime) -> VaultActionConfirmation | None:
        """Читает активную резервацию конфигурации, не раскрывая её presentation автоматически."""
        row = self._connection.execute(
            "SELECT * FROM obsidian_vault_confirmations WHERE app_user_id = ? AND status = 'pending' AND expires_at > ?",
            (app_user_id, format_utc_timestamp(now)),
        ).fetchone()
        return vault_confirmation_from_row(row) if row else None

    def get_by_id(self, confirmation_id: int, app_user_id: int) -> VaultActionConfirmation | None:
        """Возвращает конкретную операцию её внутреннему владельцу."""
        row = self._connection.execute(
            "SELECT * FROM obsidian_vault_confirmations WHERE id = ? AND app_user_id = ?",
            (confirmation_id, app_user_id),
        ).fetchone()
        return vault_confirmation_from_row(row) if row else None

    def decide(
        self, confirmation: VaultActionConfirmation, *, approve: bool, now: datetime
    ) -> ObsidianVault | None:
        """Применяет решение под тем же межпроцессным lock, что и write-back.

        Args:
            confirmation: Подтверждаемая замена или отключение vault.
            approve: Применить изменение конфигурации.
            now: Время проверки TTL и занятого lease.

        Returns:
            Актуальный vault либо `None`, если изменение его удалило.

        Raises:
            VaultResourceBusyError: Внешняя запись удерживает lock vault.
            PendingOperationError: Ожидание или конфигурация уже изменились.
        """
        with vault_process_lock(
            self._connection, confirmation.base_vault_id, blocking=False,
        ) as locked:
            if not locked:
                raise VaultResourceBusyError(
                    "Vault занят синхронизацией или записью. Повтори решение позже."
                )
            return self._decide_locked(confirmation, approve=approve, now=now)

    def _decide_locked(
        self, confirmation: VaultActionConfirmation, *, approve: bool, now: datetime
    ) -> ObsidianVault | None:
        """Одной транзакцией проверяет CAS и меняет vault вместе со статусом решения.

        Args:
            confirmation: Сохранённые ID, адрес, действие и показанная версия.
            approve: Применить изменение либо отменить ожидание.
            now: Текущее UTC-время для проверки TTL и занятости lease.

        Returns:
            Новый vault при замене; прежний vault при отмене или отключении.

        Raises:
            PendingOperationError: Ожидание или исходная конфигурация уже изменились.
            VaultResourceBusyError: Sync/write lease ещё занят.
        """
        with self._connection:
            # Write lock берётся до чтения vault/lease, чтобы sync не вклинился в решение.
            cursor = self._connection.execute(
                """UPDATE obsidian_vault_confirmations SET status = ?, version = version + 1
                WHERE id = ? AND app_user_id = ? AND origin_channel = ? AND origin_chat_id = ?
                    AND status = 'pending' AND version = ? AND action = ? AND expires_at > ?""",
                (
                    "applied" if approve else "cancelled",
                    confirmation.id,
                    confirmation.app_user_id,
                    confirmation.origin.channel,
                    confirmation.origin.chat_id,
                    confirmation.version,
                    confirmation.action.value,
                    format_utc_timestamp(now),
                ),
            )
            if cursor.rowcount != 1:
                raise PendingOperationError("Ожидание завершено, устарело или истёк срок действия.")
            stored = self.get_by_id(confirmation.id or 0, confirmation.app_user_id)
            if stored is None:
                raise RuntimeError("Claimed configuration operation disappeared")
            repository = SQLiteObsidianVaultRepository(self._connection)
            current = repository.get_for_user(confirmation.app_user_id)
            if current is None or current.id != stored.base_vault_id:
                raise PendingOperationError("Конфигурация vault уже изменилась.")
            if not approve:
                return current
            busy = self._connection.execute(
                "SELECT 1 FROM obsidian_vault_sync_leases WHERE app_user_id = ? AND vault_id = ? AND expires_at > ?",
                (confirmation.app_user_id, current.id, format_utc_timestamp(now)),
            ).fetchone()
            if busy:
                raise VaultResourceBusyError(
                    "Vault занят синхронизацией или записью. Повтори решение позже."
                )
            self._connection.execute(
                "DELETE FROM obsidian_vaults WHERE id = ? AND app_user_id = ?",
                (current.id, confirmation.app_user_id),
            )
            if confirmation.action is VaultConfirmationAction.DISCONNECT:
                return current
            # Записывается сохранённый preview, а не переданный извне экземпляр.
            candidate = stored.replacement
            if candidate is None:
                raise ValueError("replacement is required")
            cursor = self._connection.execute(
                """INSERT INTO obsidian_vaults (app_user_id, installation_id, repository_id, owner,
                repository, branch, root_path, head_commit_sha, tree_sha, head_etag, last_checked_at, last_synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _vault_values(candidate),
            )
            return repository.get_by_id(
                app_user_id=confirmation.app_user_id, vault_id=cursor.lastrowid
            )
