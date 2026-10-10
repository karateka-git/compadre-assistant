from __future__ import annotations

from pathlib import Path
import sqlite3

from compadre_assistant.capabilities.obsidian.domain.search.indexing import (
    VaultChunkIndexer,
    VaultEmbeddingIndexer,
)
from compadre_assistant.capabilities.obsidian.domain.search.ports import EmbeddingProvider, VaultNoteChunker
from compadre_assistant.capabilities.obsidian.domain.vaults.ports import GitHubVaultGateway
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import (
    VaultStatus,
    VaultSyncManager,
    VaultSyncResult,
    VaultSyncService,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import (
    SQLiteVaultChunkIndexRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_embedding_index_repository import (
    SQLiteVaultEmbeddingIndexRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import (
    SQLiteVaultInstructionRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import (
    SQLiteVaultSyncLeaseRepository,
)


class SQLiteGitHubVaultSyncManager(VaultSyncManager):
    """Собирает синхронизацию vault на отдельном SQLite-соединении."""

    def __init__(
        self,
        *,
        database_path: Path,
        github_gateway: GitHubVaultGateway,
        note_chunker: VaultNoteChunker,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._database_path = database_path
        self._github_gateway = github_gateway
        self._note_chunker = note_chunker
        self._embedding_provider = embedding_provider

    def _create_service(self, connection: sqlite3.Connection) -> VaultSyncService:
        """Собирает sync service и chunk index на одном SQLite-соединении."""
        chunk_repository = SQLiteVaultChunkIndexRepository(connection)
        chunk_indexer = VaultChunkIndexer(
            chunker=self._note_chunker,
            repository=chunk_repository,
        )
        embedding_indexer = (
            VaultEmbeddingIndexer(
                chunk_repository=chunk_repository,
                embedding_repository=SQLiteVaultEmbeddingIndexRepository(connection),
                provider=self._embedding_provider,
            )
            if self._embedding_provider is not None
            else None
        )
        return VaultSyncService(
            vault_repository=SQLiteObsidianVaultRepository(connection),
            note_repository=SQLiteVaultNoteRepository(connection),
            instruction_repository=SQLiteVaultInstructionRepository(connection),
            lease_repository=SQLiteVaultSyncLeaseRepository(connection),
            github_gateway=self._github_gateway,
            chunk_indexer=chunk_indexer,
            embedding_indexer=embedding_indexer,
        )

    def sync(self, app_user_id: int) -> VaultSyncResult:
        """Синхронизирует vault пользователя через GitHub и SQLite."""
        with connect_database(self._database_path) as connection:
            return self._create_service(connection).sync(app_user_id)

    def sync_if_stale(self, app_user_id: int) -> VaultSyncResult:
        """Проверяет vault только после истечения шестичасового окна."""
        with connect_database(self._database_path) as connection:
            return self._create_service(connection).sync_if_stale(app_user_id)

    def get_status(self, app_user_id: int) -> VaultStatus:
        """Возвращает статус локальной копии выбранного vault."""
        with connect_database(self._database_path) as connection:
            return self._create_service(connection).get_status(app_user_id)
