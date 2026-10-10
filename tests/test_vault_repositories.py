"""Интеграционные тесты SQLite-хранилищ Этапа 9.1."""
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import (
    SQLiteVaultInstructionRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_confirmation_repository import (
    SQLiteVaultActionConfirmationRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import (
    SQLiteVaultSyncLeaseRepository,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import (
    ObsidianVault,
    VaultActionConfirmation,
    VaultConfirmationAction,
    VaultInstruction,
    VaultNote,
)
from tests.sqlite_helpers import ensure_app_user


class VaultRepositoriesTest(unittest.TestCase):
    """Проверяет хранение installations, vault, заметок и lease."""

    def test_installation_diff_preserves_or_cascades_active_vault(self) -> None:
        """Неизменная installation сохраняет vault, отозванная удаляет его."""
        with TemporaryDirectory(prefix="compadre-assistant-vault-installations-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                installations = SQLiteGitHubInstallationRepository(connection)
                vaults = SQLiteObsidianVaultRepository(connection)
                _ensure_github_account(connection, app_user_id=1)

                installations.replace_for_user(
                    app_user_id=1,
                    installation_ids={101, 102},
                )
                saved_vault = vaults.replace(_vault(installation_id=101))
                installations.replace_for_user(
                    app_user_id=1,
                    installation_ids={101, 103},
                )
                preserved = vaults.get_for_user(1)
                installations.replace_for_user(
                    app_user_id=1,
                    installation_ids={103},
                )

                self.assertEqual(preserved, saved_vault)
                self.assertIsNone(vaults.get_for_user(1))

    def test_replacing_vault_removes_previous_notes(self) -> None:
        """Замена единственного vault каскадно очищает старый Markdown."""
        with TemporaryDirectory(prefix="compadre-assistant-vault-replace-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                installations = SQLiteGitHubInstallationRepository(connection)
                vaults = SQLiteObsidianVaultRepository(connection)
                notes = SQLiteVaultNoteRepository(connection)
                _ensure_github_account(connection, app_user_id=1)
                installations.replace_for_user(
                    app_user_id=1,
                    installation_ids={101},
                )
                first = vaults.replace(_vault(installation_id=101))
                notes.upsert(_note(vault_id=first.id))

                second = vaults.replace(
                    _vault(
                        installation_id=101,
                        repository_id=502,
                        repository="second-notes",
                    )
                )

                self.assertNotEqual(first.id, second.id)
                self.assertEqual(
                    notes.list_for_vault(app_user_id=1, vault_id=first.id),
                    [],
                )

    def test_note_upsert_replaces_markdown_and_ordered_metadata(self) -> None:
        """Upsert сохраняет полный Markdown и заменяет tags и wikilinks."""
        with TemporaryDirectory(prefix="compadre-assistant-vault-note-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                vault = _prepare_vault(connection, app_user_id=1, installation_id=101)
                notes = SQLiteVaultNoteRepository(connection)
                original = notes.upsert(
                    _note(
                        vault_id=vault.id,
                        tags=("python", "github"),
                        wikilinks=("Index", "Projects/Bot"),
                    )
                )
                updated = notes.upsert(
                    _note(
                        vault_id=vault.id,
                        blob_sha="blob-2",
                        markdown="---\ntype: project\n---\n# Updated",
                        frontmatter="type: project",
                        tags=("updated",),
                        wikilinks=("Projects/Updated",),
                    )
                )

                self.assertEqual(updated.id, original.id)
                self.assertEqual(updated.blob_sha, "blob-2")
                self.assertEqual(updated.tags, ("updated",))
                self.assertEqual(updated.wikilinks, ("Projects/Updated",))
                self.assertEqual(updated.frontmatter, "type: project")

    def test_note_queries_and_foreign_keys_enforce_user_isolation(self) -> None:
        """Один пользователь не читает и не изменяет заметки другого."""
        with TemporaryDirectory(prefix="compadre-assistant-vault-isolation-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                connection.execute(
                    "INSERT INTO app_users (id, display_name) VALUES (2, 'Second user')"
                )
                first_vault = _prepare_vault(
                    connection,
                    app_user_id=1,
                    installation_id=101,
                )
                second_vault = _prepare_vault(
                    connection,
                    app_user_id=2,
                    installation_id=202,
                    repository_id=502,
                )
                notes = SQLiteVaultNoteRepository(connection)
                first_note = notes.upsert(_note(vault_id=first_vault.id))
                second_note = notes.upsert(
                    _note(app_user_id=2, vault_id=second_vault.id)
                )

                self.assertIsNone(
                    notes.get_by_path(
                        app_user_id=2,
                        vault_id=first_vault.id,
                        path="Projects/Bot.md",
                    )
                )
                self.assertNotEqual(first_note.id, second_note.id)
                with self.assertRaises(ValueError):
                    notes.upsert(
                        _note(app_user_id=2, vault_id=first_vault.id, blob_sha="evil")
                    )
                unchanged = notes.get_by_path(
                    app_user_id=1,
                    vault_id=first_vault.id,
                    path="Projects/Bot.md",
                )
                self.assertEqual(unchanged, first_note)

    def test_instruction_replace_preserves_order_and_isolates_vault(self) -> None:
        """Полный набор правил заменяется атомарно внутри области пользователя."""
        with TemporaryDirectory(prefix="compadre-assistant-vault-rules-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                vault = _prepare_vault(
                    connection,
                    app_user_id=1,
                    installation_id=101,
                )
                repository = SQLiteVaultInstructionRepository(connection)
                saved = repository.replace_for_vault(
                    app_user_id=1,
                    vault_id=vault.id,
                    instructions=(
                        _instruction(vault_id=vault.id, position=0),
                        _instruction(
                            vault_id=vault.id,
                            position=1,
                            path="memory-bank/docs/workflows.md.txt",
                        ),
                    ),
                )

                self.assertEqual(
                    [instruction.position for instruction in saved],
                    [0, 1],
                )
                replacement = repository.replace_for_vault(
                    app_user_id=1,
                    vault_id=vault.id,
                    instructions=(
                        _instruction(
                            vault_id=vault.id,
                            position=0,
                            path="memory-bank/docs/workflows.md.txt",
                            blob_sha="rules-2",
                        ),
                    ),
                )
                self.assertEqual(len(replacement), 1)
                self.assertEqual(replacement[0].blob_sha, "rules-2")
                with self.assertRaises(ValueError):
                    repository.replace_for_vault(
                        app_user_id=2,
                        vault_id=vault.id,
                        instructions=(),
                    )

    def test_sync_state_update_is_scoped_by_user(self) -> None:
        """Source SHA нельзя обновить через ID другого пользователя."""
        checked_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        with TemporaryDirectory(prefix="compadre-assistant-vault-state-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                vault = _prepare_vault(connection, app_user_id=1, installation_id=101)
                vaults = SQLiteObsidianVaultRepository(connection)

                wrong_scope = vaults.update_sync_state(
                    app_user_id=2,
                    vault_id=vault.id,
                    head_commit_sha="commit-1",
                    tree_sha="tree-1",
                    head_etag='"etag-1"',
                    last_checked_at=checked_at,
                    last_synced_at=checked_at,
                )
                unchanged = vaults.get_for_user(1)
                updated = vaults.update_sync_state(
                    app_user_id=1,
                    vault_id=vault.id,
                    head_commit_sha="commit-1",
                    tree_sha="tree-1",
                    head_etag='"etag-1"',
                    last_checked_at=checked_at,
                    last_synced_at=checked_at,
                )

                self.assertIsNone(wrong_scope)
                self.assertIsNone(unchanged.head_commit_sha)
                self.assertEqual(updated.head_commit_sha, "commit-1")
                self.assertEqual(updated.last_synced_at, checked_at)

    def test_confirmation_persists_replacement_and_disconnect_with_ttl(self) -> None:
        """Ожидающее действие доступно обоим adapters и ограничено TTL."""
        now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        with TemporaryDirectory(prefix="compadre-assistant-vault-confirm-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                _ensure_github_account(connection, app_user_id=1)
                SQLiteGitHubInstallationRepository(connection).replace_for_user(
                    app_user_id=1,
                    installation_ids={101},
                )
                SQLiteObsidianVaultRepository(connection).replace(_vault(installation_id=101))
                confirmations = SQLiteVaultActionConfirmationRepository(connection)
                replacement = _vault(installation_id=101)
                confirmations.save(
                    VaultActionConfirmation(
                        origin=PendingOrigin("telegram", "chat"),
                        base_vault_id=1,
                        app_user_id=1,
                        action=VaultConfirmationAction.REPLACE,
                        replacement=replacement,
                        expires_at=now + timedelta(minutes=10),
                    ),
                    now=now,
                )

                saved_replace = confirmations.find_active(app_user_id=1, now=now)
                expired = confirmations.find_active(
                    app_user_id=1,
                    now=now + timedelta(minutes=10),
                )
                confirmations.save(
                    VaultActionConfirmation(
                        origin=PendingOrigin("telegram", "chat"),
                        base_vault_id=1,
                        app_user_id=1,
                        action=VaultConfirmationAction.DISCONNECT,
                        expires_at=now + timedelta(minutes=20),
                    ),
                    now=now + timedelta(minutes=10),
                )
                saved_disconnect = confirmations.find_active(
                    app_user_id=1, now=now + timedelta(minutes=10)
                )

                self.assertEqual(saved_replace.action, VaultConfirmationAction.REPLACE)
                self.assertEqual(saved_replace.replacement, replacement)
                self.assertIsNone(expired)
                self.assertEqual(
                    saved_disconnect.action,
                    VaultConfirmationAction.DISCONNECT,
                )
                self.assertIsNone(saved_disconnect.replacement)

    def test_lease_blocks_second_connection_until_expiration(self) -> None:
        """SQLite lease координирует два независимых процесса adapters."""
        now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        with TemporaryDirectory(prefix="compadre-assistant-vault-lease-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as first_connection:
                apply_migrations(first_connection)
                vault = _prepare_vault(
                    first_connection,
                    app_user_id=1,
                    installation_id=101,
                )
                first = SQLiteVaultSyncLeaseRepository(first_connection)
                with connect_database(database_path) as second_connection:
                    second = SQLiteVaultSyncLeaseRepository(second_connection)

                    acquired = first.acquire(
                        app_user_id=1,
                        vault_id=vault.id,
                        owner="tg-catcher",
                        now=now,
                        expires_at=now + timedelta(minutes=2),
                    )
                    blocked = second.acquire(
                        app_user_id=1,
                        vault_id=vault.id,
                        owner="vk-catcher",
                        now=now + timedelta(minutes=1),
                        expires_at=now + timedelta(minutes=3),
                    )
                    taken_over = second.acquire(
                        app_user_id=1,
                        vault_id=vault.id,
                        owner="vk-catcher",
                        now=now + timedelta(minutes=2),
                        expires_at=now + timedelta(minutes=4),
                    )

                    self.assertEqual(acquired.owner, "tg-catcher")
                    self.assertIsNone(blocked)
                    self.assertEqual(taken_over.owner, "vk-catcher")
                    self.assertFalse(
                        first.release(
                            app_user_id=1,
                            vault_id=vault.id,
                            owner="tg-catcher",
                        )
                    )
                    self.assertTrue(
                        second.release(
                            app_user_id=1,
                            vault_id=vault.id,
                            owner="vk-catcher",
                        )
                    )


def _prepare_vault(
    connection: sqlite3.Connection,
    *,
    app_user_id: int,
    installation_id: int,
    repository_id: int = 501,
) -> ObsidianVault:
    """Создаёт разрешённую installation и активный vault пользователя."""
    _ensure_github_account(connection, app_user_id=app_user_id)
    SQLiteGitHubInstallationRepository(connection).replace_for_user(
        app_user_id=app_user_id,
        installation_ids={installation_id},
    )
    return SQLiteObsidianVaultRepository(connection).replace(
        _vault(
            app_user_id=app_user_id,
            installation_id=installation_id,
            repository_id=repository_id,
        )
    )


def _ensure_github_account(
    connection: sqlite3.Connection,
    *,
    app_user_id: int,
) -> None:
    """Создаёт родительский GitHub-аккаунт для тестовых installations."""
    ensure_app_user(connection, app_user_id=app_user_id)
    connection.execute(
        "INSERT OR IGNORE INTO github_accounts "
        "(app_user_id, github_user_id, login) VALUES (?, ?, ?)",
        (app_user_id, 700 + app_user_id, f"octocat-{app_user_id}"),
    )


def _vault(
    *,
    app_user_id: int = 1,
    installation_id: int,
    repository_id: int = 501,
    repository: str = "notes",
) -> ObsidianVault:
    """Создаёт доменную модель тестового vault."""
    return ObsidianVault(
        app_user_id=app_user_id,
        installation_id=installation_id,
        repository_id=repository_id,
        owner="octocat",
        repository=repository,
        branch="main",
        root_path="Vault",
    )


def _note(
    *,
    vault_id: int,
    app_user_id: int = 1,
    blob_sha: str = "blob-1",
    markdown: str = "# Bot\n\n[[Index]]",
    frontmatter: str | None = "tags: [python]",
    tags: tuple[str, ...] = ("python",),
    wikilinks: tuple[str, ...] = ("Index",),
) -> VaultNote:
    """Создаёт доменную модель тестовой Markdown-заметки."""
    return VaultNote(
        app_user_id=app_user_id,
        vault_id=vault_id,
        path="Projects/Bot.md",
        blob_sha=blob_sha,
        title="Bot",
        markdown=markdown,
        frontmatter=frontmatter,
        tags=tags,
        wikilinks=wikilinks,
    )


def _instruction(
    *,
    vault_id: int,
    position: int,
    path: str = "memory-bank/AGENTS.md.txt",
    blob_sha: str = "rules-1",
) -> VaultInstruction:
    """Создаёт доменную модель обязательного instruction-файла."""
    return VaultInstruction(
        app_user_id=1,
        vault_id=vault_id,
        position=position,
        path=path,
        blob_sha=blob_sha,
        content="Правила vault",
    )


if __name__ == "__main__":
    unittest.main()
