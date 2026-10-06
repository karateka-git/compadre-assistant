"""SQLite-хранилище float32 embeddings и согласованного профиля vault."""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
import sqlite3

from obs_chat_bot.capabilities.obsidian.application.search.models import VaultChunkEmbeddingDraft
from obs_chat_bot.capabilities.obsidian.application.search.ports import VaultEmbeddingIndexRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.embedding_mappers import (
    encode_float32_vector,
    vault_chunk_embedding_dto_from_row,
    vault_chunk_embedding_from_dto,
    vault_embedding_index_state_dto_from_row,
    vault_embedding_index_state_from_dto,
)
from obs_chat_bot.capabilities.obsidian.domain.search.entities import (
    VaultChunkEmbedding,
    VaultChunkEmbeddingMetadata,
    VaultEmbeddingIndexState,
)


EMBEDDING_COLUMNS = """
    app_user_id,
    vault_id,
    chunk_id,
    document_model,
    dimension,
    content_hash,
    vector,
    updated_at
"""


class SQLiteVaultEmbeddingIndexRepository(VaultEmbeddingIndexRepository):
    """Хранит vectors как float32 BLOB с project-scoped marker поколения."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_state(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> VaultEmbeddingIndexState | None:
        """Возвращает профиль согласованного embedding index."""
        row = self._connection.execute(
            """
            SELECT app_user_id, vault_id, chunk_index_signature,
                   document_model, query_model, dimension, indexed_at
            FROM obsidian_embedding_index_states
            WHERE app_user_id = ? AND vault_id = ?
            """,
            (app_user_id, vault_id),
        ).fetchone()
        if row is None:
            return None
        return vault_embedding_index_state_from_dto(
            vault_embedding_index_state_dto_from_row(row)
        )

    def list_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultChunkEmbedding]:
        """Возвращает embeddings vault в стабильном порядке chunk ID."""
        rows = self._connection.execute(
            f"""
            SELECT {EMBEDDING_COLUMNS}
            FROM obsidian_chunk_embeddings
            WHERE app_user_id = ? AND vault_id = ?
            ORDER BY chunk_id
            """,
            (app_user_id, vault_id),
        ).fetchall()
        return [
            vault_chunk_embedding_from_dto(
                vault_chunk_embedding_dto_from_row(row)
            )
            for row in rows
        ]

    def list_metadata_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultChunkEmbeddingMetadata]:
        """Читает только compatibility metadata, не загружая vector BLOB."""
        rows = self._connection.execute(
            """
            SELECT app_user_id, vault_id, chunk_id, document_model,
                   dimension, content_hash
            FROM obsidian_chunk_embeddings
            WHERE app_user_id = ? AND vault_id = ?
            ORDER BY chunk_id
            """,
            (app_user_id, vault_id),
        ).fetchall()
        return [
            VaultChunkEmbeddingMetadata(
                app_user_id=row["app_user_id"],
                vault_id=row["vault_id"],
                chunk_id=row["chunk_id"],
                document_model=row["document_model"],
                dimension=row["dimension"],
                content_hash=row["content_hash"],
            )
            for row in rows
        ]

    def invalidate(
        self, *, app_user_id: int, vault_id: int,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """Удаляет только marker, проверяя lease под write lock при наличии guard."""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if before_write is not None:
                before_write()
            self._connection.execute(
                """
                DELETE FROM obsidian_embedding_index_states
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            )

    def save_batch(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        embeddings: tuple[VaultChunkEmbeddingDraft, ...],
        chunk_index_signature: str,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """Фиксирует пакет отдельно, проверяя source и lease под write lock.

        Raises:
            ValueError: Пакет или текущее поколение chunks несовместимы.
            RuntimeError: Владелец потерял lease; guard останавливает запись.
        """
        if not embeddings:
            raise ValueError("checkpoint batch must not be empty")
        model, dimension = embeddings[0].document_model, embeddings[0].dimension
        self._validate_generation(
            app_user_id=app_user_id, vault_id=vault_id,
            current_chunk_ids={item.chunk_id for item in embeddings},
            embeddings=embeddings, document_model=model, dimension=dimension,
        )
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if before_write is not None:
                before_write()
            self._check_chunk_state(app_user_id, vault_id, chunk_index_signature)
            for item in embeddings:
                source = self._connection.execute(
                    """SELECT content_hash FROM obsidian_note_chunks
                       WHERE app_user_id = ? AND vault_id = ? AND id = ?""",
                    (app_user_id, vault_id, item.chunk_id),
                ).fetchone()
                if source is None or source["content_hash"] != item.content_hash:
                    raise ValueError("checkpoint source chunk changed")
            self._connection.execute(
                "DELETE FROM obsidian_embedding_index_states WHERE app_user_id=? AND vault_id=?",
                (app_user_id, vault_id),
            )
            # Не оставляем смешанную размерность: иначе retry отвергнет и
            # только что оплаченные checkpoints вместе со старыми vectors.
            self._connection.execute(
                """DELETE FROM obsidian_chunk_embeddings
                   WHERE app_user_id=? AND vault_id=?
                     AND (document_model<>? OR dimension<>?)""",
                (app_user_id, vault_id, model, dimension),
            )
            for embedding in embeddings:
                self._upsert_embedding(embedding)

    def complete_generation(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        current_chunk_ids: set[int],
        chunk_index_signature: str,
        document_model: str,
        query_model: str,
        dimension: int | None,
        before_write: Callable[[], None] | None = None,
    ) -> int:
        """Публикует marker только при полном совместимом покрытии в SQLite.

        Проверка source, hashes, размерности и запись marker атомарны.
        Частичные checkpoints не считаются доступным semantic-поколением.
        """
        self._validate_generation(
            app_user_id=app_user_id, vault_id=vault_id,
            current_chunk_ids=current_chunk_ids, embeddings=(),
            document_model=document_model, dimension=dimension,
        )
        if not chunk_index_signature.strip() or not query_model.strip():
            raise ValueError("embedding profile values must not be empty")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if before_write is not None:
                before_write()
            self._check_chunk_state(app_user_id, vault_id, chunk_index_signature)
            rows = self._connection.execute(
                """SELECT c.id, c.content_hash AS source_hash, e.content_hash,
                          e.document_model, e.dimension
                   FROM obsidian_note_chunks c
                   LEFT JOIN obsidian_chunk_embeddings e
                     ON e.chunk_id=c.id AND e.app_user_id=c.app_user_id
                     AND e.vault_id=c.vault_id
                   WHERE c.app_user_id=? AND c.vault_id=?""",
                (app_user_id, vault_id),
            ).fetchall()
            if {row["id"] for row in rows} != current_chunk_ids or any(
                row["source_hash"] != row["content_hash"]
                or row["document_model"] != document_model
                or row["dimension"] != dimension for row in rows
            ):
                raise ValueError("embedding generation coverage is incomplete or stale")
            deleted = self._delete_missing(
                app_user_id=app_user_id, vault_id=vault_id,
                current_chunk_ids=current_chunk_ids,
            )
            self._connection.execute(
                """
                INSERT INTO obsidian_embedding_index_states (
                    app_user_id,
                    vault_id,
                    chunk_index_signature,
                    document_model,
                    query_model,
                    dimension
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_user_id, vault_id) DO UPDATE SET
                    chunk_index_signature = excluded.chunk_index_signature,
                    document_model = excluded.document_model,
                    query_model = excluded.query_model,
                    dimension = excluded.dimension,
                    indexed_at = CURRENT_TIMESTAMP
                """,
                (
                    app_user_id,
                    vault_id,
                    chunk_index_signature,
                    document_model,
                    query_model,
                    dimension,
                ),
            )
        return deleted

    def _check_chunk_state(self, app_user_id: int, vault_id: int, signature: str) -> None:
        """Не допускает checkpoint или marker для stale chunk-поколения."""
        row = self._connection.execute(
            """SELECT index_signature FROM obsidian_chunk_index_states
               WHERE app_user_id=? AND vault_id=?""",
            (app_user_id, vault_id),
        ).fetchone()
        if row is None or row["index_signature"] != signature:
            raise ValueError("chunk generation changed during embedding")

    def _delete_missing(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        current_chunk_ids: set[int],
    ) -> int:
        """Удаляет vectors chunks, отсутствующих в текущем поколении."""
        if not current_chunk_ids:
            cursor = self._connection.execute(
                """
                DELETE FROM obsidian_chunk_embeddings
                WHERE app_user_id = ? AND vault_id = ?
                """,
                (app_user_id, vault_id),
            )
            return cursor.rowcount
        ordered_ids = sorted(current_chunk_ids)
        placeholders = ", ".join("?" for _ in ordered_ids)
        cursor = self._connection.execute(
            f"""
            DELETE FROM obsidian_chunk_embeddings
            WHERE app_user_id = ? AND vault_id = ?
              AND chunk_id NOT IN ({placeholders})
            """,
            (app_user_id, vault_id, *ordered_ids),
        )
        return cursor.rowcount

    def _upsert_embedding(self, embedding: VaultChunkEmbeddingDraft) -> None:
        """Записывает один vector и обновляет metadata при конфликте chunk ID."""
        self._connection.execute(
            """
            INSERT INTO obsidian_chunk_embeddings (
                app_user_id,
                vault_id,
                chunk_id,
                document_model,
                dimension,
                content_hash,
                vector
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                app_user_id = excluded.app_user_id,
                vault_id = excluded.vault_id,
                document_model = excluded.document_model,
                dimension = excluded.dimension,
                content_hash = excluded.content_hash,
                vector = excluded.vector,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                embedding.app_user_id,
                embedding.vault_id,
                embedding.chunk_id,
                embedding.document_model,
                embedding.dimension,
                embedding.content_hash,
                encode_float32_vector(embedding.values),
            ),
        )

    def _validate_generation(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        current_chunk_ids: set[int],
        embeddings: tuple[VaultChunkEmbeddingDraft, ...],
        document_model: str,
        dimension: int | None,
    ) -> None:
        """Проверяет scope и единый embedding profile до транзакции."""
        if not document_model.strip():
            raise ValueError("document_model must not be empty")
        if dimension is None and current_chunk_ids:
            raise ValueError("non-empty generation must have dimension")
        if dimension is not None and dimension <= 0:
            raise ValueError("dimension must be positive")
        seen: set[int] = set()
        for item in embeddings:
            if (
                item.app_user_id != app_user_id
                or item.vault_id != vault_id
                or item.chunk_id not in current_chunk_ids
            ):
                raise ValueError("embedding does not belong to requested vault")
            if item.chunk_id in seen:
                raise ValueError("chunk embedding must be unique")
            if item.document_model != document_model or item.dimension != dimension:
                raise ValueError("embedding does not match generation profile")
            if any(not isfinite(value) for value in item.values):
                raise ValueError("embedding values must be finite")
            seen.add(item.chunk_id)
