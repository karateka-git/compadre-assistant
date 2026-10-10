"""Application-модели поисковой индексации и retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from uuid import uuid4

from compadre_assistant.capabilities.obsidian.domain.search.entities import (
    ArticleSearchQuery,
    VaultHybridSearchHit,
)
from compadre_assistant.capabilities.obsidian.domain.search.statuses import (
    VaultSearchFallbackReason,
    VaultSearchMode,
)


@dataclass(frozen=True, slots=True)
class EmbeddingCallContext:
    """Связывает технические логи embedding-вызова с application scope.

    Контекст не содержит исходный текст, query, API key или vector values и не
    является частью domain-модели embedding. Один `operation_id` объединяет все
    batches одного вызова `iter_document_batches` либо один `embed_query`.
    """

    app_user_id: int
    vault_id: int
    article_id: int | None = None
    operation_id: str = ""

    def __post_init__(self) -> None:
        if min(self.app_user_id, self.vault_id) <= 0:
            raise ValueError("embedding context IDs must be positive")
        if self.article_id is not None and self.article_id <= 0:
            raise ValueError("article_id must be positive when present")
        if not self.operation_id:
            object.__setattr__(self, "operation_id", uuid4().hex)
        elif not self.operation_id.strip():
            raise ValueError("operation_id must not be blank")


@dataclass(frozen=True, slots=True)
class VaultNoteChunkDraft:
    """Связывает универсальный chunk с сохранённой заметкой пользователя."""

    app_user_id: int
    vault_id: int
    note_id: int
    note_path: str
    chunk_key: str
    position: int
    heading_path: tuple[str, ...]
    part_index: int
    text: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if self.note_id <= 0:
            raise ValueError("note_id must be positive")
        if not self.note_path.strip():
            raise ValueError("note_path must not be empty")
        if not self.chunk_key.strip():
            raise ValueError("chunk_key must not be empty")
        if self.position < 0:
            raise ValueError("position must not be negative")
        if any(not heading.strip() for heading in self.heading_path):
            raise ValueError("heading_path must not contain empty values")
        if self.part_index < 0:
            raise ValueError("part_index must not be negative")
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if not self.content_hash.strip():
            raise ValueError("content_hash must not be empty")


@dataclass(frozen=True, slots=True)
class ChunkIndexUpdate:
    """Содержит счётчики одной операции обновления chunk index."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0

    def __post_init__(self) -> None:
        if min(self.created, self.updated, self.deleted, self.unchanged) < 0:
            raise ValueError("chunk index counters must not be negative")

    @property
    def processed(self) -> int:
        """Возвращает число chunks в новом наборе без удалённых записей."""
        return self.created + self.updated + self.unchanged

    def merge(self, other: ChunkIndexUpdate) -> ChunkIndexUpdate:
        """Объединяет независимые счётчики последовательных операций."""
        return ChunkIndexUpdate(
            created=self.created + other.created,
            updated=self.updated + other.updated,
            deleted=self.deleted + other.deleted,
            unchanged=self.unchanged + other.unchanged,
        )


@dataclass(frozen=True, slots=True)
class VaultChunkEmbeddingDraft:
    """Содержит новый float-вектор chunk до записи в project storage."""

    app_user_id: int
    vault_id: int
    chunk_id: int
    document_model: str
    content_hash: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if min(self.app_user_id, self.vault_id, self.chunk_id) <= 0:
            raise ValueError("embedding IDs must be positive")
        if not self.document_model.strip():
            raise ValueError("document_model must not be empty")
        if not self.content_hash.strip():
            raise ValueError("content_hash must not be empty")
        if not self.values:
            raise ValueError("values must not be empty")
        if any(not isfinite(value) for value in self.values):
            raise ValueError("values must contain only finite numbers")

    @property
    def dimension(self) -> int:
        """Возвращает размерность сохраняемого вектора."""
        return len(self.values)


@dataclass(frozen=True, slots=True)
class EmbeddingIndexUpdate:
    """Содержит счётчики обновления embedding-индекса vault."""

    embedded: int = 0
    deleted: int = 0
    unchanged: int = 0
    dimension: int | None = None

    def __post_init__(self) -> None:
        if min(self.embedded, self.deleted, self.unchanged) < 0:
            raise ValueError("embedding index counters must not be negative")
        if self.dimension is not None and self.dimension <= 0:
            raise ValueError("dimension must be positive when present")


@dataclass(frozen=True, slots=True)
class EmbeddingIndexCoverage:
    """Описывает покрытие текущего набора chunks совместимыми embeddings."""

    total_chunks: int = 0
    embedded_chunks: int = 0

    def __post_init__(self) -> None:
        if min(self.total_chunks, self.embedded_chunks) < 0:
            raise ValueError("embedding coverage counters must not be negative")
        if self.embedded_chunks > self.total_chunks:
            raise ValueError("embedded_chunks must not exceed total_chunks")

    @property
    def missing_chunks(self) -> int:
        """Возвращает число chunks, которые ещё нужно векторизовать."""
        return self.total_chunks - self.embedded_chunks

    @property
    def is_complete(self) -> bool:
        """Проверяет, покрыты ли embeddings все текущие chunks."""
        return self.embedded_chunks == self.total_chunks


@dataclass(frozen=True, slots=True)
class VaultSearchResult:
    """Содержит лучшие chunks и честный режим выполненного retrieval."""

    query: ArticleSearchQuery
    vault_id: int
    hits: tuple[VaultHybridSearchHit, ...]
    lexical_candidates: int
    vector_candidates: int
    mode: VaultSearchMode = VaultSearchMode.HYBRID
    fallback_reason: VaultSearchFallbackReason | None = None

    def __post_init__(self) -> None:
        if self.vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if min(self.lexical_candidates, self.vector_candidates) < 0:
            raise ValueError("candidate counters must not be negative")
        if not isinstance(self.mode, VaultSearchMode):
            raise TypeError("mode must be a VaultSearchMode")
        if self.fallback_reason is not None and not isinstance(
            self.fallback_reason,
            VaultSearchFallbackReason,
        ):
            raise TypeError(
                "fallback_reason must be a VaultSearchFallbackReason when present"
            )
        if self.mode is VaultSearchMode.HYBRID and self.fallback_reason is not None:
            raise ValueError("hybrid result must not have fallback_reason")
        if self.mode is VaultSearchMode.FTS_FALLBACK:
            if self.fallback_reason is None:
                raise ValueError("FTS fallback result must have fallback_reason")
            if self.vector_candidates != 0:
                raise ValueError("FTS fallback must not contain vector candidates")
            if any(
                hit.vector_rank is not None or hit.vector_score is not None
                for hit in self.hits
            ):
                raise ValueError("FTS fallback hits must not contain vector data")
        if any(
            hit.chunk.app_user_id != self.query.app_user_id
            or hit.chunk.vault_id != self.vault_id
            for hit in self.hits
        ):
            raise ValueError("hybrid hits must belong to requested user and vault")
