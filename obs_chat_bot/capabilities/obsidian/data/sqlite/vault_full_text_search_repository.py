"""SQLite FTS5-адаптер поиска по сохранённым chunks vault."""

from __future__ import annotations

import re
import sqlite3

from obs_chat_bot.capabilities.obsidian.application.search.ports import VaultFullTextSearchRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.chunk_mappers import (
    vault_chunk_search_hit_dto_from_row,
    vault_chunk_search_hit_from_dto,
)
from obs_chat_bot.capabilities.obsidian.domain.search.entities import VaultChunkSearchHit


_SEARCH_TERM_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_MAX_SEARCH_TERMS = 64


class SQLiteVaultFullTextSearchRepository(VaultFullTextSearchRepository):
    """Ищет chunks через FTS5 BM25 в границах пользователя и vault."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def search(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        query: str,
        expected_index_signature: str,
        limit: int,
    ) -> tuple[VaultChunkSearchHit, ...]:
        """Возвращает BM25-выдачу только для актуальной index signature.

        Args:
            app_user_id: Внутренний ID пользователя приложения.
            vault_id: ID активного Obsidian vault.
            query: Обычный текст; специальные операторы FTS5 не исполняются.
            expected_index_signature: Signature текущих parser и policy.
            limit: Максимальное число результатов от 1 до 100.

        Returns:
            Найденные chunks с убывающим score. Если generation marker
            отсутствует или устарел, возвращается пустой tuple.

        Raises:
            ValueError: Если scope, signature или limit некорректны.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if not expected_index_signature.strip():
            raise ValueError("expected_index_signature must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        match_query = _build_match_query(query)
        if match_query is None:
            return ()
        rows = self._connection.execute(
            """
            SELECT
                chunks.id AS id,
                chunks.app_user_id AS app_user_id,
                chunks.vault_id AS vault_id,
                chunks.note_id AS note_id,
                chunks.note_path AS note_path,
                chunks.chunk_key AS chunk_key,
                chunks.position AS position,
                chunks.heading_path AS heading_path,
                chunks.part_index AS part_index,
                chunks.text AS text,
                chunks.content_hash AS content_hash,
                chunks.created_at AS created_at,
                chunks.updated_at AS updated_at,
                MAX(
                    0.0,
                    -bm25(
                        obsidian_note_chunks_fts,
                        0.0,
                        0.0,
                        0.0,
                        5.0,
                        2.0,
                        4.0,
                        3.0,
                        1.0
                    )
                ) AS score
            FROM obsidian_note_chunks_fts
            JOIN obsidian_note_chunks AS chunks
                ON chunks.id = obsidian_note_chunks_fts.rowid
            JOIN obsidian_chunk_index_states AS index_state
                ON index_state.app_user_id = chunks.app_user_id
                AND index_state.vault_id = chunks.vault_id
            WHERE obsidian_note_chunks_fts MATCH ?
                AND chunks.app_user_id = ?
                AND chunks.vault_id = ?
                AND CAST(obsidian_note_chunks_fts.app_user_id AS INTEGER) = ?
                AND CAST(obsidian_note_chunks_fts.vault_id AS INTEGER) = ?
                AND index_state.index_signature = ?
            ORDER BY score DESC, chunks.note_path, chunks.position
            LIMIT ?
            """,
            (
                match_query,
                app_user_id,
                vault_id,
                app_user_id,
                vault_id,
                expected_index_signature,
                limit,
            ),
        ).fetchall()
        return tuple(
            vault_chunk_search_hit_from_dto(
                vault_chunk_search_hit_dto_from_row(row)
            )
            for row in rows
        )


def _build_match_query(query: str) -> str | None:
    """Преобразует произвольный текст в безопасное OR-выражение FTS5."""
    terms: list[str] = []
    seen: set[str] = set()
    for match in _SEARCH_TERM_PATTERN.finditer(query):
        term = match.group(0)
        normalized = term.casefold()
        if normalized in seen:
            continue
        terms.append(term)
        seen.add(normalized)
        if len(terms) == _MAX_SEARCH_TERMS:
            break
    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms)
