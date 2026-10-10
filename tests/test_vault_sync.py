"""Тесты application-сервиса инкрементальной синхронизации vault."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest

from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import (
    GitHubInstructionFile,
    GitHubMarkdownFile,
    GitHubVaultSnapshot,
    GitHubVaultSnapshotStatus,
)
from compadre_assistant.capabilities.obsidian.domain.search.errors import EmbeddingProviderError
from compadre_assistant.capabilities.obsidian.domain.search.models import (
    ChunkIndexUpdate,
    EmbeddingIndexCoverage,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import (
    VaultSyncService,
    VaultSyncStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import (
    ObsidianVault,
    VaultInstruction,
    VaultNote,
    VaultSyncLease,
)


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


class MemoryVaultRepository:
    """Минимальное хранилище vault для unit-теста."""

    def __init__(self, vault):
        self.vault = vault
        self.updates = []

    def get_for_user(self, _app_user_id):
        return self.vault

    def update_sync_state(self, **values):
        self.updates.append(values)
        self.vault = ObsidianVault(
            app_user_id=self.vault.app_user_id,
            installation_id=self.vault.installation_id,
            repository_id=self.vault.repository_id,
            owner=self.vault.owner,
            repository=self.vault.repository,
            branch=self.vault.branch,
            id=self.vault.id,
            head_commit_sha=values["head_commit_sha"] or self.vault.head_commit_sha,
            tree_sha=values["tree_sha"] or self.vault.tree_sha,
            head_etag=values["head_etag"] or self.vault.head_etag,
            last_checked_at=values["last_checked_at"],
            last_synced_at=values["last_synced_at"] or self.vault.last_synced_at,
        )
        return self.vault


class MemoryNoteRepository:
    """Минимальное хранилище заметок для unit-теста."""

    def __init__(self, notes):
        self.notes = {note.path: note for note in notes}

    def list_for_vault(self, **_values):
        return sorted(self.notes.values(), key=lambda note: note.path)

    def upsert(self, note):
        self.notes[note.path] = note
        return note

    def delete_paths(self, *, paths, **_values):
        deleted = 0
        for path in paths:
            deleted += self.notes.pop(path, None) is not None
        return deleted


class MemoryInstructionRepository:
    """Хранит обязательные правила vault в памяти."""

    def __init__(self, instructions=()):
        self.instructions = list(instructions)

    def list_for_vault(self, **_values):
        return sorted(self.instructions, key=lambda item: item.position)

    def replace_for_vault(self, *, instructions, **_values):
        self.instructions = list(instructions)
        return self.list_for_vault()


class MemoryLeaseRepository:
    """Всегда выдаёт свободный lease и фиксирует освобождение."""

    def __init__(self):
        self.released = False
        self.lease = None

    def acquire(self, **values):
        self.lease = VaultSyncLease(
            app_user_id=values["app_user_id"],
            vault_id=values["vault_id"],
            owner=values["owner"],
            acquired_at=values["now"],
            expires_at=values["expires_at"],
        )
        return self.lease

    def get(self, **_values):
        return self.lease

    def release(self, **_values):
        self.released = True
        self.lease = None
        return True


class FailingNoteRepository(MemoryNoteRepository):
    """Имитирует сбой SQLite при сохранении изменённой заметки."""

    def upsert(self, note):
        raise RuntimeError("storage failed")


class SnapshotGateway:
    """Возвращает заранее подготовленный снимок и запоминает manifest."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.known_blobs = None

    def fetch_vault_snapshot(
        self,
        _vault,
        *,
        known_blobs,
        known_instruction_blobs,
    ):
        self.known_blobs = known_blobs
        self.known_instruction_blobs = known_instruction_blobs
        return self.snapshot


class MemoryChunkIndexer:
    """Имитирует согласованный chunk index для unit-тестов sync orchestration."""

    def __init__(self, *, current=True):
        self.current = current
        self.indexed_paths = []
        self.rebuilds = []
        self.invalidations = 0

    @property
    def index_signature(self):
        return "test-parser:test-policy"

    def is_current(self, **_values):
        return self.current

    def invalidate(self, **_values):
        self.current = False
        self.invalidations += 1

    def index_note(self, note):
        self.indexed_paths.append(note.path)
        return ChunkIndexUpdate(created=1)

    def delete_notes(self, notes):
        return len(notes)

    def rebuild(self, *, notes, **_values):
        self.rebuilds.append(tuple(note.path for note in notes))
        self.current = True
        return ChunkIndexUpdate(created=len(notes))

    def mark_current(self, **_values):
        self.current = True


class FailingEmbeddingIndexer:
    """Имитирует недоступный provider при готовом локальном chunk index."""

    def __init__(self):
        self.update_calls = 0

    def is_current(self, **_values):
        return False

    def get_coverage(self, **_values):
        return EmbeddingIndexCoverage(total_chunks=3)

    def invalidate(self, **_values):
        return None

    def update(self, **_values):
        self.update_calls += 1
        raise EmbeddingProviderError("provider unavailable")


class VaultSyncTest(unittest.TestCase):
    """Проверяет добавление, изменение, удаление и фиксацию source SHA."""

    def test_sync_applies_changed_manifest_and_metadata(self) -> None:
        """Сервис скачанные blobs upsert-ит, а отсутствующие пути удаляет."""
        vault = _vault()
        old = VaultNote(
            app_user_id=1,
            vault_id=10,
            path="old.md",
            blob_sha="old-sha",
            markdown="old",
        )
        notes = MemoryNoteRepository([old])
        gateway = SnapshotGateway(
            GitHubVaultSnapshot(
                status=GitHubVaultSnapshotStatus.CHANGED,
                head_commit_sha="commit-2",
                tree_sha="tree-2",
                head_etag='"etag-2"',
                files=(
                    GitHubMarkdownFile(
                        path="new.md",
                        blob_sha="new-sha",
                        markdown="---\ntags: [one, two]\n---\n# New\n[[Old]]",
                    ),
                ),
                instructions=(
                    GitHubInstructionFile(
                        position=0,
                        path="memory-bank/AGENTS.md.txt",
                        blob_sha="rules-sha",
                        content="Правила",
                    ),
                ),
            )
        )
        vaults = MemoryVaultRepository(vault)
        leases = MemoryLeaseRepository()

        result = VaultSyncService(
            vault_repository=vaults,
            note_repository=notes,
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=leases,
            github_gateway=gateway,
            chunk_indexer=MemoryChunkIndexer(),
            clock=lambda: NOW,
        ).sync(1)

        self.assertEqual(result.status, VaultSyncStatus.SYNCED)
        self.assertEqual((result.added_notes, result.deleted_notes), (1, 1))
        self.assertEqual(gateway.known_blobs, {"old.md": "old-sha"})
        self.assertEqual(notes.notes["new.md"].tags, ("one", "two"))
        self.assertEqual(notes.notes["new.md"].wikilinks, ("Old",))
        self.assertEqual(result.instruction_files, 1)
        self.assertEqual(vaults.vault.tree_sha, "tree-2")
        self.assertEqual(vaults.vault.last_synced_at, NOW)
        self.assertTrue(leases.released)

    def test_not_modified_only_advances_check_time(self) -> None:
        """HTTP 304 не меняет source SHA и не выполняет повторную запись."""
        vaults = MemoryVaultRepository(_vault())
        result = VaultSyncService(
            vault_repository=vaults,
            note_repository=MemoryNoteRepository([]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=SnapshotGateway(
                GitHubVaultSnapshot(
                    status=GitHubVaultSnapshotStatus.NOT_MODIFIED,
                    head_etag='"etag-1"',
                )
            ),
            chunk_indexer=MemoryChunkIndexer(),
            clock=lambda: NOW,
        ).sync(1)

        self.assertEqual(result.status, VaultSyncStatus.UNCHANGED)
        self.assertEqual(vaults.vault.head_commit_sha, "commit-1")
        self.assertIsNone(vaults.vault.last_synced_at)
        self.assertEqual(vaults.vault.last_checked_at, NOW)

    def test_storage_failure_does_not_advance_source_sha(self) -> None:
        """Новый source SHA не фиксируется после неполной локальной записи."""
        vaults = MemoryVaultRepository(_vault())
        leases = MemoryLeaseRepository()
        service = VaultSyncService(
            vault_repository=vaults,
            note_repository=FailingNoteRepository([]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=leases,
            github_gateway=SnapshotGateway(
                GitHubVaultSnapshot(
                    status=GitHubVaultSnapshotStatus.CHANGED,
                    head_commit_sha="commit-2",
                    tree_sha="tree-2",
                    files=(
                        GitHubMarkdownFile(
                            path="new.md",
                            blob_sha="new-sha",
                            markdown="# New",
                        ),
                    ),
                )
            ),
            chunk_indexer=MemoryChunkIndexer(),
            clock=lambda: NOW,
        )

        with self.assertRaisesRegex(RuntimeError, "storage failed"):
            service.sync(1)

        self.assertEqual(vaults.updates, [])
        self.assertEqual(vaults.vault.head_commit_sha, "commit-1")
        self.assertTrue(leases.released)

    def test_embedding_failure_keeps_published_source_and_chunk_snapshot(self) -> None:
        """Optional semantic-сбой не отменяет готовые Markdown и FTS."""
        vaults = MemoryVaultRepository(_vault())
        chunk_indexer = MemoryChunkIndexer()
        result = VaultSyncService(
            vault_repository=vaults,
            note_repository=MemoryNoteRepository([]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=SnapshotGateway(
                GitHubVaultSnapshot(
                    status=GitHubVaultSnapshotStatus.CHANGED,
                    head_commit_sha="commit-2",
                    tree_sha="tree-2",
                    files=(
                        GitHubMarkdownFile(
                            path="new.md",
                            blob_sha="new-sha",
                            markdown="# New",
                        ),
                    ),
                )
            ),
            chunk_indexer=chunk_indexer,
            embedding_indexer=FailingEmbeddingIndexer(),
            clock=lambda: NOW,
        ).sync(1)

        self.assertEqual(result.status, VaultSyncStatus.SYNCED)
        self.assertTrue(result.embedding_update_failed)
        self.assertEqual(vaults.vault.head_commit_sha, "commit-2")
        self.assertEqual(vaults.vault.tree_sha, "tree-2")
        self.assertEqual(vaults.vault.last_synced_at, NOW)
        self.assertTrue(chunk_indexer.current)

    def test_sync_reuses_unchanged_instruction_content_by_blob_sha(self) -> None:
        """Неизменённый instruction blob не требует повторного содержимого."""
        local_instruction = VaultInstruction(
            app_user_id=1,
            vault_id=10,
            position=0,
            path="memory-bank/AGENTS.md.txt",
            blob_sha="rules-sha",
            content="Сохранённые правила",
        )
        instructions = MemoryInstructionRepository((local_instruction,))
        gateway = SnapshotGateway(
            GitHubVaultSnapshot(
                status=GitHubVaultSnapshotStatus.CHANGED,
                head_commit_sha="commit-2",
                tree_sha="tree-2",
                instructions=(
                    GitHubInstructionFile(
                        position=0,
                        path=local_instruction.path,
                        blob_sha=local_instruction.blob_sha,
                    ),
                ),
            )
        )

        result = VaultSyncService(
            vault_repository=MemoryVaultRepository(_vault()),
            note_repository=MemoryNoteRepository([]),
            instruction_repository=instructions,
            lease_repository=MemoryLeaseRepository(),
            github_gateway=gateway,
            chunk_indexer=MemoryChunkIndexer(),
            clock=lambda: NOW,
        ).sync(1)

        self.assertEqual(
            gateway.known_instruction_blobs,
            {"memory-bank/AGENTS.md.txt": "rules-sha"},
        )
        self.assertEqual(
            instructions.instructions[0].content,
            "Сохранённые правила",
        )
        self.assertEqual(result.instruction_files, 1)

    def test_sync_if_stale_skips_github_inside_six_hour_window(self) -> None:
        """Недавно проверенный vault не создаёт ни одного GitHub-запроса."""
        gateway = SnapshotGateway(
            GitHubVaultSnapshot(status=GitHubVaultSnapshotStatus.NOT_MODIFIED)
        )
        service = VaultSyncService(
            vault_repository=MemoryVaultRepository(
                replace(
                    _vault(),
                    last_checked_at=NOW - timedelta(hours=5, minutes=59),
                )
            ),
            note_repository=MemoryNoteRepository([]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=gateway,
            chunk_indexer=MemoryChunkIndexer(),
            clock=lambda: NOW,
        )

        result = service.sync_if_stale(1)

        self.assertEqual(result.status, VaultSyncStatus.FRESH)
        self.assertIsNone(gateway.known_blobs)

    def test_fresh_vault_does_not_retry_full_embedding_index(self) -> None:
        """Статья внутри окна не повторяет сбойную индексацию всего vault."""
        gateway = SnapshotGateway(
            GitHubVaultSnapshot(status=GitHubVaultSnapshotStatus.NOT_MODIFIED)
        )
        embedding_indexer = FailingEmbeddingIndexer()
        service = VaultSyncService(
            vault_repository=MemoryVaultRepository(
                replace(_vault(), last_checked_at=NOW - timedelta(minutes=5))
            ),
            note_repository=MemoryNoteRepository([]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=gateway,
            chunk_indexer=MemoryChunkIndexer(),
            embedding_indexer=embedding_indexer,
            clock=lambda: NOW,
        )

        result = service.sync_if_stale(1)

        self.assertEqual(result.status, VaultSyncStatus.FRESH)
        self.assertTrue(result.embedding_update_failed)
        self.assertEqual(embedding_indexer.update_calls, 0)
        self.assertIsNone(gateway.known_blobs)

    def test_sync_if_stale_checks_github_at_six_hour_boundary(self) -> None:
        """Ровно через шесть часов выполняется обычная условная проверка."""
        gateway = SnapshotGateway(
            GitHubVaultSnapshot(
                status=GitHubVaultSnapshotStatus.NOT_MODIFIED,
                head_etag='"etag-1"',
            )
        )
        service = VaultSyncService(
            vault_repository=MemoryVaultRepository(
                replace(
                    _vault(),
                    last_checked_at=NOW - timedelta(hours=6),
                )
            ),
            note_repository=MemoryNoteRepository([]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=gateway,
            chunk_indexer=MemoryChunkIndexer(),
            clock=lambda: NOW,
        )

        result = service.sync_if_stale(1)

        self.assertEqual(result.status, VaultSyncStatus.UNCHANGED)
        self.assertEqual(gateway.known_blobs, {})

    def test_not_modified_rebuilds_index_after_signature_change(self) -> None:
        """Новая parser/policy signature переиндексирует локальные заметки."""
        note = replace(
            VaultNote(
                app_user_id=1,
                vault_id=10,
                path="note.md",
                blob_sha="blob-sha",
                markdown="# Note",
            ),
            id=11,
        )
        indexer = MemoryChunkIndexer(current=False)
        result = VaultSyncService(
            vault_repository=MemoryVaultRepository(_vault()),
            note_repository=MemoryNoteRepository([note]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=SnapshotGateway(
                GitHubVaultSnapshot(status=GitHubVaultSnapshotStatus.NOT_MODIFIED)
            ),
            chunk_indexer=indexer,
            clock=lambda: NOW,
        ).sync(1)

        self.assertEqual(result.status, VaultSyncStatus.UNCHANGED)
        self.assertTrue(result.index_rebuilt)
        self.assertEqual(indexer.rebuilds, [("note.md",)])

    def test_fresh_vault_rebuilds_stale_index_without_github(self) -> None:
        """Stale chunk generation исправляется внутри окна без GitHub-запроса."""
        gateway = SnapshotGateway(
            GitHubVaultSnapshot(status=GitHubVaultSnapshotStatus.NOT_MODIFIED)
        )
        indexer = MemoryChunkIndexer(current=False)
        service = VaultSyncService(
            vault_repository=MemoryVaultRepository(
                replace(_vault(), last_checked_at=NOW - timedelta(minutes=5))
            ),
            note_repository=MemoryNoteRepository([]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=gateway,
            chunk_indexer=indexer,
            clock=lambda: NOW,
        )

        result = service.sync_if_stale(1)

        self.assertEqual(result.status, VaultSyncStatus.FRESH)
        self.assertTrue(result.index_rebuilt)
        self.assertIsNone(gateway.known_blobs)

    def test_changed_snapshot_indexes_only_changed_note(self) -> None:
        """Текущая signature не запускает полный rebuild неизменённых заметок."""
        unchanged = replace(
            VaultNote(
                app_user_id=1,
                vault_id=10,
                path="stable.md",
                blob_sha="stable-sha",
                markdown="# Stable",
            ),
            id=11,
        )
        indexer = MemoryChunkIndexer()
        result = VaultSyncService(
            vault_repository=MemoryVaultRepository(_vault()),
            note_repository=MemoryNoteRepository([unchanged]),
            instruction_repository=MemoryInstructionRepository(),
            lease_repository=MemoryLeaseRepository(),
            github_gateway=SnapshotGateway(
                GitHubVaultSnapshot(
                    status=GitHubVaultSnapshotStatus.CHANGED,
                    head_commit_sha="commit-2",
                    tree_sha="tree-2",
                    files=(
                        GitHubMarkdownFile(
                            path="stable.md",
                            blob_sha="stable-sha",
                        ),
                        GitHubMarkdownFile(
                            path="changed.md",
                            blob_sha="changed-sha",
                            markdown="# Changed",
                        ),
                    ),
                )
            ),
            chunk_indexer=indexer,
            clock=lambda: NOW,
        ).sync(1)

        self.assertEqual(indexer.indexed_paths, ["changed.md"])
        self.assertEqual(indexer.rebuilds, [])
        self.assertFalse(result.index_rebuilt)
        self.assertEqual(result.created_chunks, 1)


def _vault():
    return ObsidianVault(
        id=10,
        app_user_id=1,
        installation_id=20,
        repository_id=30,
        owner="owner",
        repository="notes",
        branch="main",
        head_commit_sha="commit-1",
        tree_sha="tree-1",
        head_etag='"etag-1"',
    )


if __name__ == "__main__":
    unittest.main()
