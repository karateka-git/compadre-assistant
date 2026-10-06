"""Application-сервис cosine search по embeddings chunks одного vault."""

from __future__ import annotations

from math import sqrt

from obs_chat_bot.capabilities.obsidian.application.search.errors import (
    EmbeddingProviderError,
    SearchIndexCorruptedError,
    SearchIndexUnavailableError,
)
from obs_chat_bot.capabilities.obsidian.application.search.models import EmbeddingCallContext
from obs_chat_bot.capabilities.obsidian.application.search.ports import (
    EmbeddingProvider,
    VaultChunkIndexRepository,
    VaultEmbeddingIndexRepository,
    VaultNoteChunker,
)
from obs_chat_bot.capabilities.obsidian.domain.search.entities import (
    EmbeddingVector,
    VaultChunkEmbedding,
    VaultChunkSearchHit,
    VaultNoteChunk,
)


class VaultVectorSearchService:
    """Ранжирует chunks по cosine similarity только актуального поколения."""

    def __init__(
        self,
        *,
        chunk_repository: VaultChunkIndexRepository,
        embedding_repository: VaultEmbeddingIndexRepository,
        embedding_provider: EmbeddingProvider,
        chunker: VaultNoteChunker,
    ) -> None:
        self._chunk_repository = chunk_repository
        self._embedding_repository = embedding_repository
        self._embedding_provider = embedding_provider
        self._chunker = chunker

    def search(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        query: str,
        limit: int = 10,
        article_id: int | None = None,
    ) -> tuple[VaultChunkSearchHit, ...]:
        """Векторизует query и возвращает ближайшие chunks.

        Args:
            app_user_id: Внутренний ID пользователя приложения.
            vault_id: ID активного vault пользователя.
            query: Непустой compact semantic query Этапа 10.6.
            limit: Максимальное число результатов от 1 до 100.
            article_id: ID статьи для безопасной корреляции embedding-логов.

        Returns:
            Chunks с нормализованным cosine score от нуля до единицы.

        Raises:
            ValueError: Scope, query или limit некорректны.
            SearchIndexUnavailableError: Поколение отсутствует, устарело либо
                не соответствует текущим моделям.
            SearchIndexCorruptedError: Опубликованные rows нарушают invariants.
            EmbeddingProviderError: Query vector несовместим или provider
                вернул нулевой vector.
        """
        _validate_search_arguments(
            app_user_id=app_user_id,
            vault_id=vault_id,
            query=query,
            limit=limit,
        )
        chunks, embeddings, dimension = self._load_current_generation(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        if not chunks:
            return ()
        query_vector = self._embedding_provider.embed_query(
            query,
            context=EmbeddingCallContext(
                app_user_id=app_user_id,
                vault_id=vault_id,
                article_id=article_id,
            ),
        )
        self._validate_query_vector(query_vector, dimension=dimension)
        chunk_by_id = {chunk.id: chunk for chunk in chunks}
        hits = [
            VaultChunkSearchHit(
                chunk=chunk_by_id[embedding.chunk_id],
                score=_normalized_cosine(query_vector.values, embedding.values),
            )
            for embedding in embeddings
        ]
        hits.sort(
            key=lambda hit: (
                -hit.score,
                hit.chunk.note_path,
                hit.chunk.position,
            )
        )
        return tuple(hits[:limit])

    def _load_current_generation(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> tuple[
        list[VaultNoteChunk],
        list[VaultChunkEmbedding],
        int | None,
    ]:
        """Загружает и перекрёстно проверяет marker, chunks и vectors."""
        chunk_state = self._chunk_repository.get_state(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        embedding_state = self._embedding_repository.get_state(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        expected_signature = self._chunker.index_signature
        if (
            chunk_state is None
            or chunk_state.index_signature != expected_signature
            or embedding_state is None
            or embedding_state.chunk_index_signature != expected_signature
            or embedding_state.document_model
            != self._embedding_provider.document_model
            or embedding_state.query_model != self._embedding_provider.query_model
        ):
            raise SearchIndexUnavailableError(
                "Search index generation does not match current profile"
            )
        chunks = self._chunk_repository.list_for_vault(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        embeddings = self._embedding_repository.list_for_vault(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        if any(
            chunk.app_user_id != app_user_id or chunk.vault_id != vault_id
            for chunk in chunks
        ) or any(
            embedding.app_user_id != app_user_id
            or embedding.vault_id != vault_id
            for embedding in embeddings
        ):
            raise SearchIndexCorruptedError(
                "Search index contains data outside requested user or vault"
            )
        if not chunks:
            if embeddings or embedding_state.dimension is not None:
                raise SearchIndexCorruptedError(
                    "Empty chunk generation has unexpected embeddings"
                )
            return chunks, embeddings, None
        if embedding_state.dimension is None:
            raise SearchIndexCorruptedError(
                "Non-empty embedding generation has no dimension"
            )
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        embeddings_by_id = {embedding.chunk_id: embedding for embedding in embeddings}
        if (
            None in chunks_by_id
            or len(chunks_by_id) != len(chunks)
            or len(embeddings_by_id) != len(embeddings)
            or set(chunks_by_id) != set(embeddings_by_id)
        ):
            raise SearchIndexCorruptedError(
                "Chunk and embedding generations do not contain the same IDs"
            )
        for embedding in embeddings:
            chunk = chunks_by_id[embedding.chunk_id]
            if (
                embedding.document_model != embedding_state.document_model
                or embedding.dimension != embedding_state.dimension
                or embedding.content_hash != chunk.content_hash
            ):
                raise SearchIndexCorruptedError(
                    "Stored embedding does not match current chunk or profile"
                )
            if not any(embedding.values):
                raise SearchIndexCorruptedError(
                    "Stored embedding must not be a zero vector"
                )
        return chunks, embeddings, embedding_state.dimension

    def _validate_query_vector(
        self,
        vector: EmbeddingVector,
        *,
        dimension: int | None,
    ) -> None:
        """Проверяет query model и dimension перед cosine calculation."""
        if (
            vector.model != self._embedding_provider.query_model
            or dimension is None
            or vector.dimension != dimension
        ):
            raise EmbeddingProviderError(
                "Query embedding does not match stored index profile"
            )


def _validate_search_arguments(
    *,
    app_user_id: int,
    vault_id: int,
    query: str,
    limit: int,
) -> None:
    """Проверяет общие границы vector search до чтения storage и API."""
    if app_user_id <= 0:
        raise ValueError("app_user_id must be positive")
    if vault_id <= 0:
        raise ValueError("vault_id must be positive")
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def _normalized_cosine(
    left: tuple[float, ...],
    right: tuple[float, ...],
) -> float:
    """Возвращает cosine similarity, линейно приведённый к диапазону 0..1."""
    if len(left) != len(right) or not left:
        raise SearchIndexCorruptedError("Vector dimensions are incompatible")
    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0:
        raise EmbeddingProviderError("Query embedding must not be a zero vector")
    if right_norm == 0:
        raise SearchIndexCorruptedError(
            "Stored embedding must not be a zero vector"
        )
    cosine = dot_product / (left_norm * right_norm)
    return min(1.0, max(0.0, (cosine + 1.0) / 2.0))
