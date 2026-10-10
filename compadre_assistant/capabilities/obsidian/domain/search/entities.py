"""Доменные сущности chunks и состояния поискового индекса."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite


@dataclass(frozen=True, slots=True)
class VaultNoteChunk:
    """Представляет сохранённый структурный fragment Markdown-заметки."""

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
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

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
        if self.id is not None and self.id <= 0:
            raise ValueError("id must be positive")


@dataclass(frozen=True, slots=True)
class VaultChunkIndexState:
    """Фиксирует signature полностью согласованного индекса одного vault."""

    app_user_id: int
    vault_id: int
    index_signature: str
    indexed_at: datetime

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if not self.index_signature.strip():
            raise ValueError("index_signature must not be empty")


@dataclass(frozen=True, slots=True)
class VaultChunkSearchHit:
    """Представляет chunk, найденный одной ранжированной ветвью поиска.

    Attributes:
        chunk: Сохранённый chunk, доступный текущему пользователю и vault.
        score: Релевантность результата; большее значение означает более
            высокую позицию в выдаче.
    """

    chunk: VaultNoteChunk
    score: float

    def __post_init__(self) -> None:
        if not isfinite(self.score) or self.score < 0:
            raise ValueError("score must be finite and not negative")


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """Представляет числовой semantic-вектор, возвращённый одной моделью.

    Attributes:
        model: Стабильный ID embedding-модели провайдера.
        values: Конечные float-координаты непустого вектора.
    """

    model: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not self.values:
            raise ValueError("values must not be empty")
        if any(not isfinite(value) for value in self.values):
            raise ValueError("values must contain only finite numbers")

    @property
    def dimension(self) -> int:
        """Возвращает число координат вектора."""
        return len(self.values)


@dataclass(frozen=True, slots=True)
class VaultChunkEmbedding:
    """Представляет сохранённый semantic-вектор одного chunk.

    Attributes:
        app_user_id: Внутренний ID владельца данных.
        vault_id: ID активного vault пользователя.
        chunk_id: ID исходного структурного chunk.
        document_model: Модель, которой получен вектор документа.
        dimension: Число float32-координат.
        content_hash: Hash текста chunk на момент векторизации.
        values: Декодированные координаты вектора.
        updated_at: Время последней записи в SQLite.
    """

    app_user_id: int
    vault_id: int
    chunk_id: int
    document_model: str
    dimension: int
    content_hash: str
    values: tuple[float, ...]
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if min(self.app_user_id, self.vault_id, self.chunk_id) <= 0:
            raise ValueError("embedding IDs must be positive")
        if not self.document_model.strip():
            raise ValueError("document_model must not be empty")
        if self.dimension <= 0 or self.dimension != len(self.values):
            raise ValueError("dimension must match non-empty values")
        if any(not isfinite(value) for value in self.values):
            raise ValueError("values must contain only finite numbers")
        if not self.content_hash.strip():
            raise ValueError("content_hash must not be empty")


@dataclass(frozen=True, slots=True)
class VaultChunkEmbeddingMetadata:
    """Описывает совместимость vector без загрузки тяжёлого BLOB из SQLite."""

    app_user_id: int
    vault_id: int
    chunk_id: int
    document_model: str
    dimension: int
    content_hash: str

    def __post_init__(self) -> None:
        if min(self.app_user_id, self.vault_id, self.chunk_id) <= 0:
            raise ValueError("embedding metadata IDs must be positive")
        if not self.document_model.strip():
            raise ValueError("document_model must not be empty")
        if self.dimension <= 0:
            raise ValueError("dimension must be positive")
        if not self.content_hash.strip():
            raise ValueError("content_hash must not be empty")


@dataclass(frozen=True, slots=True)
class VaultEmbeddingIndexState:
    """Фиксирует полностью согласованный профиль embedding-индекса vault."""

    app_user_id: int
    vault_id: int
    chunk_index_signature: str
    document_model: str
    query_model: str
    dimension: int | None
    indexed_at: datetime

    def __post_init__(self) -> None:
        if min(self.app_user_id, self.vault_id) <= 0:
            raise ValueError("embedding state IDs must be positive")
        if not self.chunk_index_signature.strip():
            raise ValueError("chunk_index_signature must not be empty")
        if not self.document_model.strip() or not self.query_model.strip():
            raise ValueError("embedding models must not be empty")
        if self.dimension is not None and self.dimension <= 0:
            raise ValueError("dimension must be positive when present")


@dataclass(frozen=True, slots=True)
class ArticleSearchQuery:
    """Представляет два компактных поисковых представления одной статьи.

    Attributes:
        app_user_id: Внутренний ID владельца статьи и будущей выдачи.
        article_id: ID проанализированной статьи.
        analysis_id: ID сохранённого LLM-анализа, если он уже назначен.
        semantic_text: Текст для embedding query и vector search.
        lexical_text: Короткий текст терминов для полнотекстового поиска.
    """

    app_user_id: int
    article_id: int
    analysis_id: int | None
    semantic_text: str
    lexical_text: str

    def __post_init__(self) -> None:
        if min(self.app_user_id, self.article_id) <= 0:
            raise ValueError("search query IDs must be positive")
        if self.analysis_id is not None and self.analysis_id <= 0:
            raise ValueError("analysis_id must be positive when present")
        if not self.semantic_text.strip():
            raise ValueError("semantic_text must not be empty")
        if not self.lexical_text.strip():
            raise ValueError("lexical_text must not be empty")


@dataclass(frozen=True, slots=True)
class VaultHybridSearchHit:
    """Представляет chunk после объединения lexical и semantic рангов.

    Attributes:
        chunk: Сохранённый chunk текущего пользователя и vault.
        score: Суммарный Reciprocal Rank Fusion score.
        lexical_rank: Позиция BM25-ветви, начиная с единицы.
        vector_rank: Позиция vector-ветви, начиная с единицы.
        lexical_score: Исходный BM25 score для диагностики.
        vector_score: Нормализованный cosine score от нуля до единицы.
    """

    chunk: VaultNoteChunk
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None
    lexical_score: float | None = None
    vector_score: float | None = None

    def __post_init__(self) -> None:
        if not isfinite(self.score) or self.score <= 0:
            raise ValueError("hybrid score must be finite and positive")
        for rank in (self.lexical_rank, self.vector_rank):
            if rank is not None and rank <= 0:
                raise ValueError("search ranks must be positive when present")
        for branch_score in (self.lexical_score, self.vector_score):
            if branch_score is not None and (
                not isfinite(branch_score) or branch_score < 0
            ):
                raise ValueError("branch scores must be finite and not negative")
        if self.vector_score is not None and self.vector_score > 1:
            raise ValueError("vector_score must not be greater than one")
        if self.lexical_rank is None and self.vector_rank is None:
            raise ValueError("hybrid hit must belong to at least one branch")
