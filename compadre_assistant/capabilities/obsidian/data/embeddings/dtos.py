"""Технические DTO ответа OpenAI-compatible Embeddings API."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingResponseItemDto:
    """Представляет один упорядоченный embedding из внешнего ответа."""

    index: int
    values: tuple[float, ...]
