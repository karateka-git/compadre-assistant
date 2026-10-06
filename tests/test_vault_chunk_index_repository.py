"""Интеграционные тесты SQLite-хранилища chunks Этапа 10.2."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from document_chunker import (
    ChunkingPolicy,
    DocumentChunker,
    DocumentFormat,
    MarkdownDocumentParser,
)
from obs_chat_bot.capabilities.obsidian.application.search.indexing import VaultChunkIndexer
from obs_chat_bot.capabilities.obsidian.data.chunking.document_note_chunker import DocumentVaultNoteChunker
from obs_chat_bot.data.sqlite.connection import connect_database
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from obs_chat_bot.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import (
    SQLiteVaultChunkIndexRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote
from tests.sqlite_helpers import ensure_app_user


class VaultChunkIndexRepositoryTest(unittest.TestCase):
    """Проверяет generation marker, diff и изоляцию chunk index."""

    def test_full_rebuild_saves_chunks_and_current_signature(self) -> None:
        """Полная индексация сохраняет IDs, структуру и signature policy."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            note = SQLiteVaultNoteRepository(connection).upsert(
                _note(vault.id, markdown="# Python\nТекст заметки про Python.")
            )
            repository = SQLiteVaultChunkIndexRepository(connection)
            indexer = VaultChunkIndexer(
                chunker=_chunker(),
                repository=repository,
            )

            update = indexer.rebuild(
                app_user_id=1,
                vault_id=vault.id,
                notes=(note,),
            )

            chunks = repository.list_for_vault(app_user_id=1, vault_id=vault.id)
            self.assertEqual(update.created, len(chunks))
            self.assertGreater(len(chunks), 0)
            self.assertEqual(chunks[0].note_id, note.id)
            self.assertEqual(chunks[0].heading_path, ("Python",))
            self.assertTrue(indexer.is_current(app_user_id=1, vault_id=vault.id))

    def test_note_replacement_preserves_unchanged_chunk_identity(self) -> None:
        """Инкрементальный diff не пересоздаёт неизменившийся chunk."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            notes = SQLiteVaultNoteRepository(connection)
            saved = notes.upsert(
                _note(
                    vault.id,
                    markdown="# A\nСтабильный раздел.\n## B\nПервый вариант.",
                )
            )
            repository = SQLiteVaultChunkIndexRepository(connection)
            chunker = _chunker(minimum=1, target=40, maximum=80)
            indexer = VaultChunkIndexer(chunker=chunker, repository=repository)
            indexer.rebuild(app_user_id=1, vault_id=vault.id, notes=(saved,))
            before = repository.list_for_note(
                app_user_id=1,
                vault_id=vault.id,
                note_id=saved.id,
            )
            changed = notes.upsert(
                _note(
                    vault.id,
                    markdown="# A\nСтабильный раздел.\n## B\nВторой вариант заметки.",
                )
            )

            update = indexer.index_note(changed)

            after = repository.list_for_note(
                app_user_id=1,
                vault_id=vault.id,
                note_id=saved.id,
            )
            before_ids = {chunk.chunk_key: chunk.id for chunk in before}
            after_ids = {chunk.chunk_key: chunk.id for chunk in after}
            common_keys = set(before_ids) & set(after_ids)
            self.assertTrue(common_keys)
            self.assertTrue(all(before_ids[key] == after_ids[key] for key in common_keys))
            self.assertGreater(update.updated + update.created + update.deleted, 0)

    def test_missing_global_marker_reconciles_only_dirty_note(self) -> None:
        """Recovery использует per-note state и сохраняет чужие chunk IDs."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            notes = SQLiteVaultNoteRepository(connection)
            first = notes.upsert(_note(vault.id, path="first.md"))
            second = notes.upsert(_note(vault.id, path="second.md"))
            repository = SQLiteVaultChunkIndexRepository(connection)
            indexer = VaultChunkIndexer(chunker=_chunker(), repository=repository)
            indexer.rebuild(
                app_user_id=1,
                vault_id=vault.id,
                notes=(first, second),
            )
            second_before = repository.list_for_note(
                app_user_id=1,
                vault_id=vault.id,
                note_id=second.id,
            )
            changed = notes.upsert(
                _note(
                    vault.id,
                    path="first.md",
                    markdown="# Note\nПолностью новый текст первой заметки.",
                    blob_sha="changed-blob",
                )
            )
            indexer.invalidate(app_user_id=1, vault_id=vault.id)

            stale = repository.list_stale_note_ids(
                app_user_id=1,
                vault_id=vault.id,
                index_signature=indexer.index_signature,
            )
            update = indexer.reconcile(
                app_user_id=1,
                vault_id=vault.id,
                notes=(changed, second),
            )
            second_after = repository.list_for_note(
                app_user_id=1,
                vault_id=vault.id,
                note_id=second.id,
            )

            self.assertEqual(stale, {changed.id})
            self.assertGreater(update.updated + update.created + update.deleted, 0)
            self.assertEqual(
                [chunk.id for chunk in second_after],
                [chunk.id for chunk in second_before],
            )
            self.assertTrue(indexer.is_current(app_user_id=1, vault_id=vault.id))

    def test_invalidation_hides_stale_generation(self) -> None:
        """Удаление marker заставляет следующий sync выполнить полный rebuild."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            note = SQLiteVaultNoteRepository(connection).upsert(_note(vault.id))
            repository = SQLiteVaultChunkIndexRepository(connection)
            indexer = VaultChunkIndexer(
                chunker=_chunker(),
                repository=repository,
            )
            indexer.rebuild(app_user_id=1, vault_id=vault.id, notes=(note,))

            indexer.invalidate(app_user_id=1, vault_id=vault.id)

            self.assertFalse(indexer.is_current(app_user_id=1, vault_id=vault.id))
            self.assertIsNone(repository.get_state(app_user_id=1, vault_id=vault.id))
            self.assertGreater(
                len(repository.list_for_vault(app_user_id=1, vault_id=vault.id)),
                0,
            )

    def test_chunks_are_isolated_by_user_and_vault(self) -> None:
        """Один пользователь не видит chunks vault другого пользователя."""
        with self._database() as connection:
            first_vault = _prepare_vault(connection)
            second_vault = _prepare_vault(
                connection,
                app_user_id=2,
                installation_id=202,
                repository_id=502,
            )
            notes = SQLiteVaultNoteRepository(connection)
            first_note = notes.upsert(_note(first_vault.id))
            second_note = notes.upsert(
                _note(
                    second_vault.id,
                    app_user_id=2,
                    path="private.md",
                    markdown="# Private\nСекрет второго пользователя.",
                )
            )
            repository = SQLiteVaultChunkIndexRepository(connection)
            indexer = VaultChunkIndexer(chunker=_chunker(), repository=repository)
            indexer.rebuild(
                app_user_id=1,
                vault_id=first_vault.id,
                notes=(first_note,),
            )
            indexer.rebuild(
                app_user_id=2,
                vault_id=second_vault.id,
                notes=(second_note,),
            )

            first_chunks = repository.list_for_vault(
                app_user_id=1,
                vault_id=first_vault.id,
            )

            self.assertTrue(first_chunks)
            self.assertEqual({chunk.app_user_id for chunk in first_chunks}, {1})
            self.assertNotIn("Секрет", " ".join(chunk.text for chunk in first_chunks))

    @contextmanager
    def _database(self):
        """Открывает новую development-схему для одного теста."""
        with TemporaryDirectory(prefix="obs-chat-bot-chunks-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                yield connection


def _prepare_vault(
    connection,
    *,
    app_user_id: int = 1,
    installation_id: int = 101,
    repository_id: int = 501,
) -> ObsidianVault:
    """Создаёт пользователя, GitHub installation и активный vault."""
    ensure_app_user(connection, app_user_id=app_user_id)
    connection.execute(
        "INSERT INTO github_accounts (app_user_id, github_user_id, login) "
        "VALUES (?, ?, ?)",
        (app_user_id, 700 + app_user_id, f"octocat-{app_user_id}"),
    )
    SQLiteGitHubInstallationRepository(connection).replace_for_user(
        app_user_id=app_user_id,
        installation_ids={installation_id},
    )
    return SQLiteObsidianVaultRepository(connection).replace(
        ObsidianVault(
            app_user_id=app_user_id,
            installation_id=installation_id,
            repository_id=repository_id,
            owner="owner",
            repository=f"notes-{app_user_id}",
            branch="main",
        )
    )


def _note(
    vault_id: int | None,
    *,
    app_user_id: int = 1,
    path: str = "note.md",
    markdown: str = "# Note\nТекст заметки.",
    blob_sha: str = "blob-sha",
) -> VaultNote:
    """Создаёт тестовую Markdown-заметку одного vault."""
    if vault_id is None:
        raise ValueError("vault_id must be saved")
    return VaultNote(
        app_user_id=app_user_id,
        vault_id=vault_id,
        path=path,
        blob_sha=blob_sha,
        markdown=markdown,
    )


def _chunker(
    *,
    minimum: int = 300,
    target: int = 900,
    maximum: int = 1400,
) -> DocumentVaultNoteChunker:
    """Собирает реальный Markdown chunker без полного composition root."""
    return DocumentVaultNoteChunker(
        DocumentChunker(
            parsers={DocumentFormat.MARKDOWN: MarkdownDocumentParser()},
            policy=ChunkingPolicy(
                minimum_size_chars=minimum,
                target_size_chars=target,
                maximum_size_chars=maximum,
            ),
        )
    )


if __name__ == "__main__":
    unittest.main()
