from __future__ import annotations

import sqlite3

from compadre_assistant.capabilities.obsidian.domain.vaults.ports import VaultInstructionRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_mappers import (
    vault_instruction_dto_from_row,
    vault_instruction_from_dto,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultInstruction
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_lease_fence import fence_vault_write


INSTRUCTION_COLUMNS = """
    id,
    app_user_id,
    vault_id,
    position,
    path,
    blob_sha,
    content,
    created_at,
    updated_at
"""


class SQLiteVaultInstructionRepository(VaultInstructionRepository):
    """Хранит обязательные instruction-файлы отдельно от Obsidian-заметок."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def replace_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        instructions: tuple[VaultInstruction, ...],
    ) -> list[VaultInstruction]:
        """Атомарно заменяет полный упорядоченный набор instruction-файлов."""
        if any(
            instruction.app_user_id != app_user_id
            or instruction.vault_id != vault_id
            for instruction in instructions
        ):
            raise ValueError("instruction does not belong to requested vault")
        with self._connection:
            fence_vault_write(self._connection, app_user_id, vault_id)
            vault_row = self._connection.execute(
                """
                SELECT 1
                FROM obsidian_vaults
                WHERE app_user_id = ? AND id = ?
                """,
                (app_user_id, vault_id),
            ).fetchone()
            if vault_row is None:
                raise ValueError("vault does not belong to app_user_id")
            self._connection.execute(
                """
                DELETE FROM obsidian_vault_instructions
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            )
            self._connection.executemany(
                """
                INSERT INTO obsidian_vault_instructions (
                    app_user_id,
                    vault_id,
                    position,
                    path,
                    blob_sha,
                    content
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        instruction.app_user_id,
                        instruction.vault_id,
                        instruction.position,
                        instruction.path,
                        instruction.blob_sha,
                        instruction.content,
                    )
                    for instruction in instructions
                ),
            )
        return self.list_for_vault(app_user_id=app_user_id, vault_id=vault_id)

    def list_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultInstruction]:
        """Возвращает instruction-файлы в порядке конфигурации."""
        rows = self._connection.execute(
            f"""
            SELECT {INSTRUCTION_COLUMNS}
            FROM obsidian_vault_instructions
            WHERE app_user_id = ? AND vault_id = ?
            ORDER BY position
            """,
            (app_user_id, vault_id),
        ).fetchall()
        return [
            vault_instruction_from_dto(vault_instruction_dto_from_row(row))
            for row in rows
        ]
