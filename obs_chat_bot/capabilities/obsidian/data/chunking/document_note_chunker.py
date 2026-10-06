"""Adapter между VaultNote и независимым пакетом document_chunker."""

from __future__ import annotations

from document_chunker import DocumentChunker, DocumentFormat, SourceDocument

from obs_chat_bot.capabilities.obsidian.application.search.models import VaultNoteChunkDraft
from obs_chat_bot.capabilities.obsidian.application.search.ports import VaultNoteChunker
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultNote


class DocumentVaultNoteChunker(VaultNoteChunker):
    """Добавляет project IDs к универсальным chunks сохранённой заметки."""

    def __init__(self, engine: DocumentChunker) -> None:
        self._engine = engine

    @property
    def index_signature(self) -> str:
        """Возвращает signature Markdown parser и фактической policy."""
        return self._engine.signature_for(DocumentFormat.MARKDOWN)

    def split(self, note: VaultNote) -> tuple[VaultNoteChunkDraft, ...]:
        """Преобразует сохранённую заметку в project-scoped chunks.

        Args:
            note: Заметка с назначенным SQLite ID.

        Returns:
            Chunks с `app_user_id`, `vault_id`, `note_id` и путём заметки.

        Raises:
            ValueError: Если заметка ещё не получила ID хранилища.
        """
        if note.id is None:
            raise ValueError("note must be saved before chunking")
        document = SourceDocument(
            text=note.markdown,
            format=DocumentFormat.MARKDOWN,
            source_name=note.path,
            metadata={
                "tags": ", ".join(note.tags),
                "title": note.title or "",
            },
        )
        chunks = self._engine.split(document)
        return tuple(
            VaultNoteChunkDraft(
                app_user_id=note.app_user_id,
                vault_id=note.vault_id,
                note_id=note.id,
                note_path=note.path,
                chunk_key=chunk.chunk_key,
                position=chunk.position,
                heading_path=chunk.heading_path,
                part_index=chunk.part_index,
                text=chunk.text,
                content_hash=chunk.content_hash,
            )
            for chunk in chunks
        )
