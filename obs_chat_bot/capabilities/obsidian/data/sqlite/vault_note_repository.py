from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import VaultNoteRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import (
    vault_note_dto_from_row,
    vault_note_from_dto,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultNote


NOTE_COLUMNS = """
    id,
    app_user_id,
    vault_id,
    path,
    blob_sha,
    title,
    markdown,
    frontmatter,
    created_at,
    updated_at
"""


class SQLiteVaultNoteRepository(VaultNoteRepository):
    """Хранит Markdown, tags и wikilinks заметок Obsidian в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def upsert(self, note: VaultNote) -> VaultNote:
        """Атомарно сохраняет заметку и полностью заменяет её metadata."""
        with self._connection:
            vault_row = self._connection.execute(
                """
                SELECT 1
                FROM obsidian_vaults
                WHERE app_user_id = ? AND id = ?
                """,
                (note.app_user_id, note.vault_id),
            ).fetchone()
            if vault_row is None:
                raise ValueError("vault does not belong to app_user_id")
            self._connection.execute(
                """
                INSERT INTO obsidian_notes (
                    app_user_id,
                    vault_id,
                    path,
                    blob_sha,
                    title,
                    markdown,
                    frontmatter
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vault_id, path) DO UPDATE SET
                    blob_sha = excluded.blob_sha,
                    title = excluded.title,
                    markdown = excluded.markdown,
                    frontmatter = excluded.frontmatter,
                    updated_at = CURRENT_TIMESTAMP
                WHERE obsidian_notes.app_user_id = excluded.app_user_id
                """,
                (
                    note.app_user_id,
                    note.vault_id,
                    note.path,
                    note.blob_sha,
                    note.title,
                    note.markdown,
                    note.frontmatter,
                ),
            )
            row = self._connection.execute(
                """
                SELECT id
                FROM obsidian_notes
                WHERE app_user_id = ? AND vault_id = ? AND path = ?
                """,
                (note.app_user_id, note.vault_id, note.path),
            ).fetchone()
            if row is None:
                raise RuntimeError("Saved vault note could not be read")
            note_id = row["id"]
            self._replace_metadata(
                note_id=note_id,
                app_user_id=note.app_user_id,
                tags=note.tags,
                wikilinks=note.wikilinks,
            )

        saved = self.get_by_path(
            app_user_id=note.app_user_id,
            vault_id=note.vault_id,
            path=note.path,
        )
        if saved is None:
            raise RuntimeError("Saved vault note could not be read")
        return saved

    def get_by_path(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        path: str,
    ) -> VaultNote | None:
        """Возвращает заметку по пути внутри vault пользователя."""
        row = self._connection.execute(
            f"""
            SELECT {NOTE_COLUMNS}
            FROM obsidian_notes
            WHERE app_user_id = ? AND vault_id = ? AND path = ?
            """,
            (app_user_id, vault_id, path),
        ).fetchone()
        return self._note_from_row(row) if row is not None else None

    def list_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultNote]:
        """Возвращает все заметки vault в порядке пути."""
        rows = self._connection.execute(
            f"""
            SELECT {NOTE_COLUMNS}
            FROM obsidian_notes
            WHERE app_user_id = ? AND vault_id = ?
            ORDER BY path
            """,
            (app_user_id, vault_id),
        ).fetchall()
        return [self._note_from_row(row) for row in rows]

    def delete_paths(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        paths: set[str],
    ) -> int:
        """Удаляет перечисленные заметки и связанные metadata."""
        if not paths:
            return 0
        ordered_paths = sorted(paths)
        placeholders = ", ".join("?" for _ in ordered_paths)
        with self._connection:
            cursor = self._connection.execute(
                f"""
                DELETE FROM obsidian_notes
                WHERE app_user_id = ?
                    AND vault_id = ?
                    AND path IN ({placeholders})
                """,
                (app_user_id, vault_id, *ordered_paths),
            )
        return cursor.rowcount

    def _replace_metadata(
        self,
        *,
        note_id: int,
        app_user_id: int,
        tags: tuple[str, ...],
        wikilinks: tuple[str, ...],
    ) -> None:
        self._connection.execute(
            "DELETE FROM obsidian_note_tags WHERE note_id = ?",
            (note_id,),
        )
        self._connection.execute(
            "DELETE FROM obsidian_note_wikilinks WHERE note_id = ?",
            (note_id,),
        )
        self._connection.executemany(
            """
            INSERT INTO obsidian_note_tags (app_user_id, note_id, tag, position)
            VALUES (?, ?, ?, ?)
            """,
            (
                (app_user_id, note_id, tag, position)
                for position, tag in enumerate(tags)
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO obsidian_note_wikilinks (
                app_user_id,
                note_id,
                target,
                position
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                (app_user_id, note_id, target, position)
                for position, target in enumerate(wikilinks)
            ),
        )

    def _note_from_row(self, row: sqlite3.Row) -> VaultNote:
        note_id = row["id"]
        tags = tuple(
            metadata_row["tag"]
            for metadata_row in self._connection.execute(
                """
                SELECT tag
                FROM obsidian_note_tags
                WHERE app_user_id = ? AND note_id = ?
                ORDER BY position
                """,
                (row["app_user_id"], note_id),
            ).fetchall()
        )
        wikilinks = tuple(
            metadata_row["target"]
            for metadata_row in self._connection.execute(
                """
                SELECT target
                FROM obsidian_note_wikilinks
                WHERE app_user_id = ? AND note_id = ?
                ORDER BY position
                """,
                (row["app_user_id"], note_id),
            ).fetchall()
        )
        return vault_note_from_dto(
            vault_note_dto_from_row(row, tags=tags, wikilinks=wikilinks)
        )
