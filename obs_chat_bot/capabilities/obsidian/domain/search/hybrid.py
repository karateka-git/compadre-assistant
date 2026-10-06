"""Application-сервис объединения lexical и vector ranks через RRF."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from obs_chat_bot.capabilities.obsidian.domain.search.errors import (
    EmbeddingProviderError,
    SearchIndexUnavailableError,
)
from obs_chat_bot.capabilities.obsidian.domain.search.models import VaultSearchResult
from obs_chat_bot.capabilities.obsidian.domain.search.ports import VaultChunkSearch
from obs_chat_bot.capabilities.obsidian.domain.search.entities import (
    ArticleSearchQuery,
    VaultChunkSearchHit,
    VaultHybridSearchHit,
    VaultNoteChunk,
)
from obs_chat_bot.capabilities.obsidian.domain.search.statuses import (
    VaultSearchFallbackReason,
    VaultSearchMode,
)


DEFAULT_RRF_K = 60
DEFAULT_CANDIDATE_LIMIT = 20
DEFAULT_RESULT_LIMIT = 10
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _FusionEntry:
    """Накапливает ranks и scores одного chunk до создания domain hit."""

    chunk: VaultNoteChunk
    score: float = 0.0
    lexical_rank: int | None = None
    vector_rank: int | None = None
    lexical_score: float | None = None
    vector_score: float | None = None


class VaultHybridSearchService:
    """Независимо получает две выдачи и объединяет их по reciprocal ranks."""

    def __init__(
        self,
        *,
        lexical_search: VaultChunkSearch,
        vector_search: VaultChunkSearch,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> None:
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        self._lexical_search = lexical_search
        self._vector_search = vector_search
        self._rrf_k = rrf_k

    def search(
        self,
        *,
        query: ArticleSearchQuery,
        vault_id: int,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        result_limit: int = DEFAULT_RESULT_LIMIT,
    ) -> VaultSearchResult:
        """Выполняет обе ветви и возвращает лучшие chunks после RRF.

        Args:
            query: Два compact search representation Этапа 10.6.
            vault_id: ID активного vault того же пользователя.
            candidate_limit: Число кандидатов от каждой ветви, от 1 до 100.
            result_limit: Итоговое число chunks, от 1 до candidate_limit.

        Returns:
            Hybrid result с branch ranks и исходными scores для диагностики.

        Raises:
            ValueError: Vault или limits некорректны.
            RuntimeError: Неожиданная ошибка ветви, не относящаяся к ожидаемым
                semantic failures.
        """
        if vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if not 1 <= candidate_limit <= 100:
            raise ValueError("candidate_limit must be between 1 and 100")
        if not 1 <= result_limit <= candidate_limit:
            raise ValueError("result_limit must be between 1 and candidate_limit")
        lexical_hits = self._lexical_search.search(
            app_user_id=query.app_user_id,
            vault_id=vault_id,
            query=query.lexical_text,
            limit=candidate_limit,
            article_id=query.article_id,
        )
        _validate_branch_scope(
            lexical_hits,
            app_user_id=query.app_user_id,
            vault_id=vault_id,
        )
        try:
            vector_hits = self._vector_search.search(
                app_user_id=query.app_user_id,
                vault_id=vault_id,
                query=query.semantic_text,
                limit=candidate_limit,
                article_id=query.article_id,
            )
        except SearchIndexUnavailableError as error:
            return self._build_fallback_result(
                query=query,
                vault_id=vault_id,
                lexical_hits=lexical_hits,
                result_limit=result_limit,
                reason=VaultSearchFallbackReason.EMBEDDING_INDEX_UNAVAILABLE,
                error=error,
            )
        except EmbeddingProviderError as error:
            return self._build_fallback_result(
                query=query,
                vault_id=vault_id,
                lexical_hits=lexical_hits,
                result_limit=result_limit,
                reason=VaultSearchFallbackReason.EMBEDDING_PROVIDER_FAILED,
                error=error,
            )
        _validate_branch_scope(
            vector_hits,
            app_user_id=query.app_user_id,
            vault_id=vault_id,
        )
        fused = _reciprocal_rank_fusion(
            lexical_hits=lexical_hits,
            vector_hits=vector_hits,
            rrf_k=self._rrf_k,
        )
        return VaultSearchResult(
            query=query,
            vault_id=vault_id,
            hits=fused[:result_limit],
            lexical_candidates=len(lexical_hits),
            vector_candidates=len(vector_hits),
        )

    def _build_fallback_result(
        self,
        *,
        query: ArticleSearchQuery,
        vault_id: int,
        lexical_hits: tuple[VaultChunkSearchHit, ...],
        result_limit: int,
        reason: VaultSearchFallbackReason,
        error: Exception,
    ) -> VaultSearchResult:
        """Сохраняет BM25 order и явно маркирует ожидаемый semantic failure."""
        LOGGER.warning(
            "Semantic search unavailable, using FTS fallback: "
            "event=embedding_fallback operation_id=%s app_user_id=%s "
            "vault_id=%s article_id=%s reason=%s error_type=%s",
            getattr(error, "operation_id", None) or "none",
            query.app_user_id,
            vault_id,
            query.article_id,
            reason.value,
            type(error).__name__,
        )
        fused = _reciprocal_rank_fusion(
            lexical_hits=lexical_hits,
            vector_hits=(),
            rrf_k=self._rrf_k,
        )
        return VaultSearchResult(
            query=query,
            vault_id=vault_id,
            hits=fused[:result_limit],
            lexical_candidates=len(lexical_hits),
            vector_candidates=0,
            mode=VaultSearchMode.FTS_FALLBACK,
            fallback_reason=reason,
        )


class VaultFtsFallbackSearchService:
    """Выполняет честный FTS-only поиск при отключённом embedding provider."""

    def __init__(self, *, lexical_search: VaultChunkSearch) -> None:
        self._lexical_search = lexical_search

    def search(
        self,
        *,
        query: ArticleSearchQuery,
        vault_id: int,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        result_limit: int = DEFAULT_RESULT_LIMIT,
    ) -> VaultSearchResult:
        """Возвращает BM25-выдачу с явной причиной отсутствия semantic ветви."""
        if vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if not 1 <= candidate_limit <= 100:
            raise ValueError("candidate_limit must be between 1 and 100")
        if not 1 <= result_limit <= candidate_limit:
            raise ValueError("result_limit must be between 1 and candidate_limit")
        lexical_hits = self._lexical_search.search(
            app_user_id=query.app_user_id,
            vault_id=vault_id,
            query=query.lexical_text,
            limit=candidate_limit,
            article_id=query.article_id,
        )
        _validate_branch_scope(
            lexical_hits,
            app_user_id=query.app_user_id,
            vault_id=vault_id,
        )
        fused = _reciprocal_rank_fusion(
            lexical_hits=lexical_hits,
            vector_hits=(),
            rrf_k=DEFAULT_RRF_K,
        )
        return VaultSearchResult(
            query=query,
            vault_id=vault_id,
            hits=fused[:result_limit],
            lexical_candidates=len(lexical_hits),
            vector_candidates=0,
            mode=VaultSearchMode.FTS_FALLBACK,
            fallback_reason=VaultSearchFallbackReason.EMBEDDING_NOT_CONFIGURED,
        )


def _validate_branch_scope(
    hits: tuple[VaultChunkSearchHit, ...],
    *,
    app_user_id: int,
    vault_id: int,
) -> None:
    """Не позволяет malformed adapter смешать tenants до RRF и итогового limit."""
    if any(
        hit.chunk.app_user_id != app_user_id or hit.chunk.vault_id != vault_id
        for hit in hits
    ):
        raise ValueError("search branch returned chunk outside requested scope")


def _reciprocal_rank_fusion(
    *,
    lexical_hits: tuple[VaultChunkSearchHit, ...],
    vector_hits: tuple[VaultChunkSearchHit, ...],
    rrf_k: int,
) -> tuple[VaultHybridSearchHit, ...]:
    """Суммирует reciprocal ranks двух выдач по стабильному SQLite chunk ID."""
    entries: dict[int, _FusionEntry] = {}
    _add_branch(entries, lexical_hits, branch="lexical", rrf_k=rrf_k)
    _add_branch(entries, vector_hits, branch="vector", rrf_k=rrf_k)
    fused = tuple(
        VaultHybridSearchHit(
            chunk=entry.chunk,
            score=entry.score,
            lexical_rank=entry.lexical_rank,
            vector_rank=entry.vector_rank,
            lexical_score=entry.lexical_score,
            vector_score=entry.vector_score,
        )
        for entry in entries.values()
    )
    return tuple(
        sorted(
            fused,
            key=lambda hit: (
                -hit.score,
                min(hit.lexical_rank or 10**9, hit.vector_rank or 10**9),
                hit.chunk.note_path,
                hit.chunk.position,
            ),
        )
    )


def _add_branch(
    entries: dict[int, _FusionEntry],
    hits: tuple[VaultChunkSearchHit, ...],
    *,
    branch: Literal["lexical", "vector"],
    rrf_k: int,
) -> None:
    """Добавляет одну ветвь в mutable accumulator с проверкой chunk identity."""
    seen: set[int] = set()
    for rank, hit in enumerate(hits, start=1):
        chunk_id = hit.chunk.id
        if chunk_id is None:
            raise ValueError("search hit chunk must be saved")
        if chunk_id in seen:
            raise ValueError("search branch must not contain duplicate chunks")
        seen.add(chunk_id)
        entry = entries.setdefault(
            chunk_id,
            _FusionEntry(chunk=hit.chunk),
        )
        if entry.chunk != hit.chunk:
            raise ValueError("search branches disagree about chunk identity")
        entry.score += 1.0 / (rrf_k + rank)
        if branch == "lexical":
            entry.lexical_rank = rank
            entry.lexical_score = hit.score
        else:
            entry.vector_rank = rank
            entry.vector_score = hit.score
