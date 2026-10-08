"""Регрессии долгого vault lease на двух независимых SQLite-соединениях."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_vault_repository import SQLiteObsidianVaultRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import SQLiteVaultChunkIndexRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_embedding_index_repository import SQLiteVaultEmbeddingIndexRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_instruction_repository import SQLiteVaultInstructionRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import SQLiteVaultSyncLeaseRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_process_lock import vault_process_lock
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultNote
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import (
    GitHubMarkdownFile, GitHubVaultSnapshot, GitHubVaultSnapshotStatus,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.lease_scope import (
    VaultLeaseLostError, VaultLeaseScope, active_vault_lease,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_sync import VaultSyncService
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from sqlite_helpers import ensure_app_user


class VaultLeaseFencingTest(unittest.TestCase):
    """Проверяет продление и запрет stale публикации после takeover."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="vault-lease-fencing-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            ensure_app_user(connection)
            connection.executescript("""
                INSERT INTO github_accounts (app_user_id, github_user_id, login)
                VALUES (1, 10, 'owner');
                INSERT INTO github_installations (app_user_id, installation_id)
                VALUES (1, 20);
                INSERT INTO obsidian_vaults (id, app_user_id, installation_id,
                    repository_id, owner, repository, branch, head_commit_sha, tree_sha)
                VALUES (1, 1, 20, 30, 'owner', 'vault', 'main', 'old-head', 'old-tree');
            """)
        self.now = [datetime(2026, 10, 8, 12, tzinfo=UTC)]

    def _note(self, markdown: str) -> VaultNote:
        """Создаёт заметку для проверяемой транзакции."""
        return VaultNote(1, 1, "note.md", "blob-1", markdown)

    def test_write_renews_lease_before_expiration(self) -> None:
        """Локальная запись продлевает TTL до допуска другого соединения."""
        with connect_database(self.path) as first, connect_database(self.path) as second:
            leases = SQLiteVaultSyncLeaseRepository(first)
            contender = SQLiteVaultSyncLeaseRepository(second)
            leases.acquire(app_user_id=1, vault_id=1, owner="first",
                           now=self.now[0], expires_at=self.now[0] + timedelta(seconds=1))
            with active_vault_lease(VaultLeaseScope(
                1, 1, "first", lambda: self.now[0], timedelta(seconds=1),
            )):
                self.now[0] += timedelta(milliseconds=500)
                SQLiteVaultNoteRepository(first).upsert(self._note("# First"))
            self.assertIsNone(contender.acquire(
                app_user_id=1, vault_id=1, owner="second",
                now=self.now[0] + timedelta(milliseconds=600),
                expires_at=self.now[0] + timedelta(seconds=2),
            ))
            self.assertEqual(leases.get(app_user_id=1, vault_id=1).owner, "first")

    def test_takeover_fences_all_local_publications(self) -> None:
        """После истечения old worker не меняет Markdown, FTS, markers и SHA."""
        with connect_database(self.path) as first, connect_database(self.path) as second:
            old = SQLiteVaultSyncLeaseRepository(first)
            new = SQLiteVaultSyncLeaseRepository(second)
            old.acquire(app_user_id=1, vault_id=1, owner="first",
                        now=self.now[0], expires_at=self.now[0] + timedelta(seconds=1))
            self.now[0] += timedelta(seconds=2)
            self.assertIsNotNone(new.acquire(
                app_user_id=1, vault_id=1, owner="second", now=self.now[0],
                expires_at=self.now[0] + timedelta(minutes=5),
            ))
            with active_vault_lease(VaultLeaseScope(
                1, 1, "first", lambda: self.now[0], timedelta(seconds=1),
            )):
                attempts = (
                    lambda: SQLiteVaultNoteRepository(first).upsert(self._note("# Stale")),
                    lambda: SQLiteVaultInstructionRepository(first).replace_for_vault(
                        app_user_id=1, vault_id=1, instructions=(),
                    ),
                    lambda: SQLiteVaultChunkIndexRepository(first).invalidate(
                        app_user_id=1, vault_id=1,
                    ),
                    lambda: SQLiteVaultChunkIndexRepository(first).replace_for_vault(
                        app_user_id=1, vault_id=1, chunks=(), index_signature="new-index",
                    ),
                    lambda: SQLiteVaultChunkIndexRepository(first).mark_current(
                        app_user_id=1, vault_id=1, index_signature="new-index",
                    ),
                    lambda: SQLiteVaultEmbeddingIndexRepository(first).invalidate(
                        app_user_id=1, vault_id=1,
                    ),
                    lambda: SQLiteVaultEmbeddingIndexRepository(first).complete_generation(
                        app_user_id=1, vault_id=1, current_chunk_ids=set(),
                        chunk_index_signature="new-index", document_model="doc",
                        query_model="query", dimension=None,
                    ),
                    lambda: SQLiteObsidianVaultRepository(first).update_sync_state(
                        app_user_id=1, vault_id=1, head_commit_sha="stale-head",
                        tree_sha="stale-tree", head_etag=None,
                        last_checked_at=self.now[0], last_synced_at=self.now[0],
                    ),
                )
                for attempt in attempts:
                    with self.subTest(attempt=attempt), self.assertRaises(VaultLeaseLostError):
                        attempt()
            self.assertFalse(old.release(app_user_id=1, vault_id=1, owner="first"))
            self.assertEqual(new.get(app_user_id=1, vault_id=1).owner, "second")
            self.assertEqual(SQLiteVaultNoteRepository(second).list_for_vault(
                app_user_id=1, vault_id=1,
            ), [])
            vault = SQLiteObsidianVaultRepository(second).get_for_user(1)
            self.assertEqual((vault.head_commit_sha, vault.tree_sha), ("old-head", "old-tree"))

    def test_delayed_sync_fetch_cannot_publish_after_takeover(self) -> None:
        """Worker, задержанный на GitHub дольше TTL, не публикует snapshot."""
        with connect_database(self.path) as first, connect_database(self.path) as second:
            contender = SQLiteVaultSyncLeaseRepository(second)
            now = self.now

            class DelayedGateway:
                """Имитирует захват lease другим процессом во время fetch."""

                def fetch_vault_snapshot(self, vault, **_kwargs):
                    now[0] += timedelta(seconds=2)
                    self_taken = contender.acquire(
                        app_user_id=1, vault_id=vault.id, owner="second",
                        now=now[0], expires_at=now[0] + timedelta(minutes=5),
                    )
                    assert self_taken is not None
                    return GitHubVaultSnapshot(
                        status=GitHubVaultSnapshotStatus.CHANGED,
                        head_commit_sha="new-head", tree_sha="new-tree",
                        files=(GitHubMarkdownFile("note.md", "new-blob", "# New"),),
                    )

            class CurrentIndex:
                """Сообщает, что локальный FTS marker до fetch был актуален."""

                def is_current(self, **_kwargs):
                    return True

            service = VaultSyncService(
                vault_repository=SQLiteObsidianVaultRepository(first),
                note_repository=SQLiteVaultNoteRepository(first),
                instruction_repository=SQLiteVaultInstructionRepository(first),
                lease_repository=SQLiteVaultSyncLeaseRepository(first),
                github_gateway=DelayedGateway(), chunk_indexer=CurrentIndex(),
                clock=lambda: now[0], lease_duration=timedelta(seconds=1),
            )
            with self.assertRaises(VaultLeaseLostError):
                service.sync(1)
            self.assertEqual(contender.get(app_user_id=1, vault_id=1).owner, "second")
            self.assertEqual(SQLiteVaultNoteRepository(second).list_for_vault(
                app_user_id=1, vault_id=1,
            ), [])
            vault = SQLiteObsidianVaultRepository(second).get_for_user(1)
            self.assertEqual((vault.head_commit_sha, vault.tree_sha), ("old-head", "old-tree"))

    def test_process_crash_releases_vault_lock_without_deleting_file(self) -> None:
        """После аварийного выхода OS освобождает lock для нового процесса."""
        script = """
import os
import sqlite3
import sys
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_process_lock import vault_process_lock
connection = sqlite3.connect(sys.argv[1])
with vault_process_lock(connection, 1):
    os._exit(0)
"""
        subprocess.run([sys.executable, "-c", script, str(self.path)], check=True)
        lock_file = self.path.with_name(f"{self.path.name}.vault-1.lock")
        self.assertTrue(lock_file.exists())
        with connect_database(self.path) as connection:
            with vault_process_lock(connection, 1, blocking=False) as acquired:
                self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
