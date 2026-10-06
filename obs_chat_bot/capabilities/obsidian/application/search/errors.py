"""Типизированные ошибки semantic indexing и retrieval."""


class EmbeddingProviderError(RuntimeError):
    """Ошибка получения или проверки embeddings внешнего провайдера.

    `operation_id` — безопасный технический correlation ID для связи
    окончательного provider failure с последующим FTS fallback.
    """

    def __init__(self, message: str, *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.operation_id = operation_id


class SearchIndexUnavailableError(RuntimeError):
    """Текущее поколение поискового индекса отсутствует или несовместимо."""


class SearchIndexCorruptedError(RuntimeError):
    """Опубликованный поисковый индекс нарушает внутренние invariants."""
