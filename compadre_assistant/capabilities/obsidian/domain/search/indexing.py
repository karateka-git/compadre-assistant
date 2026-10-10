"""Application-сервисы инкрементальной индексации chunks и embeddings."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing

from compadre_assistant.capabilities.obsidian.domain.search.errors import EmbeddingProviderError
from compadre_assistant.capabilities.obsidian.domain.search.models import (
    ChunkIndexUpdate,
    EmbeddingCallContext,
    EmbeddingIndexCoverage,
    EmbeddingIndexUpdate,
    VaultChunkEmbeddingDraft,
)
from compadre_assistant.capabilities.obsidian.domain.search.ports import (
    EmbeddingProvider,
    VaultChunkIndexRepository,
    VaultEmbeddingIndexRepository,
    VaultNoteChunker,
)
from compadre_assistant.capabilities.obsidian.domain.search.entities import VaultNoteChunk
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultNote


class VaultChunkIndexer:
    """Координирует chunker и storage без знания SQLite или GitHub."""

    def __init__(
        self,
        *,
        chunker: VaultNoteChunker,
        repository: VaultChunkIndexRepository,
    ) -> None:
        self._chunker = chunker
        self._repository = repository

    @property
    def index_signature(self) -> str:
        """Возвращает текущую signature parser, engine и policy."""
        return self._chunker.index_signature

    def is_current(self, *, app_user_id: int, vault_id: int) -> bool:
        """Проверяет, соответствует ли полный индекс текущей signature."""
        state = self._repository.get_state(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        return (
            state is not None
            and state.index_signature == self.index_signature
        )

    def invalidate(self, *, app_user_id: int, vault_id: int) -> None:
        """Помечает индекс несогласованным до начала связанных записей."""
        self._repository.invalidate(app_user_id=app_user_id, vault_id=vault_id)

    def index_note(self, note: VaultNote) -> ChunkIndexUpdate:
        """Перестраивает только chunks одной уже сохранённой заметки."""
        if note.id is None:
            raise ValueError("note must be saved before indexing")
        chunks = self._chunker.split(note)
        update = self._repository.replace_for_note(
            app_user_id=note.app_user_id,
            vault_id=note.vault_id,
            note_id=note.id,
            chunks=chunks,
        )
        self._repository.mark_note_current(
            app_user_id=note.app_user_id,
            vault_id=note.vault_id,
            note_id=note.id,
            source_blob_sha=note.blob_sha,
            index_signature=self.index_signature,
        )
        return update

    def reconcile(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        notes: tuple[VaultNote, ...],
    ) -> ChunkIndexUpdate:
        """Обновляет только заметки с новым blob SHA или другой signature.

        Global marker намеренно публикуется лишь после обработки всех dirty
        notes. Его отсутствие поэтому не требует удаления корректных chunks.
        """
        stale_ids = self._repository.list_stale_note_ids(
            app_user_id=app_user_id,
            vault_id=vault_id,
            index_signature=self.index_signature,
        )
        update = ChunkIndexUpdate()
        for note in notes:
            if note.app_user_id != app_user_id or note.vault_id != vault_id:
                raise ValueError("all notes must belong to requested user and vault")
            if note.id in stale_ids:
                update = update.merge(self.index_note(note))
        self.mark_current(app_user_id=app_user_id, vault_id=vault_id)
        return update

    def delete_notes(self, notes: tuple[VaultNote, ...]) -> int:
        """Удаляет chunks заметок перед каскадным удалением source rows."""
        if not notes:
            return 0
        app_user_id = notes[0].app_user_id
        vault_id = notes[0].vault_id
        note_ids: set[int] = set()
        for note in notes:
            if note.id is None:
                raise ValueError("note must be saved before deleting its chunks")
            if note.app_user_id != app_user_id or note.vault_id != vault_id:
                raise ValueError("all notes must belong to one user and vault")
            note_ids.add(note.id)
        return self._repository.delete_for_notes(
            app_user_id=app_user_id,
            vault_id=vault_id,
            note_ids=note_ids,
        )

    def rebuild(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        notes: tuple[VaultNote, ...],
    ) -> ChunkIndexUpdate:
        """Логически переиндексирует vault, сохраняя совместимые chunk IDs."""
        return self.reconcile(
            app_user_id=app_user_id,
            vault_id=vault_id,
            notes=notes,
        )

    def mark_current(self, *, app_user_id: int, vault_id: int) -> None:
        """Фиксирует успешное завершение всех инкрементальных операций."""
        self._repository.mark_current(
            app_user_id=app_user_id,
            vault_id=vault_id,
            index_signature=self.index_signature,
        )


class VaultEmbeddingIndexer:
    """Инкрементально векторизует только новые или изменённые chunks."""

    def __init__(
        self,
        *,
        chunk_repository: VaultChunkIndexRepository,
        embedding_repository: VaultEmbeddingIndexRepository,
        provider: EmbeddingProvider,
    ) -> None:
        self._chunk_repository = chunk_repository
        self._embedding_repository = embedding_repository
        self._provider = provider

    def is_current(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        chunk_index_signature: str,
    ) -> bool:
        """Проверяет signature chunks и обе модели embedding-профиля."""
        state = self._embedding_repository.get_state(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        return (
            state is not None
            and state.chunk_index_signature == chunk_index_signature
            and state.document_model == self._provider.document_model
            and state.query_model == self._provider.query_model
        )

    def invalidate(self, *, app_user_id: int, vault_id: int) -> None:
        """Удаляет marker до изменения исходного chunk-поколения."""
        self._embedding_repository.invalidate(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )

    def get_coverage(
        self,
        *,
        app_user_id: int,
        vault_id: int,
    ) -> EmbeddingIndexCoverage:
        """Возвращает покрытие current chunks переиспользуемыми embeddings.

        Учитываются только vectors с той же document model и content hash.
        Набор с несколькими размерностями нельзя безопасно переиспользовать, поэтому
        его покрытие считается нулевым.
        """
        chunks = self._chunk_repository.list_for_vault(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        current_by_id = {
            chunk.id: chunk
            for chunk in chunks
            if chunk.id is not None
        }
        compatible = [
            embedding
            for embedding in self._embedding_repository.list_metadata_for_vault(
                app_user_id=app_user_id,
                vault_id=vault_id,
            )
            if (
                embedding.document_model == self._provider.document_model
                and (chunk := current_by_id.get(embedding.chunk_id)) is not None
                and embedding.content_hash == chunk.content_hash
            )
        ]
        if len({embedding.dimension for embedding in compatible}) > 1:
            compatible = []
        return EmbeddingIndexCoverage(
            total_chunks=len(current_by_id),
            embedded_chunks=len(compatible),
        )

    def update(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        chunk_index_signature: str,
        before_write: Callable[[], None] | None = None,
    ) -> EmbeddingIndexUpdate:
        """Обновляет embedding-поколение после готовности chunk index.

        Неизменившийся chunk переиспользует сохранённый vector по его SQLite ID,
        `content_hash` и document model. Query model входит только в профиль:
        её смена инвалидирует поколение, но не требует повторной оплаты за те же
        document vectors.

        Args:
            app_user_id: Пользователь, которому принадлежит индекс.
            vault_id: Активный vault пользователя.
            chunk_index_signature: Ожидаемое поколение chunks.
            before_write: Проверка lease без записи в БД; вызывается перед
                запросами и внутри коротких storage-транзакций под write lock.

        Returns:
            Число новых, сохранённых и удалённых vectors и размерность.

        Raises:
            EmbeddingProviderError: Пакет или полное покрытие ответа неверны.
            ValueError: Исходные chunks или профиль изменились во время работы.
            RuntimeError: Проверка lease отклонила продолжение операции.
        """
        chunk_state = self._chunk_repository.get_state(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        if (
            chunk_state is None
            or chunk_state.index_signature != chunk_index_signature
        ):
            raise ValueError("chunk index must be current before embedding")
        chunks = self._chunk_repository.list_for_vault(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        if any(chunk.id is None for chunk in chunks):
            raise ValueError("saved chunks must have IDs before embedding")
        existing = self._embedding_repository.list_metadata_for_vault(
            app_user_id=app_user_id,
            vault_id=vault_id,
        )
        current_chunk_ids = {chunk.id for chunk in chunks if chunk.id is not None}
        current_by_id = {chunk.id: chunk for chunk in chunks}
        existing_by_id = {item.chunk_id: item for item in existing}
        stored_rows_match = all(
            item.document_model == self._provider.document_model
            and item.content_hash == current_by_id[item.chunk_id].content_hash
            for item in existing
            if item.chunk_id in current_by_id
        )
        if self.is_current(
            app_user_id=app_user_id,
            vault_id=vault_id,
            chunk_index_signature=chunk_index_signature,
        ) and set(existing_by_id) == current_chunk_ids and stored_rows_match:
            dimension = existing[0].dimension if existing else None
            return EmbeddingIndexUpdate(
                unchanged=len(chunks),
                dimension=dimension,
            )

        self._embedding_repository.invalidate(
            app_user_id=app_user_id,
            vault_id=vault_id,
            before_write=before_write,
        )
        existing_by_chunk_id = {item.chunk_id: item for item in existing}
        reusable = {
            chunk.id: item
            for chunk in chunks
            if chunk.id is not None
            and (item := existing_by_chunk_id.get(chunk.id)) is not None
            and item.document_model == self._provider.document_model
            and item.content_hash == chunk.content_hash
        }
        reusable_dimensions = {item.dimension for item in reusable.values()}
        if len(reusable_dimensions) > 1:
            reusable = {}
            reusable_dimensions = set()
        pending = [chunk for chunk in chunks if chunk.id not in reusable]
        reused_dimension = next(iter(reusable_dimensions), None)
        embedded, new_dimension = self._embed_chunks(
            pending, chunk_index_signature=chunk_index_signature,
            before_write=before_write,
        )
        if (
            reused_dimension is not None
            and new_dimension is not None
            and reused_dimension != new_dimension
        ):
            # Первый checkpoint новой размерности уже удалил несовместимые
            # строки. Повторно оплачиваем только прежние reusable chunks.
            additional, _ = self._embed_chunks(
                [chunk for chunk in chunks if chunk.id in reusable],
                chunk_index_signature=chunk_index_signature,
                before_write=before_write,
                expected_dimension=new_dimension,
            )
            embedded += additional
            reusable = {}
            reused_dimension = None
        dimension = new_dimension or reused_dimension
        deleted = self._embedding_repository.complete_generation(
            app_user_id=app_user_id,
            vault_id=vault_id,
            current_chunk_ids=current_chunk_ids,
            chunk_index_signature=chunk_index_signature,
            document_model=self._provider.document_model,
            query_model=self._provider.query_model,
            dimension=dimension,
            before_write=before_write,
        )
        return EmbeddingIndexUpdate(
            embedded=embedded,
            deleted=deleted,
            unchanged=len(reusable),
            dimension=dimension,
        )

    def _embed_chunks(
        self,
        chunks: list[VaultNoteChunk],
        *,
        chunk_index_signature: str,
        before_write: Callable[[], None] | None,
        expected_dimension: int | None = None,
    ) -> tuple[int, int | None]:
        """Сохраняет каждый batch до следующего запроса и проверяет покрытие."""
        if not chunks:
            return 0, None
        offset = 0
        batches = self._provider.iter_document_batches(
            tuple(chunk.text for chunk in chunks),
            context=EmbeddingCallContext(
                app_user_id=chunks[0].app_user_id,
                vault_id=chunks[0].vault_id,
            ),
        )
        # Явно закрываем generator при ошибке SQLite/lease: новые платные
        # запросы не выполняются, локальная ошибка не попадает внутрь provider.
        with closing(batches):
            while True:
                if before_write is not None:
                    before_write()
                try:
                    vectors = next(batches)
                except StopIteration:
                    break
                if not vectors or offset + len(vectors) > len(chunks):
                    raise EmbeddingProviderError("Embedding batch count is invalid")
                dimensions = {vector.dimension for vector in vectors}
                if len(dimensions) != 1 or any(
                    vector.model != self._provider.document_model for vector in vectors
                ):
                    raise EmbeddingProviderError("Embedding batch profile is invalid")
                dimension = next(iter(dimensions))
                if expected_dimension is not None and dimension != expected_dimension:
                    raise EmbeddingProviderError("Embedding batch dimension changed")
                expected_dimension = dimension
                batch_chunks = chunks[offset:offset + len(vectors)]
                drafts = tuple(
                    VaultChunkEmbeddingDraft(
                        app_user_id=chunk.app_user_id,
                        vault_id=chunk.vault_id,
                        chunk_id=chunk.id,
                        document_model=vector.model,
                        content_hash=chunk.content_hash,
                        values=vector.values,
                    )
                    for chunk, vector in zip(batch_chunks, vectors, strict=True)
                )
                self._embedding_repository.save_batch(
                    app_user_id=chunks[0].app_user_id,
                    vault_id=chunks[0].vault_id,
                    embeddings=drafts,
                    chunk_index_signature=chunk_index_signature,
                    before_write=before_write,
                )
                offset += len(vectors)
        if offset != len(chunks):
            raise EmbeddingProviderError("Embedding response count does not match chunks")
        return offset, expected_dimension
