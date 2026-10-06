"""SQLite DTO embeddings и профиля semantic-индекса vault."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VaultChunkEmbeddingDto:
    """Представляет float32 embedding chunk в форме SQLite."""

    app_user_id: int
    vault_id: int
    chunk_id: int
    document_model: str
    dimension: int
    content_hash: str
    vector: bytes
    updated_at: str


@dataclass(frozen=True, slots=True)
class VaultEmbeddingIndexStateDto:
    """Представляет профиль согласованного embedding index в SQLite."""

    app_user_id: int
    vault_id: int
    chunk_index_signature: str
    document_model: str
    query_model: str
    dimension: int | None
    indexed_at: str
