"""Преобразования float32 SQLite BLOB и domain embedding-моделей."""

from __future__ import annotations

from math import isfinite
import sqlite3
import struct

from compadre_assistant.capabilities.obsidian.data.sqlite.embedding_dtos import (
    VaultChunkEmbeddingDto,
    VaultEmbeddingIndexStateDto,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_mappers import parse_utc_timestamp
from compadre_assistant.capabilities.obsidian.domain.search.entities import (
    VaultChunkEmbedding,
    VaultEmbeddingIndexState,
)


def encode_float32_vector(values: tuple[float, ...]) -> bytes:
    """Кодирует конечные координаты как portable little-endian float32 BLOB."""
    if not values or any(not isfinite(value) for value in values):
        raise ValueError("embedding values must be non-empty and finite")
    try:
        return struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error) as error:
        raise ValueError("embedding values must fit float32") from error


def decode_float32_vector(vector: bytes, *, dimension: int) -> tuple[float, ...]:
    """Декодирует BLOB и строго проверяет заявленную dimension."""
    if dimension <= 0 or len(vector) != dimension * 4:
        raise ValueError("embedding BLOB length does not match dimension")
    values = struct.unpack(f"<{dimension}f", vector)
    if any(not isfinite(value) for value in values):
        raise ValueError("stored embedding contains non-finite values")
    return values


def vault_chunk_embedding_dto_from_row(
    row: sqlite3.Row,
) -> VaultChunkEmbeddingDto:
    """Преобразует строку SQLite в DTO embedding chunk."""
    return VaultChunkEmbeddingDto(
        app_user_id=row["app_user_id"],
        vault_id=row["vault_id"],
        chunk_id=row["chunk_id"],
        document_model=row["document_model"],
        dimension=row["dimension"],
        content_hash=row["content_hash"],
        vector=bytes(row["vector"]),
        updated_at=row["updated_at"],
    )


def vault_chunk_embedding_from_dto(
    dto: VaultChunkEmbeddingDto,
) -> VaultChunkEmbedding:
    """Преобразует SQLite DTO в domain embedding chunk."""
    return VaultChunkEmbedding(
        app_user_id=dto.app_user_id,
        vault_id=dto.vault_id,
        chunk_id=dto.chunk_id,
        document_model=dto.document_model,
        dimension=dto.dimension,
        content_hash=dto.content_hash,
        values=decode_float32_vector(dto.vector, dimension=dto.dimension),
        updated_at=parse_utc_timestamp(dto.updated_at),
    )


def vault_embedding_index_state_dto_from_row(
    row: sqlite3.Row,
) -> VaultEmbeddingIndexStateDto:
    """Преобразует строку SQLite в DTO embedding marker."""
    return VaultEmbeddingIndexStateDto(
        app_user_id=row["app_user_id"],
        vault_id=row["vault_id"],
        chunk_index_signature=row["chunk_index_signature"],
        document_model=row["document_model"],
        query_model=row["query_model"],
        dimension=row["dimension"],
        indexed_at=row["indexed_at"],
    )


def vault_embedding_index_state_from_dto(
    dto: VaultEmbeddingIndexStateDto,
) -> VaultEmbeddingIndexState:
    """Преобразует SQLite DTO в domain embedding marker."""
    return VaultEmbeddingIndexState(
        app_user_id=dto.app_user_id,
        vault_id=dto.vault_id,
        chunk_index_signature=dto.chunk_index_signature,
        document_model=dto.document_model,
        query_model=dto.query_model,
        dimension=dto.dimension,
        indexed_at=parse_utc_timestamp(dto.indexed_at),
    )
