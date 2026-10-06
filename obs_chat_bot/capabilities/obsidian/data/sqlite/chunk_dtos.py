"""SQLite DTO chunks и состояния индекса vault."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VaultNoteChunkDto:
    """Представляет сохранённый chunk в форме SQLite."""

    app_user_id: int
    vault_id: int
    note_id: int
    note_path: str
    chunk_key: str
    position: int
    heading_path: str
    part_index: int
    text: str
    content_hash: str
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class VaultChunkIndexStateDto:
    """Представляет signature согласованного vault index в форме SQLite."""

    app_user_id: int
    vault_id: int
    index_signature: str
    indexed_at: str


@dataclass(frozen=True, slots=True)
class VaultChunkSearchHitDto:
    """Представляет найденный SQLite FTS5 chunk и его BM25 score."""

    chunk: VaultNoteChunkDto
    score: float
