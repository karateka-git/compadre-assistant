"""SQLite-хранилище chunks и signature полного индекса vault."""

from __future__ import annotations

import json
import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.search.models import (
    ChunkIndexUpdate,
    VaultNoteChunkDraft,
)
from obs_chat_bot.capabilities.obsidian.domain.search.ports import VaultChunkIndexRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.chunk_mappers import (
    vault_chunk_index_state_dto_from_row,
    vault_chunk_index_state_from_dto,
    vault_note_chunk_dto_from_row,
    vault_note_chunk_from_dto,
)
from obs_chat_bot.capabilities.obsidian.domain.search.entities import VaultChunkIndexState, VaultNoteChunk
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_lease_fence import fence_vault_write


CHUNK_COLUMNS = """
    id,
    app_user_id,
    vault_id,
    note_id,
    note_path,
    chunk_key,
    position,
    heading_path,
    part_index,
    text,
    content_hash,
    created_at,
    updated_at
"""


class SQLiteVaultChunkIndexRepository(VaultChunkIndexRepository):
    """Хранит project-scoped chunks и marker согласованного поколения."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_state(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> VaultChunkIndexState | None:
        """Возвращает сохранённую signature либо `None` для invalid index."""
        row = self._connection.execute(
            """
            SELECT app_user_id, vault_id, index_signature, indexed_at
            FROM obsidian_chunk_index_states
            WHERE app_user_id = ? AND vault_id = ?
            """,
            (app_user_id, vault_id),
        ).fetchone()
        if row is None:
            return None
        return vault_chunk_index_state_from_dto(
            vault_chunk_index_state_dto_from_row(row)
        )

    def list_for_note(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_id: int,
    ) -> list[VaultNoteChunk]:
        """Возвращает chunks одной заметки в порядке позиции."""
        rows = self._connection.execute(
            f"""
            SELECT {CHUNK_COLUMNS}
            FROM obsidian_note_chunks
            WHERE app_user_id = ? AND vault_id = ? AND note_id = ?
            ORDER BY position
            """,
            (app_user_id, vault_id, note_id),
        ).fetchall()
        return [_chunk_from_row(row) for row in rows]

    def list_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultNoteChunk]:
        """Возвращает все chunks vault в стабильном порядке source path."""
        rows = self._connection.execute(
            f"""
            SELECT {CHUNK_COLUMNS}
            FROM obsidian_note_chunks
            WHERE app_user_id = ? AND vault_id = ?
            ORDER BY note_path, position
            """,
            (app_user_id, vault_id),
        ).fetchall()
        return [_chunk_from_row(row) for row in rows]

    def invalidate(self, *, app_user_id: int, vault_id: int) -> None:
        """Удаляет marker до начала потенциально частичной серии записей."""
        with self._connection:
            fence_vault_write(self._connection, app_user_id, vault_id)
            self._connection.execute(
                """
                DELETE FROM obsidian_chunk_index_states
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            )

    def list_stale_note_ids(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        index_signature: str,
    ) -> set[int]:
        """Находит заметки, чьи blob SHA ещё не отражены в текущих chunks."""
        if not index_signature.strip():
            raise ValueError("index_signature must not be empty")
        rows = self._connection.execute(
            """
            SELECT note.id
            FROM obsidian_notes AS note
            LEFT JOIN obsidian_note_chunk_index_states AS state
              ON state.note_id = note.id
             AND state.app_user_id = note.app_user_id
             AND state.vault_id = note.vault_id
            WHERE note.app_user_id = ? AND note.vault_id = ?
              AND (
                  state.note_id IS NULL
                  OR state.source_blob_sha <> note.blob_sha
                  OR state.index_signature <> ?
              )
            """,
            (app_user_id, vault_id, index_signature),
        ).fetchall()
        return {row["id"] for row in rows}

    def mark_note_current(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_id: int,
        source_blob_sha: str,
        index_signature: str,
    ) -> None:
        """Публикует source revision chunks одной заметки после её diff."""
        if not source_blob_sha.strip() or not index_signature.strip():
            raise ValueError("note index state values must not be empty")
        with self._connection:
            fence_vault_write(self._connection, app_user_id, vault_id)
            self._require_note_scope(
                app_user_id=app_user_id,
                vault_id=vault_id,
                note_id=note_id,
            )
            self._connection.execute(
                """
                INSERT INTO obsidian_note_chunk_index_states (
                    app_user_id, vault_id, note_id, source_blob_sha,
                    index_signature
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    app_user_id = excluded.app_user_id,
                    vault_id = excluded.vault_id,
                    source_blob_sha = excluded.source_blob_sha,
                    index_signature = excluded.index_signature,
                    indexed_at = CURRENT_TIMESTAMP
                """,
                (
                    app_user_id,
                    vault_id,
                    note_id,
                    source_blob_sha,
                    index_signature,
                ),
            )

    def replace_for_note(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_id: int,
        chunks: tuple[VaultNoteChunkDraft, ...],
    ) -> ChunkIndexUpdate:
        """Атомарно сравнивает и заменяет chunks одной заметки."""
        with self._connection:
            fence_vault_write(self._connection, app_user_id, vault_id)
            note_path = self._require_note_scope(
                app_user_id=app_user_id,
                vault_id=vault_id,
                note_id=note_id,
            )
            _validate_chunk_set(
                chunks,
                app_user_id=app_user_id,
                vault_id=vault_id,
                note_paths={note_id: note_path},
            )
            existing_rows = self._connection.execute(
                f"""
                SELECT {CHUNK_COLUMNS}
                FROM obsidian_note_chunks
                WHERE app_user_id = ? AND vault_id = ? AND note_id = ?
                """,
                (app_user_id, vault_id, note_id),
            ).fetchall()
            existing_by_key = {row["chunk_key"]: row for row in existing_rows}
            new_by_key = {chunk.chunk_key: chunk for chunk in chunks}
            deleted_keys = set(existing_by_key) - set(new_by_key)
            created_keys = set(new_by_key) - set(existing_by_key)
            retained_keys = set(new_by_key) & set(existing_by_key)
            updated_keys = {
                key
                for key in retained_keys
                if not _row_matches_draft(existing_by_key[key], new_by_key[key])
            }
            unchanged_keys = retained_keys - updated_keys

            if existing_rows:
                maximum_position = max(
                    [row["position"] for row in existing_rows]
                    + [chunk.position for chunk in chunks]
                )
                offset = maximum_position + len(existing_rows) + len(chunks) + 1
                self._connection.execute(
                    """
                    UPDATE obsidian_note_chunks
                    SET position = position + ?
                    WHERE app_user_id = ? AND vault_id = ? AND note_id = ?
                    """,
                    (offset, app_user_id, vault_id, note_id),
                )

            if deleted_keys:
                placeholders = ", ".join("?" for _ in deleted_keys)
                self._connection.execute(
                    f"""
                    DELETE FROM obsidian_note_chunks
                    WHERE app_user_id = ? AND vault_id = ? AND note_id = ?
                        AND chunk_key IN ({placeholders})
                    """,
                    (app_user_id, vault_id, note_id, *sorted(deleted_keys)),
                )

            for key in sorted(created_keys):
                self._insert_chunk(new_by_key[key])
            for key in sorted(retained_keys):
                self._update_chunk(
                    row_id=existing_by_key[key]["id"],
                    chunk=new_by_key[key],
                    touch_updated_at=key in updated_keys,
                )

        return ChunkIndexUpdate(
            created=len(created_keys),
            updated=len(updated_keys),
            deleted=len(deleted_keys),
            unchanged=len(unchanged_keys),
        )

    def delete_for_notes(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_ids: set[int],
    ) -> int:
        """Удаляет chunks выбранных заметок перед удалением source rows."""
        if not note_ids:
            return 0
        ordered_ids = sorted(note_ids)
        placeholders = ", ".join("?" for _ in ordered_ids)
        with self._connection:
            fence_vault_write(self._connection, app_user_id, vault_id)
            cursor = self._connection.execute(
                f"""
                DELETE FROM obsidian_note_chunks
                WHERE app_user_id = ? AND vault_id = ?
                    AND note_id IN ({placeholders})
                """,
                (app_user_id, vault_id, *ordered_ids),
            )
        return cursor.rowcount

    def replace_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        chunks: tuple[VaultNoteChunkDraft, ...],
        index_signature: str,
    ) -> ChunkIndexUpdate:
        """Атомарно заменяет полное поколение chunks и его signature."""
        if not index_signature.strip():
            raise ValueError("index_signature must not be empty")
        with self._connection:
            fence_vault_write(self._connection, app_user_id, vault_id)
            note_paths = self._list_note_paths(
                app_user_id=app_user_id,
                vault_id=vault_id,
            )
            note_rows = self._connection.execute(
                """
                SELECT id, blob_sha FROM obsidian_notes
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            ).fetchall()
            self._require_vault_scope(app_user_id=app_user_id, vault_id=vault_id)
            _validate_chunk_set(
                chunks,
                app_user_id=app_user_id,
                vault_id=vault_id,
                note_paths=note_paths,
            )
            old_count = self._connection.execute(
                """
                SELECT COUNT(*)
                FROM obsidian_note_chunks
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            ).fetchone()[0]
            self._connection.execute(
                """
                DELETE FROM obsidian_note_chunks
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            )
            for chunk in sorted(chunks, key=lambda item: (item.note_path, item.position)):
                self._insert_chunk(chunk)
            self._connection.execute(
                """
                DELETE FROM obsidian_note_chunk_index_states
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            )
            self._connection.executemany(
                """
                INSERT INTO obsidian_note_chunk_index_states (
                    app_user_id, vault_id, note_id, source_blob_sha,
                    index_signature
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        app_user_id,
                        vault_id,
                        row["id"],
                        row["blob_sha"],
                        index_signature,
                    )
                    for row in note_rows
                ),
            )
            self._upsert_state(
                app_user_id=app_user_id,
                vault_id=vault_id,
                index_signature=index_signature,
            )
        return ChunkIndexUpdate(created=len(chunks), deleted=old_count)

    def mark_current(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        index_signature: str,
    ) -> VaultChunkIndexState:
        """Записывает marker после успешной серии инкрементальных операций."""
        if not index_signature.strip():
            raise ValueError("index_signature must not be empty")
        with self._connection:
            fence_vault_write(self._connection, app_user_id, vault_id)
            self._require_vault_scope(app_user_id=app_user_id, vault_id=vault_id)
            if self.list_stale_note_ids(
                app_user_id=app_user_id,
                vault_id=vault_id,
                index_signature=index_signature,
            ):
                raise ValueError("all vault notes must be indexed before publishing")
            self._upsert_state(
                app_user_id=app_user_id,
                vault_id=vault_id,
                index_signature=index_signature,
            )
        state = self.get_state(app_user_id=app_user_id, vault_id=vault_id)
        if state is None:
            raise RuntimeError("Current chunk index state could not be read")
        return state

    def _insert_chunk(self, chunk: VaultNoteChunkDraft) -> None:
        self._connection.execute(
            """
            INSERT INTO obsidian_note_chunks (
                app_user_id,
                vault_id,
                note_id,
                note_path,
                chunk_key,
                position,
                heading_path,
                part_index,
                text,
                content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _chunk_values(chunk),
        )

    def _update_chunk(
        self,
        *,
        row_id: int,
        chunk: VaultNoteChunkDraft,
        touch_updated_at: bool,
    ) -> None:
        updated_at_sql = ", updated_at = CURRENT_TIMESTAMP" if touch_updated_at else ""
        self._connection.execute(
            f"""
            UPDATE obsidian_note_chunks
            SET note_path = ?,
                position = ?,
                heading_path = ?,
                part_index = ?,
                text = ?,
                content_hash = ?
                {updated_at_sql}
            WHERE id = ? AND app_user_id = ? AND vault_id = ? AND note_id = ?
            """,
            (
                chunk.note_path,
                chunk.position,
                _heading_path_json(chunk.heading_path),
                chunk.part_index,
                chunk.text,
                chunk.content_hash,
                row_id,
                chunk.app_user_id,
                chunk.vault_id,
                chunk.note_id,
            ),
        )

    def _upsert_state(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        index_signature: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO obsidian_chunk_index_states (
                app_user_id,
                vault_id,
                index_signature
            )
            VALUES (?, ?, ?)
            ON CONFLICT(app_user_id, vault_id) DO UPDATE SET
                index_signature = excluded.index_signature,
                indexed_at = CURRENT_TIMESTAMP
            """,
            (app_user_id, vault_id, index_signature),
        )

    def _require_note_scope(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_id: int,
    ) -> str:
        row = self._connection.execute(
            """
            SELECT path
            FROM obsidian_notes
            WHERE app_user_id = ? AND vault_id = ? AND id = ?
            """,
            (app_user_id, vault_id, note_id),
        ).fetchone()
        if row is None:
            raise ValueError("note does not belong to app_user_id and vault_id")
        return row["path"]

    def _require_vault_scope(self, *, app_user_id: int, vault_id: int) -> None:
        row = self._connection.execute(
            """
            SELECT 1
            FROM obsidian_vaults
            WHERE app_user_id = ? AND id = ?
            """,
            (app_user_id, vault_id),
        ).fetchone()
        if row is None:
            raise ValueError("vault does not belong to app_user_id")

    def _list_note_paths(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> dict[int, str]:
        rows = self._connection.execute(
            """
            SELECT id, path
            FROM obsidian_notes
            WHERE app_user_id = ? AND vault_id = ?
            """,
            (app_user_id, vault_id),
        ).fetchall()
        return {row["id"]: row["path"] for row in rows}


def _validate_chunk_set(
    chunks: tuple[VaultNoteChunkDraft, ...],
    *,
    app_user_id: int,
    vault_id: int,
    note_paths: dict[int, str],
) -> None:
    identities: set[tuple[int, str]] = set()
    positions: set[tuple[int, int]] = set()
    for chunk in chunks:
        if chunk.app_user_id != app_user_id or chunk.vault_id != vault_id:
            raise ValueError("chunk does not belong to requested user and vault")
        expected_path = note_paths.get(chunk.note_id)
        if expected_path is None or expected_path != chunk.note_path:
            raise ValueError("chunk note_id and note_path do not match saved note")
        identity = (chunk.note_id, chunk.chunk_key)
        position = (chunk.note_id, chunk.position)
        if identity in identities:
            raise ValueError("chunk_key must be unique inside note")
        if position in positions:
            raise ValueError("chunk position must be unique inside note")
        identities.add(identity)
        positions.add(position)


def _row_matches_draft(row: sqlite3.Row, chunk: VaultNoteChunkDraft) -> bool:
    return (
        row["note_path"] == chunk.note_path
        and row["position"] == chunk.position
        and row["heading_path"] == _heading_path_json(chunk.heading_path)
        and row["part_index"] == chunk.part_index
        and row["text"] == chunk.text
        and row["content_hash"] == chunk.content_hash
    )


def _chunk_values(chunk: VaultNoteChunkDraft) -> tuple[object, ...]:
    return (
        chunk.app_user_id,
        chunk.vault_id,
        chunk.note_id,
        chunk.note_path,
        chunk.chunk_key,
        chunk.position,
        _heading_path_json(chunk.heading_path),
        chunk.part_index,
        chunk.text,
        chunk.content_hash,
    )


def _heading_path_json(heading_path: tuple[str, ...]) -> str:
    return json.dumps(
        heading_path,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _chunk_from_row(row: sqlite3.Row) -> VaultNoteChunk:
    return vault_note_chunk_from_dto(vault_note_chunk_dto_from_row(row))
