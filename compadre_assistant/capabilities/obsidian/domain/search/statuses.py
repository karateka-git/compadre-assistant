"""Статусы выполнения hybrid retrieval и его ограниченного fallback."""

from enum import StrEnum


class VaultSearchMode(StrEnum):
    """Описывает фактически использованные ветви поиска."""

    HYBRID = "hybrid"  # Результат объединяет FTS5 и semantic ranks.
    FTS_FALLBACK = "fts_fallback"  # Semantic-ветвь недоступна, оставлен FTS5.


class VaultSearchFallbackReason(StrEnum):
    """Описывает ожидаемую причину перехода к ограниченному FTS5."""

    EMBEDDING_INDEX_UNAVAILABLE = "embedding_index_unavailable"  # Stale index.
    EMBEDDING_PROVIDER_FAILED = "embedding_provider_failed"  # API/response error.
    EMBEDDING_NOT_CONFIGURED = "embedding_not_configured"  # FTS-only deployment.
