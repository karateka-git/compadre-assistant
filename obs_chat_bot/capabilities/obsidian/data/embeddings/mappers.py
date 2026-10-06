"""Преобразования embedding DTO в domain-модели."""

from __future__ import annotations

from obs_chat_bot.capabilities.obsidian.data.embeddings.dtos import EmbeddingResponseItemDto
from obs_chat_bot.capabilities.obsidian.domain.search.entities import EmbeddingVector


def embedding_vector_from_dto(
    dto: EmbeddingResponseItemDto,
    *,
    model: str,
) -> EmbeddingVector:
    """Добавляет ID модели к проверенным координатам ответа provider."""
    return EmbeddingVector(model=model, values=dto.values)
