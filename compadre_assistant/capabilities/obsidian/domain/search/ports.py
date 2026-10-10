"""Application ports разбиения vault-заметок на chunks."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Protocol

from compadre_assistant.capabilities.obsidian.domain.search.models import (
    ChunkIndexUpdate,
    EmbeddingCallContext,
    VaultChunkEmbeddingDraft,
    VaultNoteChunkDraft,
)
from compadre_assistant.capabilities.obsidian.domain.search.entities import (
    EmbeddingVector,
    VaultChunkEmbedding,
    VaultChunkEmbeddingMetadata,
    VaultChunkIndexState,
    VaultChunkSearchHit,
    VaultEmbeddingIndexState,
    VaultNoteChunk,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultNote


class VaultNoteChunker(Protocol):
    """Описывает project-facing разбиение сохранённой vault-заметки."""

    @property
    def index_signature(self) -> str:
        """Возвращает signature алгоритма и policy для будущего индекса."""

    def split(self, note: VaultNote) -> tuple[VaultNoteChunkDraft, ...]:
        """Разбивает сохранённую заметку и добавляет project IDs.

        Args:
            note: Vault-заметка с назначенным SQLite ID.

        Returns:
            Chunks, связанные с пользователем, vault и заметкой.

        Raises:
            ValueError: Если заметка ещё не сохранена и не имеет ID.
        """


class VaultChunkIndexRepository(Protocol):
    """Описывает storage chunks и signature согласованного индекса vault."""

    def get_state(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> VaultChunkIndexState | None:
        """Возвращает состояние полного индекса либо `None`, если он invalid."""

    def list_for_note(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_id: int,
    ) -> list[VaultNoteChunk]:
        """Возвращает chunks заметки в порядке позиции."""

    def list_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultNoteChunk]:
        """Возвращает все chunks vault в порядке заметки и позиции."""

    def invalidate(self, *, app_user_id: int, vault_id: int) -> None:
        """Удаляет marker согласованности до потенциально частичной записи."""

    def list_stale_note_ids(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        index_signature: str,
    ) -> set[int]:
        """Возвращает заметки без chunks для текущих blob SHA и signature."""

    def mark_note_current(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_id: int,
        source_blob_sha: str,
        index_signature: str,
    ) -> None:
        """Фиксирует готовность chunks одной исходной заметки."""

    def replace_for_note(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_id: int,
        chunks: tuple[VaultNoteChunkDraft, ...],
    ) -> ChunkIndexUpdate:
        """Атомарно приводит chunks одной заметки к переданному набору."""

    def delete_for_notes(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        note_ids: set[int],
    ) -> int:
        """Удаляет chunks перечисленных заметок и возвращает число строк."""

    def replace_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        chunks: tuple[VaultNoteChunkDraft, ...],
        index_signature: str,
    ) -> ChunkIndexUpdate:
        """Атомарно перестраивает все chunks и фиксирует новую signature."""

    def mark_current(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        index_signature: str,
    ) -> VaultChunkIndexState:
        """Фиксирует завершение успешного инкрементального обновления."""


class VaultFullTextSearchRepository(Protocol):
    """Описывает точный полнотекстовый поиск по сохранённым chunks vault."""

    def search(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        query: str,
        expected_index_signature: str,
        limit: int,
    ) -> tuple[VaultChunkSearchHit, ...]:
        """Возвращает релевантные chunks только из актуального поколения.

        Args:
            app_user_id: Внутренний ID пользователя приложения.
            vault_id: ID активного Obsidian vault.
            query: Пользовательский текст, а не сырой синтаксис FTS5.
            expected_index_signature: Signature текущих parser и policy.
            limit: Максимальное число результатов.

        Returns:
            Chunks в порядке убывания полнотекстовой релевантности.
        """


class VaultChunkSearch(Protocol):
    """Описывает одну независимо ранжированную ветвь поиска по chunks."""

    def search(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        query: str,
        limit: int = 10,
        article_id: int | None = None,
    ) -> tuple[VaultChunkSearchHit, ...]:
        """Возвращает chunks в порядке убывания релевантности ветви."""


class EmbeddingProvider(Protocol):
    """Описывает сменяемый источник semantic-векторов текста."""

    @property
    def document_model(self) -> str:
        """Возвращает ID модели corpus documents."""

    @property
    def query_model(self) -> str:
        """Возвращает ID совместимой модели поисковых запросов."""

    def iter_document_batches(
        self,
        texts: tuple[str, ...],
        *,
        context: EmbeddingCallContext | None = None,
    ) -> Generator[tuple[EmbeddingVector, ...], None, None]:
        """Векторизует corpus chunks, сохраняя порядок входных текстов.

        Args:
            texts: Непустые тексты документов; пустой tuple разрешён.
            context: Безопасный application scope для корреляции логов.

        Yields:
            Непустые проверенные пакеты в порядке `texts`. Следующий запрос
            начинается только после возобновления итератора. При сбое ранее
            выданные пакеты остаются пригодными для checkpoint-сохранения.

        Raises:
            ValueError: Если один из переданных текстов пуст.
            EmbeddingProviderError: Если provider недоступен или ответ неверен.
        """

    def embed_query(
        self,
        text: str,
        *,
        context: EmbeddingCallContext | None = None,
    ) -> EmbeddingVector:
        """Векторизует поисковый запрос в совместимое пространство.

        Args:
            text: Непустой текст поискового запроса.
            context: Безопасный application scope для корреляции логов.

        Returns:
            Вектор той же модели, что используется для документов.

        Raises:
            ValueError: Если запрос пуст.
            EmbeddingProviderError: Если provider недоступен или ответ неверен.
        """


class VaultEmbeddingIndexRepository(Protocol):
    """Описывает SQLite-независимое хранение embedding-поколения vault."""

    def get_state(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> VaultEmbeddingIndexState | None:
        """Возвращает marker согласованного поколения либо `None`."""

    def list_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultChunkEmbedding]:
        """Возвращает сохранённые embeddings vault в порядке chunk ID."""

    def list_metadata_for_vault(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> list[VaultChunkEmbeddingMetadata]:
        """Возвращает metadata без чтения и декодирования vector BLOB."""

    def invalidate(
        self, *, app_user_id: int, vault_id: int,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """Удаляет marker до внешних запросов и потенциально частичной записи."""

    def save_batch(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        embeddings: tuple[VaultChunkEmbeddingDraft, ...],
        chunk_index_signature: str,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """Сохраняет проверенный пакет без marker короткой транзакцией.

        Проверяет актуальность chunks и вызывает before_write под write lock.
        Несовместимые модель/размерность прежних vectors удаляются атомарно.
        Локальные нарушения контракта пробрасываются, не становясь fallback.
        """

    def complete_generation(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        current_chunk_ids: set[int],
        chunk_index_signature: str,
        document_model: str,
        query_model: str,
        dimension: int | None,
        before_write: Callable[[], None] | None = None,
    ) -> int:
        """Проверяет полное покрытие под write lock и публикует marker.

        Returns:
            Число удалённых embeddings отсутствующих chunks.
        """
