"""Тесты project adapter независимого document chunker."""

from __future__ import annotations

import unittest

from document_chunker import (
    DocumentChunker,
    DocumentFormat,
    MarkdownDocumentParser,
)

from obs_chat_bot.capabilities.obsidian.data.chunking.document_note_chunker import (
    DocumentVaultNoteChunker,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultNote


class DocumentVaultNoteChunkerTest(unittest.TestCase):
    """Проверяет mapping универсальных chunks в project-scoped drafts."""

    def test_adds_user_vault_note_ids_after_chunking(self) -> None:
        """Adapter добавляет project IDs, не передавая их parser."""
        adapter = _adapter()
        chunks = adapter.split(
            VaultNote(
                id=42,
                app_user_id=7,
                vault_id=11,
                path="Programming/JWT.md",
                blob_sha="blob-sha",
                markdown="# JWT\nОписание.\n\n## RSA\nПодпись.",
                title="JWT",
                tags=("auth", "security"),
            )
        )

        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(chunk.app_user_id == 7 for chunk in chunks))
        self.assertTrue(all(chunk.vault_id == 11 for chunk in chunks))
        self.assertTrue(all(chunk.note_id == 42 for chunk in chunks))
        self.assertTrue(
            all(chunk.note_path == "Programming/JWT.md" for chunk in chunks)
        )
        self.assertEqual(chunks[1].heading_path, ("JWT", "RSA"))

    def test_rejects_note_without_storage_id(self) -> None:
        """Новая несохранённая заметка не может породить project chunks."""
        with self.assertRaisesRegex(ValueError, "saved before chunking"):
            _adapter().split(
                VaultNote(
                    app_user_id=1,
                    vault_id=2,
                    path="Note.md",
                    blob_sha="blob-sha",
                    markdown="# Note\nText",
                )
            )

    def test_exposes_engine_index_signature(self) -> None:
        """Adapter публикует signature для состояния будущего индекса."""
        adapter = _adapter()

        self.assertEqual(len(adapter.index_signature), 64)
        self.assertEqual(adapter.index_signature, adapter.index_signature)


def _adapter() -> DocumentVaultNoteChunker:
    return DocumentVaultNoteChunker(
        DocumentChunker(
            parsers={DocumentFormat.MARKDOWN: MarkdownDocumentParser()},
        )
    )


if __name__ == "__main__":
    unittest.main()
