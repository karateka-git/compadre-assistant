"""Unit-тесты vector search и Reciprocal Rank Fusion Этапа 10.7."""

from dataclasses import replace
from datetime import UTC, datetime
import unittest

from obs_chat_bot.capabilities.obsidian.application.search.errors import (
    EmbeddingProviderError,
    SearchIndexCorruptedError,
    SearchIndexUnavailableError,
)
from obs_chat_bot.capabilities.obsidian.application.search.hybrid import (
    VaultFtsFallbackSearchService,
    VaultHybridSearchService,
)
from obs_chat_bot.capabilities.obsidian.application.search.vector import VaultVectorSearchService
from obs_chat_bot.capabilities.obsidian.domain.search.entities import (
    ArticleSearchQuery,
    EmbeddingVector,
    VaultChunkEmbedding,
    VaultChunkIndexState,
    VaultChunkSearchHit,
    VaultEmbeddingIndexState,
    VaultNoteChunk,
)
from obs_chat_bot.capabilities.obsidian.domain.search.statuses import (
    VaultSearchFallbackReason,
    VaultSearchMode,
)


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
SIGNATURE = "parser-v1:policy-hash"


class MemoryChunkRepository:
    """Возвращает заранее согласованные chunks и generation marker."""

    def __init__(self, chunks, *, signature: str = SIGNATURE) -> None:
        self.chunks = list(chunks)
        self.signature = signature

    def get_state(self, *, app_user_id, vault_id):
        """Возвращает marker текущего chunk index."""
        return VaultChunkIndexState(
            app_user_id=app_user_id,
            vault_id=vault_id,
            index_signature=self.signature,
            indexed_at=NOW,
        )

    def list_for_vault(self, **_values):
        """Возвращает тестовые chunks."""
        return list(self.chunks)


class MemoryEmbeddingRepository:
    """Возвращает vectors и профиль embedding generation."""

    def __init__(
        self,
        embeddings,
        *,
        signature: str = SIGNATURE,
        document_model: str = "doc-model",
        query_model: str = "query-model",
        dimension: int | None = 2,
    ) -> None:
        self.embeddings = list(embeddings)
        self.signature = signature
        self.document_model = document_model
        self.query_model = query_model
        self.dimension = dimension

    def get_state(self, *, app_user_id, vault_id):
        """Возвращает тестовый embedding profile."""
        return VaultEmbeddingIndexState(
            app_user_id=app_user_id,
            vault_id=vault_id,
            chunk_index_signature=self.signature,
            document_model=self.document_model,
            query_model=self.query_model,
            dimension=self.dimension,
            indexed_at=NOW,
        )

    def list_for_vault(self, **_values):
        """Возвращает тестовые document vectors."""
        return list(self.embeddings)


class QueryEmbeddingProvider:
    """Возвращает управляемый query vector и считает внешние вызовы."""

    document_model = "doc-model"
    query_model = "query-model"

    def __init__(
        self,
        values: tuple[float, ...] = (1.0, 0.0),
        *,
        model: str = "query-model",
    ) -> None:
        self.values = values
        self.model = model
        self.calls: list[str] = []
        self.contexts = []

    def embed_query(self, text: str, *, context=None) -> EmbeddingVector:
        """Имитирует один оплачиваемый semantic query."""
        self.calls.append(text)
        self.contexts.append(context)
        return EmbeddingVector(model=self.model, values=self.values)

    def iter_document_batches(self, texts, *, context=None):
        """Не используется retrieval-сервисом."""
        raise AssertionError(f"Unexpected document embedding call: {texts}")


class StaticChunker:
    """Предоставляет ожидаемую signature без реального parsing."""

    index_signature = SIGNATURE


class RecordingSearch:
    """Возвращает заранее ранжированную ветвь и запоминает query."""

    def __init__(self, hits=(), *, error: Exception | None = None) -> None:
        self.hits = tuple(hits)
        self.error = error
        self.calls = []

    def search(self, **values):
        """Возвращает hits либо пробрасывает настроенную ошибку."""
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        return self.hits


class VaultVectorSearchServiceTest(unittest.TestCase):
    """Проверяет cosine ranking и строгую совместимость generation."""

    def test_ranks_current_embeddings_by_normalized_cosine(self) -> None:
        """Совпадающее направление получает score 1 и первое место."""
        first = _chunk(1, path="first.md", content_hash="hash-1")
        second = _chunk(2, path="second.md", content_hash="hash-2")
        provider = QueryEmbeddingProvider(values=(1.0, 0.0))
        search = _vector_search(
            chunks=(first, second),
            embeddings=(
                _embedding(first, values=(1.0, 0.0)),
                _embedding(second, values=(0.0, 1.0)),
            ),
            provider=provider,
        )

        hits = search.search(
            app_user_id=1,
            vault_id=10,
            query="semantic query",
        )

        self.assertEqual([hit.chunk.id for hit in hits], [1, 2])
        self.assertAlmostEqual(hits[0].score, 1.0)
        self.assertAlmostEqual(hits[1].score, 0.5)
        self.assertEqual(provider.calls, ["semantic query"])
        self.assertEqual(provider.contexts[0].app_user_id, 1)
        self.assertEqual(provider.contexts[0].vault_id, 10)

    def test_rejects_stale_profile_before_paid_query(self) -> None:
        """Несовместимый marker не вызывает embedding provider."""
        chunk = _chunk(1)
        provider = QueryEmbeddingProvider()
        search = VaultVectorSearchService(
            chunk_repository=MemoryChunkRepository((chunk,)),
            embedding_repository=MemoryEmbeddingRepository(
                (_embedding(chunk),),
                query_model="other-query-model",
            ),
            embedding_provider=provider,
            chunker=StaticChunker(),
        )

        with self.assertRaises(SearchIndexUnavailableError):
            search.search(app_user_id=1, vault_id=10, query="query")

        self.assertEqual(provider.calls, [])

    def test_rejects_missing_or_stale_vector_before_paid_query(self) -> None:
        """Несовпадение IDs/content hash не маскируется текущим marker."""
        chunk = _chunk(1, content_hash="current")
        provider = QueryEmbeddingProvider()
        search = _vector_search(
            chunks=(chunk,),
            embeddings=(_embedding(chunk, content_hash="stale"),),
            provider=provider,
        )

        with self.assertRaises(SearchIndexCorruptedError):
            search.search(app_user_id=1, vault_id=10, query="query")

        self.assertEqual(provider.calls, [])

    def test_rejects_query_dimension_and_zero_vectors(self) -> None:
        """Cosine не выполняется с несовместимым query или нулевым document."""
        chunk = _chunk(1)
        wrong_dimension = _vector_search(
            chunks=(chunk,),
            embeddings=(_embedding(chunk),),
            provider=QueryEmbeddingProvider(values=(1.0, 0.0, 0.0)),
        )
        with self.assertRaises(EmbeddingProviderError):
            wrong_dimension.search(app_user_id=1, vault_id=10, query="query")

        zero_provider = QueryEmbeddingProvider()
        zero_document = _vector_search(
            chunks=(chunk,),
            embeddings=(_embedding(chunk, values=(0.0, 0.0)),),
            provider=zero_provider,
        )
        with self.assertRaises(SearchIndexCorruptedError):
            zero_document.search(app_user_id=1, vault_id=10, query="query")
        self.assertEqual(zero_provider.calls, [])

    def test_empty_current_generation_skips_provider(self) -> None:
        """Пустой vault возвращает пустую выдачу без платного запроса."""
        provider = QueryEmbeddingProvider()
        search = VaultVectorSearchService(
            chunk_repository=MemoryChunkRepository(()),
            embedding_repository=MemoryEmbeddingRepository((), dimension=None),
            embedding_provider=provider,
            chunker=StaticChunker(),
        )

        hits = search.search(app_user_id=1, vault_id=10, query="query")

        self.assertEqual(hits, ())
        self.assertEqual(provider.calls, [])


class VaultHybridSearchServiceTest(unittest.TestCase):
    """Проверяет RRF ranks и независимое выполнение двух ветвей."""

    def test_rrf_prefers_chunk_present_in_both_branches(self) -> None:
        """Общий второй/первый кандидат обгоняет лидеров одной ветви."""
        lexical_only = _chunk(1, path="lexical.md")
        common = _chunk(2, path="common.md")
        vector_only = _chunk(3, path="vector.md")
        lexical = RecordingSearch(
            (
                VaultChunkSearchHit(chunk=lexical_only, score=5.0),
                VaultChunkSearchHit(chunk=common, score=2.0),
            )
        )
        vector = RecordingSearch(
            (
                VaultChunkSearchHit(chunk=common, score=0.95),
                VaultChunkSearchHit(chunk=vector_only, score=0.90),
            )
        )
        service = VaultHybridSearchService(
            lexical_search=lexical,
            vector_search=vector,
        )

        result = service.search(query=_query(), vault_id=10)

        self.assertEqual([hit.chunk.id for hit in result.hits], [2, 1, 3])
        common_hit = result.hits[0]
        self.assertEqual((common_hit.lexical_rank, common_hit.vector_rank), (2, 1))
        self.assertEqual(
            (common_hit.lexical_score, common_hit.vector_score),
            (2.0, 0.95),
        )
        self.assertEqual(result.lexical_candidates, 2)
        self.assertEqual(result.vector_candidates, 2)
        self.assertIs(result.mode, VaultSearchMode.HYBRID)
        self.assertIsNone(result.fallback_reason)
        self.assertEqual(lexical.calls[0]["query"], "Docker webhook")
        self.assertIn("Кратко", vector.calls[0]["query"])

    def test_result_limit_is_applied_after_fusion(self) -> None:
        """Итоговый limit не обрезает кандидатов до объединения ranks."""
        chunks = tuple(_chunk(index) for index in range(1, 4))
        lexical = RecordingSearch(
            tuple(
                VaultChunkSearchHit(chunk=chunk, score=float(4 - rank))
                for rank, chunk in enumerate(chunks, start=1)
            )
        )
        vector = RecordingSearch(
            tuple(
                VaultChunkSearchHit(chunk=chunk, score=0.9)
                for chunk in reversed(chunks)
            )
        )

        result = VaultHybridSearchService(
            lexical_search=lexical,
            vector_search=vector,
        ).search(
            query=_query(),
            vault_id=10,
            candidate_limit=3,
            result_limit=2,
        )

        self.assertEqual(len(result.hits), 2)
        self.assertEqual(lexical.calls[0]["limit"], 3)
        self.assertEqual(vector.calls[0]["limit"], 3)

    def test_embedding_provider_failure_returns_explicit_fts_fallback(self) -> None:
        """Provider error сохраняет BM25 order и типизированную причину."""
        error = EmbeddingProviderError(
            "provider unavailable",
            operation_id="operation-42",
        )
        chunk = _chunk(1)
        lexical = RecordingSearch((VaultChunkSearchHit(chunk=chunk, score=3.0),))
        vector = RecordingSearch(error=error)
        service = VaultHybridSearchService(
            lexical_search=lexical,
            vector_search=vector,
        )

        with self.assertLogs(
            "obs_chat_bot.capabilities.obsidian.application.search.hybrid",
            level="WARNING",
        ) as captured:
            result = service.search(query=_query(), vault_id=10)

        self.assertIs(result.mode, VaultSearchMode.FTS_FALLBACK)
        self.assertIs(
            result.fallback_reason,
            VaultSearchFallbackReason.EMBEDDING_PROVIDER_FAILED,
        )
        self.assertEqual(result.vector_candidates, 0)
        self.assertEqual([hit.chunk.id for hit in result.hits], [1])
        self.assertEqual(result.hits[0].lexical_rank, 1)
        self.assertIsNone(result.hits[0].vector_rank)
        self.assertEqual(len(lexical.calls), 1)
        self.assertEqual(len(vector.calls), 1)
        self.assertIn("event=embedding_fallback", captured.output[0])
        self.assertIn("operation_id=operation-42", captured.output[0])
        self.assertIn("article_id=50", captured.output[0])

    def test_embedding_index_failure_has_separate_fallback_reason(self) -> None:
        """Stale index отличается от сетевого или provider failure."""
        service = VaultHybridSearchService(
            lexical_search=RecordingSearch(()),
            vector_search=RecordingSearch(
                error=SearchIndexUnavailableError("stale index")
            ),
        )

        result = service.search(query=_query(), vault_id=10)

        self.assertIs(result.mode, VaultSearchMode.FTS_FALLBACK)
        self.assertIs(
            result.fallback_reason,
            VaultSearchFallbackReason.EMBEDDING_INDEX_UNAVAILABLE,
        )
        self.assertEqual(result.hits, ())

    def test_missing_embedding_config_has_explicit_fts_only_reason(self) -> None:
        """Отключённый semantic provider не изображает hybrid retrieval."""
        chunk = _chunk(1)
        result = VaultFtsFallbackSearchService(
            lexical_search=RecordingSearch(
                (VaultChunkSearchHit(chunk=chunk, score=2.0),)
            )
        ).search(query=_query(), vault_id=10)

        self.assertIs(result.mode, VaultSearchMode.FTS_FALLBACK)
        self.assertIs(
            result.fallback_reason,
            VaultSearchFallbackReason.EMBEDDING_NOT_CONFIGURED,
        )
        self.assertEqual(result.vector_candidates, 0)
        self.assertEqual(result.hits[0].lexical_rank, 1)

    def test_unexpected_vector_error_is_not_hidden_by_fallback(self) -> None:
        """Ошибка программирования или storage вне контракта пробрасывается."""
        service = VaultHybridSearchService(
            lexical_search=RecordingSearch(()),
            vector_search=RecordingSearch(error=RuntimeError("unexpected")),
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            service.search(query=_query(), vault_id=10)

    def test_corrupted_index_is_not_hidden_by_fallback(self) -> None:
        """Нарушение опубликованных invariants требует диагностики и ремонта."""
        service = VaultHybridSearchService(
            lexical_search=RecordingSearch(()),
            vector_search=RecordingSearch(
                error=SearchIndexCorruptedError("hash mismatch")
            ),
        )

        with self.assertRaisesRegex(SearchIndexCorruptedError, "hash mismatch"):
            service.search(query=_query(), vault_id=10)

    def test_duplicate_chunk_in_branch_is_rejected(self) -> None:
        """Некорректная ветвь не искажает RRF повтором одного chunk."""
        chunk = _chunk(1)
        duplicate = (
            VaultChunkSearchHit(chunk=chunk, score=2.0),
            VaultChunkSearchHit(chunk=chunk, score=1.0),
        )
        service = VaultHybridSearchService(
            lexical_search=RecordingSearch(duplicate),
            vector_search=RecordingSearch(()),
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            service.search(query=_query(), vault_id=10)

    def test_branch_cannot_return_another_users_chunk(self) -> None:
        """Tenant isolation проверяется до RRF и итогового limit."""
        foreign = replace(_chunk(1), app_user_id=2)
        service = VaultHybridSearchService(
            lexical_search=RecordingSearch(
                (VaultChunkSearchHit(chunk=foreign, score=1.0),)
            ),
            vector_search=RecordingSearch(()),
        )

        with self.assertRaisesRegex(ValueError, "scope"):
            service.search(query=_query(), vault_id=10)


def _vector_search(*, chunks, embeddings, provider):
    """Собирает vector search с memory repositories."""
    return VaultVectorSearchService(
        chunk_repository=MemoryChunkRepository(chunks),
        embedding_repository=MemoryEmbeddingRepository(embeddings),
        embedding_provider=provider,
        chunker=StaticChunker(),
    )


def _chunk(
    chunk_id: int,
    *,
    path: str | None = None,
    content_hash: str | None = None,
) -> VaultNoteChunk:
    """Создаёт сохранённый chunk одного пользователя и vault."""
    return VaultNoteChunk(
        id=chunk_id,
        app_user_id=1,
        vault_id=10,
        note_id=100 + chunk_id,
        note_path=path or f"note-{chunk_id}.md",
        chunk_key=f"chunk-{chunk_id}",
        position=0,
        heading_path=(f"Note {chunk_id}",),
        part_index=0,
        text=f"Chunk text {chunk_id}",
        content_hash=content_hash or f"hash-{chunk_id}",
    )


def _embedding(
    chunk: VaultNoteChunk,
    *,
    values: tuple[float, ...] = (1.0, 0.0),
    content_hash: str | None = None,
) -> VaultChunkEmbedding:
    """Создаёт сохранённый document vector выбранного chunk."""
    if chunk.id is None:
        raise ValueError("chunk must be saved")
    return VaultChunkEmbedding(
        app_user_id=chunk.app_user_id,
        vault_id=chunk.vault_id,
        chunk_id=chunk.id,
        document_model="doc-model",
        dimension=len(values),
        content_hash=content_hash or chunk.content_hash,
        values=values,
    )


def _query() -> ArticleSearchQuery:
    """Создаёт compact query Этапа 10.6."""
    return ArticleSearchQuery(
        app_user_id=1,
        article_id=50,
        analysis_id=60,
        semantic_text="Название: Docker\nКратко: Масштабирование webhook",
        lexical_text="Docker webhook",
    )


if __name__ == "__main__":
    unittest.main()
