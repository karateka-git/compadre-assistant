"""Явные преобразования SQLite DTO поискового индекса в domain-модели."""

from __future__ import annotations

import json
import sqlite3

from obs_chat_bot.capabilities.obsidian.data.sqlite.chunk_dtos import (
    VaultChunkIndexStateDto,
    VaultChunkSearchHitDto,
    VaultNoteChunkDto,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import parse_utc_timestamp
from obs_chat_bot.capabilities.obsidian.domain.search.entities import (
    VaultChunkIndexState,
    VaultChunkSearchHit,
    VaultNoteChunk,
)


def vault_note_chunk_dto_from_row(row: sqlite3.Row) -> VaultNoteChunkDto:
    """Преобразует строку SQLite в DTO сохранённого chunk."""
    return VaultNoteChunkDto(
        id=row["id"],
        app_user_id=row["app_user_id"],
        vault_id=row["vault_id"],
        note_id=row["note_id"],
        note_path=row["note_path"],
        chunk_key=row["chunk_key"],
        position=row["position"],
        heading_path=row["heading_path"],
        part_index=row["part_index"],
        text=row["text"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def vault_note_chunk_from_dto(dto: VaultNoteChunkDto) -> VaultNoteChunk:
    """Преобразует SQLite DTO в доменную модель chunk."""
    if dto.id is None or dto.created_at is None or dto.updated_at is None:
        raise ValueError("Saved chunk DTO must contain id and timestamps")
    heading_path_value = json.loads(dto.heading_path)
    if not isinstance(heading_path_value, list) or any(
        not isinstance(heading, str) for heading in heading_path_value
    ):
        raise ValueError("Saved chunk heading_path must be a JSON string array")
    return VaultNoteChunk(
        id=dto.id,
        app_user_id=dto.app_user_id,
        vault_id=dto.vault_id,
        note_id=dto.note_id,
        note_path=dto.note_path,
        chunk_key=dto.chunk_key,
        position=dto.position,
        heading_path=tuple(heading_path_value),
        part_index=dto.part_index,
        text=dto.text,
        content_hash=dto.content_hash,
        created_at=parse_utc_timestamp(dto.created_at),
        updated_at=parse_utc_timestamp(dto.updated_at),
    )


def vault_chunk_index_state_dto_from_row(
    row: sqlite3.Row,
) -> VaultChunkIndexStateDto:
    """Преобразует строку SQLite в DTO состояния chunk index."""
    return VaultChunkIndexStateDto(
        app_user_id=row["app_user_id"],
        vault_id=row["vault_id"],
        index_signature=row["index_signature"],
        indexed_at=row["indexed_at"],
    )


def vault_chunk_index_state_from_dto(
    dto: VaultChunkIndexStateDto,
) -> VaultChunkIndexState:
    """Преобразует SQLite DTO в доменное состояние chunk index."""
    return VaultChunkIndexState(
        app_user_id=dto.app_user_id,
        vault_id=dto.vault_id,
        index_signature=dto.index_signature,
        indexed_at=parse_utc_timestamp(dto.indexed_at),
    )


def vault_chunk_search_hit_dto_from_row(row: sqlite3.Row) -> VaultChunkSearchHitDto:
    """Преобразует объединённую строку FTS5/chunks в DTO результата."""
    return VaultChunkSearchHitDto(
        chunk=vault_note_chunk_dto_from_row(row),
        score=row["score"],
    )


def vault_chunk_search_hit_from_dto(
    dto: VaultChunkSearchHitDto,
) -> VaultChunkSearchHit:
    """Преобразует SQLite DTO в доменный результат полнотекстового поиска."""
    return VaultChunkSearchHit(
        chunk=vault_note_chunk_from_dto(dto.chunk),
        score=dto.score,
    )
