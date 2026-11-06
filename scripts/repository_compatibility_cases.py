"""Дополнительные проверки прежнего SQLite API на настоящих candidate SQL.

Файл загружается внешним worker после выбора точного previous checkout. Все
импорты приложения и test helpers разрешаются только из этого checkout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations
from compadre_assistant.core.data.sqlite.user_identity_repository import (
    SQLiteAppUserRepository, SQLiteExternalIdentityRepository,
    SQLiteIdentityLinkTokenRepository,
    SQLiteIdentityLifecycleRepository, SQLiteIdentityRebindConfirmationRepository,
)
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository, ArticleAlreadyExistsError
from compadre_assistant.capabilities.obsidian.data.sqlite.article_flow_lease_repository import SQLiteArticleFlowLeaseRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.incoming_message_repository import SQLiteIncomingMessageRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_repository import SQLiteGitHubInstallationRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_writer import SQLiteGitHubAccountAccessWriter
from compadre_assistant.capabilities.obsidian.data.sqlite.github_vault_selection_manager import SQLiteGitHubVaultSelectionManager
from compadre_assistant.capabilities.obsidian.data.sqlite.github_vault_sync_manager import SQLiteGitHubVaultSyncManager
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_vault_repository import SQLiteObsidianVaultRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import SQLiteObsidianProposalRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import SQLiteVaultChunkIndexRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_full_text_search_repository import SQLiteVaultFullTextSearchRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import SQLiteVaultInstructionRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import SQLiteVaultSyncLeaseRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_confirmation_repository import SQLiteVaultActionConfirmationRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_embedding_index_repository import SQLiteVaultEmbeddingIndexRepository
from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
from compadre_assistant.capabilities.obsidian.domain.articles.incoming_messages import ArticleIncomingMessage
from compadre_assistant.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import ObsidianProposal, ObsidianProposalAction, ObsidianProposalStatus
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOperationError, VaultResourceBusyError
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote, VaultConfirmationAction, VaultActionConfirmation, VaultConfirmationStatus
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultInstruction
from compadre_assistant.capabilities.obsidian.domain.search.indexing import VaultChunkIndexer
from compadre_assistant.capabilities.obsidian.domain.search.models import VaultNoteChunkDraft, VaultChunkEmbeddingDraft
from compadre_assistant.capabilities.obsidian.data.chunking.document_note_chunker import DocumentVaultNoteChunker
from document_chunker import ChunkingPolicy, DocumentChunker, DocumentFormat, MarkdownDocumentParser
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import GitHubRepositoryInspection, GitHubVaultCommitResult
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import GitHubVaultSnapshot, GitHubVaultSnapshotStatus
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_selection import VaultDisconnectStatus, VaultSelectionStatus
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import VaultSyncStatus
from compadre_assistant.capabilities.obsidian.domain.vaults.lease_scope import VaultLeaseLostError


class _RepositoryGateway:
    """Возвращает фиксированное описание GitHub repository без внешнего I/O."""

    def inspect_repository(self, *, installation_id: int, owner: str,
                           repository: str, root_path: str) -> GitHubRepositoryInspection:
        """Подтверждает подготовленный repository для selection manager."""
        return GitHubRepositoryInspection(
            installation_id=installation_id, repository_id=501, owner=owner,
            repository=repository, default_branch="main", root_path_is_directory=True,
        )


class _FailingVaultGateway:
    """Фиксирует неожиданный вызов GitHub при проверке отсутствующего vault."""

    def fetch_vault_snapshot(self, *args, **kwargs):
        """Прерывает тест, если отсутствие vault ошибочно вызывает сеть."""
        raise AssertionError("unexpected GitHub request")


class _FailingChunker:
    """Фиксирует неожиданный вызов chunker при пустом vault."""

    @property
    def index_signature(self) -> str:
        """Возвращает допустимую сигнатуру индекса."""
        return "compatibility:empty"

    def chunk(self, *args, **kwargs):
        """Прерывает тест при неожиданной индексации."""
        raise AssertionError("unexpected chunking")


class _NotModifiedGateway:
    """Возвращает неизменённый GitHub vault и считает реальные обращения."""

    def __init__(self) -> None:
        """Сбрасывает счётчик запросов перед сценарием."""
        self.calls = 0

    def fetch_vault_snapshot(self, vault, *, known_blobs, known_instruction_blobs):
        """Подтверждает отсутствие нового дерева без внешнего I/O."""
        self.calls += 1
        return GitHubVaultSnapshot(status=GitHubVaultSnapshotStatus.NOT_MODIFIED,
                                   head_etag='"etag-1"')


class RepositoryCompatibilityCases(unittest.TestCase):
    """Проверяет отсутствующие операции и их сохранённые postconditions."""

    def setUp(self) -> None:
        """Создаёт отдельную временную БД с целыми FK/CHECK и candidate journal."""
        temporary = TemporaryDirectory(prefix="sqlite-compatibility-case-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "case.db"
        self.connection = self.enterContext(connect_database(self.database))
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        apply_migrations(self.connection)
        self.assertEqual(
            [row[0] for row in self.connection.execute("SELECT name FROM schema_migrations ORDER BY version")],
            sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql")),
        )

    def tearDown(self) -> None:
        """Проверяет ограничения и целостность после каждого сценария."""
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def _github(self, app_user_id: int = 1, installation_id: int = 101) -> None:
        """Создаёт минимальный допустимый account и installation старым API."""
        self.connection.execute(
            "INSERT INTO github_accounts (app_user_id, github_user_id, login) VALUES (?, ?, ?)",
            (app_user_id, 700 + app_user_id, f"user-{app_user_id}"),
        )
        self.connection.commit()
        SQLiteGitHubInstallationRepository(self.connection).replace_for_user(
            app_user_id=app_user_id, installation_ids={installation_id},
        )

    def _vault(self, app_user_id: int = 1, installation_id: int = 101) -> ObsidianVault:
        """Возвращает валидную модель vault для старого repository."""
        return ObsidianVault(
            app_user_id=app_user_id, installation_id=installation_id,
            repository_id=501, owner="octocat", repository="notes", branch="main",
        )

    def _proposal(self, *, user_id: int, vault_id: int, suffix: str,
                  action: ObsidianProposalAction = ObsidianProposalAction.SKIP) -> ObsidianProposal:
        """Создаёт статью и analysis FK для отдельного review цикла."""
        article = SQLiteArticleRepository(self.connection).create(Article(
            app_user_id=user_id, source_url=f"https://example.org/{suffix}",
            normalized_url=f"https://example.org/{suffix}", cleaned_text="Source text",
            text_hash=f"hash-{suffix}",
        ))
        analysis = self.connection.execute(
            "INSERT INTO analysis_results (app_user_id, article_id, llm_model, prompt_version, result_text) VALUES (?, ?, 'model', 'v1', 'summary')",
            (user_id, article.id),
        )
        self.connection.commit()
        kwargs = ({"target_path": f"{suffix}.md", "proposed_markdown": "# New"}
                  if action is ObsidianProposalAction.ADD else {})
        return ObsidianProposal(
            origin=PendingOrigin("telegram", "chat"), app_user_id=user_id,
            article_id=article.id, analysis_id=analysis.lastrowid,
            vault_id=vault_id, action=action, reasoning="Review decision",
            base_commit_sha="base-commit", base_tree_sha="base-tree", **kwargs,
        )

    def test_user_create_delete_and_identity_count_cascade(self) -> None:
        """Проверяет три пробела: create/delete профиля и count identities."""
        users = SQLiteAppUserRepository(self.connection)
        identities = SQLiteExternalIdentityRepository(self.connection)
        first = users.create(display_name="Первый")
        second = users.create(display_name="Второй")
        updated = users.update_display_name(app_user_id=first.id, display_name="Переименован")
        self.assertEqual(updated.display_name, "Переименован")
        self.assertEqual(users.get_by_id(first.id).display_name, "Переименован")
        identity = IncomingIdentity(channel="telegram", external_user_id="42", external_chat_id="42")
        saved = identities.create(app_user_id=first.id, identity=identity)
        self.assertEqual(saved.app_user_id, first.id)
        self.assertEqual((identities.count_for_user(first.id), identities.count_for_user(second.id)), (1, 0))
        users.delete(first.id)
        self.assertIsNone(users.get_by_id(first.id))
        self.assertIsNone(identities.find(identity))
        self.assertEqual(identities.count_for_user(first.id), 0)
        self.assertEqual(users.get_by_id(second.id).display_name, "Второй")

    def test_article_text_hash_is_scoped_to_owner(self) -> None:
        """Поиск hash возвращает только статьи владельца и сохраняет текст."""
        users = SQLiteAppUserRepository(self.connection)
        first, second = users.create(), users.create()
        repository = SQLiteArticleRepository(self.connection)
        for user, suffix in ((first, "one"), (second, "two")):
            repository.create(Article(
                app_user_id=user.id, source_url=f"https://example.org/{suffix}",
                normalized_url=f"https://example.org/{suffix}",
                cleaned_text="shared", text_hash="same-hash",
            ))
        found = repository.find_by_text_hash("same-hash", app_user_id=first.id)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].app_user_id, first.id)
        self.assertEqual(found[0].cleaned_text, "shared")
        self.assertEqual(repository.find_by_text_hash("absent", app_user_id=first.id), [])

    def test_unknown_article_status_on_real_unconstrained_column(self) -> None:
        """UNKNOWN сохраняет raw значение там, где настоящая схема это разрешает."""
        user = SQLiteAppUserRepository(self.connection).create()
        self.connection.execute(
            "INSERT INTO articles (app_user_id, source_url, normalized_url, status) VALUES (?, ?, ?, ?)",
            (user.id, "https://example.org/unknown", "https://example.org/unknown", "future_status"),
        )
        self.connection.commit()
        repository = SQLiteArticleRepository(self.connection)
        article = repository.find_by_normalized_url("https://example.org/unknown", user.id)
        self.assertIs(article.status, ArticleStatus.UNKNOWN)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM articles WHERE id = ?", (article.id,)
        ).fetchone()[0], "future_status")
        with self.assertRaises(ValueError):
            repository.update_status(article.id, ArticleStatus.UNKNOWN)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM articles WHERE id = ?", (article.id,)
        ).fetchone()[0], "future_status")

    def test_candidate_check_rejects_unknown_proposal_status(self) -> None:
        """Реальный CHECK отвергает UNKNOWN, синтетические mapper cases отделены."""
        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        repository = SQLiteObsidianProposalRepository(self.connection)
        pending = repository.save_pending(self._proposal(user_id=user.id,
            vault_id=vault.id, suffix="check"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE obsidian_proposals SET status = 'future' WHERE id = ?",
                                    (pending.id,))
        self.connection.rollback()
        self.assertEqual(repository.get_by_id(pending.id, user.id).status,
                         ObsidianProposalStatus.PENDING)

    def test_article_get_update_status_content_and_not_found(self) -> None:
        """Косвенные article updates имеют явные persist и отсутствие чужого ID."""
        user = SQLiteAppUserRepository(self.connection).create()
        repository = SQLiteArticleRepository(self.connection)
        article = repository.create(Article(
            app_user_id=user.id, source_url="https://example.org/item",
            normalized_url="https://example.org/item",
        ))
        with self.assertRaises(ArticleAlreadyExistsError) as duplicate:
            repository.create(Article(
                app_user_id=user.id, source_url="https://example.org/other-source",
                normalized_url="https://example.org/item",
            ))
        self.assertEqual(duplicate.exception.article.id, article.id)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM articles WHERE app_user_id = ?", (user.id,)
        ).fetchone()[0], 1)
        self.assertEqual(repository.get_by_id(article.id).normalized_url, article.normalized_url)
        self.assertIsNone(repository.get_by_id(article.id + 999))
        status = repository.update_status(article.id, ArticleStatus.EXTRACTED)
        self.assertEqual(status.status, ArticleStatus.EXTRACTED)
        self.assertEqual(repository.get_by_id(article.id).status, ArticleStatus.EXTRACTED)
        updated = repository.update_content(article.id, title="Saved title",
                                            cleaned_text="Saved text", text_hash="saved-hash")
        self.assertEqual((updated.title, updated.cleaned_text, updated.text_hash),
                         ("Saved title", "Saved text", "saved-hash"))
        self.assertEqual(repository.find_by_text_hash("saved-hash", user.id)[0].id, article.id)
        self.assertIsNone(repository.update_status(article.id + 999, ArticleStatus.EXTRACTED))
        self.assertIsNone(repository.update_content(article.id + 999, title="Missing",
                                                    cleaned_text="Missing", text_hash="missing-hash"))
        self.assertEqual(repository.get_by_id(article.id).title, "Saved title")

    def test_installation_contains_and_delete_cascade(self) -> None:
        """Чужой installation недоступен; удаление каскадно удаляет vault."""
        users = SQLiteAppUserRepository(self.connection)
        first, second = users.create(), users.create()
        self._github(first.id)
        self._github(second.id, 202)
        installations = SQLiteGitHubInstallationRepository(self.connection)
        vaults = SQLiteObsidianVaultRepository(self.connection)
        vault = vaults.replace(self._vault(first.id))
        self.assertTrue(installations.contains(app_user_id=first.id, installation_id=101))
        self.assertFalse(installations.contains(app_user_id=second.id, installation_id=101))
        self.assertFalse(installations.contains(app_user_id=first.id, installation_id=202))
        installations.delete_for_user(first.id)
        self.assertEqual(installations.list_for_user(first.id), [])
        self.assertIsNone(vaults.get_by_id(app_user_id=first.id, vault_id=vault.id))
        self.assertTrue(installations.contains(app_user_id=second.id, installation_id=202))

    def test_account_writer_rolls_back_account_and_installations(self) -> None:
        """Fault trigger после account write откатывает всю транзакцию writer."""
        user = SQLiteAppUserRepository(self.connection).create()
        writer = SQLiteGitHubAccountAccessWriter(self.database)
        writer.replace_for_user(app_user_id=user.id, github_user_id=701,
                                login="original", installation_ids={101})
        self.connection.execute("""
            CREATE TRIGGER compatibility_fail_installation BEFORE INSERT ON github_installations
            WHEN NEW.installation_id = 202 BEGIN SELECT RAISE(ABORT, 'fault'); END
        """)
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            writer.replace_for_user(app_user_id=user.id, github_user_id=702,
                                    login="changed", installation_ids={202})
        self.assertEqual(tuple(self.connection.execute(
            "SELECT github_user_id, login FROM github_accounts WHERE app_user_id = ?",
            (user.id,)).fetchone()), (701, "original"))
        self.assertEqual([row[0] for row in self.connection.execute(
            "SELECT installation_id FROM github_installations WHERE app_user_id = ?",
            (user.id,))], [101])

    def test_vault_delete_for_user_cascades_notes_and_preserves_other_owner(self) -> None:
        """Прямой delete vault удаляет дочерние строки только своего owner."""
        users = SQLiteAppUserRepository(self.connection)
        first, second = users.create(), users.create()
        self._github(first.id)
        self._github(second.id, 202)
        vaults = SQLiteObsidianVaultRepository(self.connection)
        first_vault = vaults.replace(self._vault(first.id))
        second_vault = vaults.replace(self._vault(second.id, 202))
        notes = SQLiteVaultNoteRepository(self.connection)
        first_note = notes.upsert(VaultNote(app_user_id=first.id, vault_id=first_vault.id,
                                            path="one.md", blob_sha="sha-1", markdown="# One"))
        second_note = notes.upsert(VaultNote(app_user_id=second.id, vault_id=second_vault.id,
                                             path="two.md", blob_sha="sha-2", markdown="# Two"))
        vaults.delete_for_user(first.id)
        self.assertIsNone(vaults.get_for_user(first.id))
        self.assertIsNone(notes.get_by_path(app_user_id=first.id, vault_id=first_vault.id, path="one.md"))
        self.assertEqual(notes.get_by_path(app_user_id=second.id, vault_id=second_vault.id,
                                           path="two.md").id, second_note.id)
        self.assertIsNotNone(first_note.id)

    def test_vault_create_if_absent_and_get_by_id_scope(self) -> None:
        """Первый vault сохраняется, повтор не заменяет его и чужой ID скрыт."""
        users = SQLiteAppUserRepository(self.connection)
        first, second = users.create(), users.create()
        self._github(first.id)
        self._github(second.id, 202)
        vaults = SQLiteObsidianVaultRepository(self.connection)
        created = vaults.create_if_absent(self._vault(first.id))
        self.assertEqual(vaults.get_by_id(app_user_id=first.id, vault_id=created.id), created)
        self.assertIsNone(vaults.get_by_id(app_user_id=second.id, vault_id=created.id))
        self.assertIsNone(vaults.create_if_absent(ObsidianVault(
            app_user_id=first.id, installation_id=101, repository_id=502,
            owner="octocat", repository="replacement", branch="main",
        )))
        self.assertEqual(vaults.get_for_user(first.id).repository, "notes")
        self.assertIsNone(vaults.get_for_user(second.id))

    def test_instruction_list_note_delete_and_fts_no_stale_result(self) -> None:
        """Косвенные list/delete/search проверяют порядок, owner и FTS triggers."""
        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        instructions = SQLiteVaultInstructionRepository(self.connection)
        saved = instructions.replace_for_vault(app_user_id=user.id, vault_id=vault.id,
            instructions=(VaultInstruction(app_user_id=user.id, vault_id=vault.id,
                                           position=0, path="AGENTS.md.txt", blob_sha="blob-a",
                                           content="First"),
                          VaultInstruction(app_user_id=user.id, vault_id=vault.id,
                                           position=1, path="RULES.md.txt", blob_sha="blob-b",
                                           content="Second")))
        self.assertEqual([item.content for item in instructions.list_for_vault(
            app_user_id=user.id, vault_id=vault.id)], ["First", "Second"])
        self.assertEqual([item.id for item in saved], [item.id for item in instructions.list_for_vault(
            app_user_id=user.id, vault_id=vault.id)])
        self.assertEqual(instructions.list_for_vault(app_user_id=user.id + 999,
                                                    vault_id=vault.id), [])
        notes = SQLiteVaultNoteRepository(self.connection)
        note = notes.upsert(VaultNote(app_user_id=user.id, vault_id=vault.id,
            path="search.md", blob_sha="blob-1", markdown="# Python\nOldword content."))
        chunker = DocumentVaultNoteChunker(DocumentChunker(
            parsers={DocumentFormat.MARKDOWN: MarkdownDocumentParser()},
            policy=ChunkingPolicy(minimum_size_chars=1, target_size_chars=80,
                                  maximum_size_chars=160),
        ))
        indexer = VaultChunkIndexer(chunker=chunker,
                                    repository=SQLiteVaultChunkIndexRepository(self.connection))
        indexer.rebuild(app_user_id=user.id, vault_id=vault.id, notes=(note,))
        search = SQLiteVaultFullTextSearchRepository(self.connection)
        args = dict(app_user_id=user.id, vault_id=vault.id,
                    expected_index_signature=chunker.index_signature, limit=10)
        self.assertEqual(search.search(query="oldword", **args)[0].chunk.note_path, "search.md")
        self.assertEqual(search.search(query="oldword", app_user_id=user.id + 999,
                                       vault_id=vault.id, expected_index_signature=chunker.index_signature,
                                       limit=10), ())
        changed = notes.upsert(VaultNote(app_user_id=user.id, vault_id=vault.id,
            path="search.md", blob_sha="blob-2", markdown="# Python\nNewword content."))
        indexer.invalidate(app_user_id=user.id, vault_id=vault.id)
        indexer.index_note(changed)
        indexer.mark_current(app_user_id=user.id, vault_id=vault.id)
        self.assertEqual(search.search(query="oldword", **args), ())
        self.assertEqual(search.search(query="newword", **args)[0].chunk.note_path, "search.md")
        self.assertEqual(notes.delete_paths(app_user_id=user.id, vault_id=vault.id,
                                            paths={"search.md"}), 1)
        self.assertEqual(search.search(query="newword", **args), ())
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM obsidian_note_chunks_fts").fetchone()[0], 0)

    def test_proposal_pending_claim_release_cas_and_terminal_reads(self) -> None:
        """Косвенные proposal paths сохраняют owner, версии и терминальные статусы."""
        from datetime import timedelta

        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        repository = SQLiteObsidianProposalRepository(self.connection)
        origin = PendingOrigin("telegram", "chat")
        now = datetime.now(UTC)
        cancelled = repository.save_pending(self._proposal(user_id=user.id,
            vault_id=vault.id, suffix="cancelled"))
        self.assertEqual([item.id for item in repository.list_pending(user.id, origin)], [cancelled.id])
        self.assertEqual(repository.get_by_id(cancelled.id, user.id).id, cancelled.id)
        self.assertIsNone(repository.get_by_id(cancelled.id, user.id + 999))
        self.assertEqual(repository.get_active_for_article(app_user_id=user.id,
                         article_id=cancelled.article_id).id, cancelled.id)
        self.assertIsNone(repository.mark_cancelled(proposal_id=cancelled.id,
            app_user_id=user.id, origin=origin, version=cancelled.version + 1))
        self.assertEqual(repository.get_by_id(cancelled.id, user.id).status,
                         ObsidianProposalStatus.PENDING)
        stopped = repository.mark_cancelled(proposal_id=cancelled.id,
            app_user_id=user.id, origin=origin, version=cancelled.version)
        self.assertEqual(stopped.status, ObsidianProposalStatus.CANCELLED)
        self.assertEqual(repository.list_pending(user.id, origin), ())
        self.assertIsNone(repository.get_active_for_article(app_user_id=user.id,
                          article_id=cancelled.article_id))
        conflicted = repository.save_pending(self._proposal(user_id=user.id,
            vault_id=vault.id, suffix="conflicted"))
        self.assertIsNone(repository.claim(proposal_id=conflicted.id, app_user_id=user.id,
            origin=origin, version=conflicted.version + 1, now=now,
            expires_at=now + timedelta(minutes=5)))
        claimed = repository.claim(proposal_id=conflicted.id, app_user_id=user.id,
            origin=origin, version=conflicted.version, now=now,
            expires_at=now + timedelta(minutes=5))
        self.assertEqual((claimed.status, claimed.version),
                         (ObsidianProposalStatus.EXECUTING, conflicted.version + 1))
        self.assertIsNone(repository.release_claim(conflicted))
        released = repository.release_claim(claimed)
        self.assertEqual((released.status, released.version),
                         (ObsidianProposalStatus.PENDING, claimed.version + 1))
        reclaimed = repository.claim(proposal_id=conflicted.id, app_user_id=user.id,
            origin=origin, version=released.version, now=now,
            expires_at=now + timedelta(minutes=5))
        self.assertIsNone(repository.mark_conflict(proposal_id=reclaimed.id,
            app_user_id=user.id, version=reclaimed.version + 1))
        conflict = repository.mark_conflict(proposal_id=reclaimed.id,
            app_user_id=user.id, version=reclaimed.version)
        self.assertEqual(conflict.status, ObsidianProposalStatus.CONFLICT)
        self.assertIsNone(repository.get_latest_applied_for_article(
            app_user_id=user.id, article_id=conflicted.article_id))

    def test_proposal_complete_applied_rolls_back_intermediate_writes(self) -> None:
        """Fault trigger на последней записи откатывает note, vault и article."""
        from datetime import timedelta

        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        repository = SQLiteObsidianProposalRepository(self.connection)
        pending = repository.save_pending(self._proposal(user_id=user.id, vault_id=vault.id,
            suffix="applied", action=ObsidianProposalAction.ADD))
        now = datetime.now(UTC)
        claimed = repository.claim(proposal_id=pending.id, app_user_id=user.id,
            origin=pending.origin, version=pending.version, now=now,
            expires_at=now + timedelta(minutes=5))
        note = VaultNote(app_user_id=user.id, vault_id=vault.id, path="applied.md",
                         blob_sha="applied-blob", markdown="# New")
        self.connection.execute("""
            CREATE TRIGGER compatibility_fail_proposal BEFORE UPDATE ON obsidian_proposals
            WHEN NEW.status = 'applied' BEGIN SELECT RAISE(ABORT, 'fault'); END
        """)
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            repository.complete_applied(proposal_id=claimed.id, app_user_id=user.id,
                version=claimed.version, applied_commit_sha="commit-new",
                head_commit_sha="commit-new", tree_sha="tree-new", note=note)
        self.assertEqual(repository.get_by_id(claimed.id, user.id).status,
                         ObsidianProposalStatus.EXECUTING)
        self.assertIsNone(SQLiteVaultNoteRepository(self.connection).get_by_path(
            app_user_id=user.id, vault_id=vault.id, path="applied.md"))
        self.assertIsNone(SQLiteObsidianVaultRepository(self.connection).get_for_user(
            user.id).head_commit_sha)
        self.assertEqual(SQLiteArticleRepository(self.connection).get_by_id(
            pending.article_id).cleaned_text, "Source text")
        self.connection.execute("DROP TRIGGER compatibility_fail_proposal")
        self.connection.commit()
        applied = repository.complete_applied(proposal_id=claimed.id, app_user_id=user.id,
            version=claimed.version, applied_commit_sha="commit-new",
            head_commit_sha="commit-new", tree_sha="tree-new", note=note)
        self.assertEqual(applied.status, ObsidianProposalStatus.APPLIED)
        self.assertEqual(repository.get_latest_applied_for_article(
            app_user_id=user.id, article_id=pending.article_id).id, applied.id)
        self.assertEqual(SQLiteVaultNoteRepository(self.connection).get_by_path(
            app_user_id=user.id, vault_id=vault.id, path="applied.md").markdown, "# New")

    def test_vault_confirmation_direct_save_read_decide_cas(self) -> None:
        """Прямой repository сохраняет pending, отклоняет дубль и старую версию."""
        from datetime import timedelta

        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        repository = SQLiteVaultActionConfirmationRepository(self.connection)
        now = datetime.now(UTC)
        draft = VaultActionConfirmation(
            origin=PendingOrigin("telegram", "chat"), base_vault_id=vault.id,
            app_user_id=user.id, action=VaultConfirmationAction.DISCONNECT,
            expires_at=now + timedelta(minutes=10),
        )
        pending = repository.save(draft, now=now)
        self.assertEqual(repository.get_by_id(pending.id, user.id).action,
                         VaultConfirmationAction.DISCONNECT)
        self.assertIsNone(repository.get_by_id(pending.id, user.id + 999))
        self.assertEqual(repository.find_active(app_user_id=user.id, now=now).id, pending.id)
        with self.assertRaises(VaultResourceBusyError):
            repository.save(draft, now=now)
        self.assertEqual(repository.get_by_id(pending.id, user.id).version, 1)
        cancelled = repository.decide(pending, approve=False, now=now)
        self.assertEqual(cancelled.id, vault.id)
        self.assertEqual(repository.get_by_id(pending.id, user.id).status,
                         VaultConfirmationStatus.CANCELLED)
        self.assertIsNone(repository.find_active(app_user_id=user.id, now=now))
        with self.assertRaises(PendingOperationError):
            repository.decide(pending, approve=True, now=now)
        self.assertEqual(SQLiteObsidianVaultRepository(self.connection).get_for_user(user.id).id,
                         vault.id)
        pending = repository.save(draft, now=now)
        self.assertEqual(repository.decide(pending, approve=True, now=now).id, vault.id)
        self.assertIsNone(SQLiteObsidianVaultRepository(self.connection).get_for_user(user.id))

    def test_selection_manager_pending_disconnect_and_decide(self) -> None:
        """Три пробела менеджера подтверждаются persisted CAS и каскадом."""
        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        manager = SQLiteGitHubVaultSelectionManager(
            database_path=self.database, github_gateway=_RepositoryGateway(),
        )
        origin = PendingOrigin("telegram", "chat")
        selected = manager.select(app_user_id=user.id, origin=origin,
                                  repository_url="https://github.com/octocat/notes")
        self.assertEqual(selected.status, VaultSelectionStatus.SELECTED)
        self.assertIsNone(manager.get_pending(user.id, origin))
        request = manager.request_disconnect(user.id, origin)
        self.assertEqual(request.status, VaultDisconnectStatus.CONFIRMATION_REQUIRED)
        pending = manager.get_pending(user.id, origin)
        self.assertEqual((pending.app_user_id, pending.action), (user.id, VaultConfirmationAction.DISCONNECT))
        with self.assertRaises(PendingOperationError):
            manager.decide(user.id, confirmation_id=pending.id,
                           origin=origin, version=pending.version + 1,
                           action=VaultConfirmationAction.DISCONNECT, approve=True)
        self.assertEqual(manager.get_selected(user.id).id, selected.vault.id)
        completed = manager.decide(user.id, confirmation_id=pending.id,
                                   origin=origin, version=pending.version,
                                   action=VaultConfirmationAction.DISCONNECT, approve=True)
        self.assertEqual(completed.status, VaultDisconnectStatus.DISCONNECTED)
        self.assertIsNone(manager.get_selected(user.id))
        self.assertIsNone(manager.get_pending(user.id, origin))

    def test_sync_manager_no_vault_and_status(self) -> None:
        """Три публичные операции менеджера сохраняют пустое состояние."""
        user = SQLiteAppUserRepository(self.connection).create()
        manager = SQLiteGitHubVaultSyncManager(
            database_path=self.database, github_gateway=_FailingVaultGateway(),
            note_chunker=_FailingChunker(),
        )
        self.assertEqual(manager.sync(user.id).status, VaultSyncStatus.NO_VAULT)
        self.assertEqual(manager.sync_if_stale(user.id).status, VaultSyncStatus.NO_VAULT)
        status = manager.get_status(user.id)
        self.assertIsNone(status.vault)
        self.assertEqual((status.note_count, status.instruction_count), (0, 0))

    def test_sync_manager_persists_check_and_skips_fresh_vault(self) -> None:
        """Менеджер с vault пишет check time и повторно читает новый статус."""
        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        gateway = _NotModifiedGateway()
        manager = SQLiteGitHubVaultSyncManager(database_path=self.database,
                                               github_gateway=gateway,
                                               note_chunker=_FailingChunker())
        first = manager.sync(user.id)
        self.assertEqual(first.status, VaultSyncStatus.UNCHANGED)
        self.assertEqual(gateway.calls, 1)
        with connect_database(self.database) as connection:
            updated = SQLiteObsidianVaultRepository(connection).get_for_user(user.id)
            self.assertEqual(updated.id, vault.id)
            self.assertIsNotNone(updated.last_checked_at)
            self.assertEqual(updated.head_etag, '"etag-1"')
        second = manager.sync_if_stale(user.id)
        self.assertEqual(second.status, VaultSyncStatus.FRESH)
        self.assertEqual(gateway.calls, 1)
        status = manager.get_status(user.id)
        self.assertEqual(status.vault.id, vault.id)
        self.assertEqual(status.note_count, 0)

    def test_chunk_delete_for_notes_removes_only_selected_note(self) -> None:
        """Прямое удаление chunks не затрагивает соседнюю заметку и owner."""
        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        notes = SQLiteVaultNoteRepository(self.connection)
        first = notes.upsert(VaultNote(app_user_id=user.id, vault_id=vault.id,
                                       path="one.md", blob_sha="sha-1", markdown="One"))
        second = notes.upsert(VaultNote(app_user_id=user.id, vault_id=vault.id,
                                        path="two.md", blob_sha="sha-2", markdown="Two"))
        self.connection.executemany(
            "INSERT INTO obsidian_note_chunks (app_user_id, vault_id, note_id, note_path, chunk_key, position, heading_path, part_index, text, content_hash) VALUES (?, ?, ?, ?, ?, 0, '', 0, ?, ?)",
            [(user.id, vault.id, first.id, "one.md", "one", "One", "hash-1"),
             (user.id, vault.id, second.id, "two.md", "two", "Two", "hash-2")],
        )
        self.connection.commit()
        chunks = SQLiteVaultChunkIndexRepository(self.connection)
        self.assertEqual(chunks.delete_for_notes(app_user_id=user.id, vault_id=vault.id,
                                                note_ids={first.id}), 1)
        self.assertEqual([row[0] for row in self.connection.execute(
            "SELECT note_id FROM obsidian_note_chunks")], [second.id])
        self.assertEqual(chunks.delete_for_notes(app_user_id=user.id, vault_id=vault.id,
                                                note_ids={first.id}), 0)

    def test_chunk_index_replace_mark_invalidate_and_rollback(self) -> None:
        """Косвенные chunk writes сохраняют поколение и откатывают неверный набор."""
        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        note = SQLiteVaultNoteRepository(self.connection).upsert(VaultNote(
            app_user_id=user.id, vault_id=vault.id, path="chunk.md",
            blob_sha="blob-1", markdown="# Chunk",
        ))
        chunks = SQLiteVaultChunkIndexRepository(self.connection)

        def draft(key: str, text: str) -> VaultNoteChunkDraft:
            """Создаёт chunk текущей заметки для сравнения diff."""
            return VaultNoteChunkDraft(
                app_user_id=user.id, vault_id=vault.id, note_id=note.id,
                note_path="chunk.md", chunk_key=key, position=0,
                heading_path=(), part_index=0, text=text,
                content_hash=f"hash-{text}",
            )

        original = draft("same", "Old")
        self.assertEqual(chunks.replace_for_note(app_user_id=user.id, vault_id=vault.id,
                                                note_id=note.id, chunks=(original,)).created, 1)
        original_id = chunks.list_for_note(app_user_id=user.id, vault_id=vault.id,
                                           note_id=note.id)[0].id
        chunks.mark_note_current(app_user_id=user.id, vault_id=vault.id, note_id=note.id,
                                 source_blob_sha="blob-1", index_signature="signature-1")
        self.assertEqual(chunks.list_stale_note_ids(app_user_id=user.id, vault_id=vault.id,
                                                     index_signature="signature-1"), set())
        self.assertEqual(chunks.mark_current(app_user_id=user.id, vault_id=vault.id,
                                             index_signature="signature-1").index_signature,
                         "signature-1")
        chunks.invalidate(app_user_id=user.id, vault_id=vault.id)
        self.assertIsNone(chunks.get_state(app_user_id=user.id, vault_id=vault.id))
        self.assertEqual(chunks.list_for_note(app_user_id=user.id, vault_id=vault.id,
                                              note_id=note.id)[0].id, original_id)
        updated = draft("same", "New")
        self.assertEqual(chunks.replace_for_note(app_user_id=user.id, vault_id=vault.id,
                                                note_id=note.id, chunks=(updated,)).updated, 1)
        self.assertEqual(chunks.list_for_note(app_user_id=user.id, vault_id=vault.id,
                                              note_id=note.id)[0].text, "New")
        invalid = VaultNoteChunkDraft(app_user_id=user.id, vault_id=vault.id,
                                      note_id=note.id + 999, note_path="chunk.md",
                                      chunk_key="wrong", position=0, heading_path=(),
                                      part_index=0, text="Wrong", content_hash="wrong")
        with self.assertRaises(ValueError):
            chunks.replace_for_vault(app_user_id=user.id, vault_id=vault.id,
                                     chunks=(invalid,), index_signature="signature-2")
        self.assertEqual(chunks.list_for_note(app_user_id=user.id, vault_id=vault.id,
                                              note_id=note.id)[0].text, "New")
        result = chunks.replace_for_vault(app_user_id=user.id, vault_id=vault.id,
                                          chunks=(updated,), index_signature="signature-2")
        self.assertEqual((result.created, result.deleted), (1, 1))
        self.assertEqual(chunks.get_state(app_user_id=user.id, vault_id=vault.id).index_signature,
                         "signature-2")
        self.assertEqual(chunks.list_stale_note_ids(app_user_id=user.id, vault_id=vault.id,
                                                     index_signature="signature-2"), set())

    def test_embedding_metadata_invalidate_and_generation_postconditions(self) -> None:
        """Косвенные metadata/invalidate оставляют vectors, но снимают marker."""
        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        note = SQLiteVaultNoteRepository(self.connection).upsert(VaultNote(
            app_user_id=user.id, vault_id=vault.id, path="embed.md",
            blob_sha="blob-1", markdown="Embedding source",
        ))
        chunks = SQLiteVaultChunkIndexRepository(self.connection)
        chunks.replace_for_vault(app_user_id=user.id, vault_id=vault.id,
            chunks=(VaultNoteChunkDraft(app_user_id=user.id, vault_id=vault.id,
                note_id=note.id, note_path="embed.md", chunk_key="key", position=0,
                heading_path=(), part_index=0, text="Embedding source",
                content_hash="content-hash"),), index_signature="signature")
        chunk = chunks.list_for_note(app_user_id=user.id, vault_id=vault.id,
                                     note_id=note.id)[0]
        embeddings = SQLiteVaultEmbeddingIndexRepository(self.connection)
        embeddings.save_batch(app_user_id=user.id, vault_id=vault.id,
            embeddings=(VaultChunkEmbeddingDraft(app_user_id=user.id, vault_id=vault.id,
                chunk_id=chunk.id, document_model="doc-model", content_hash="content-hash",
                values=(0.5, 1.0)),), chunk_index_signature="signature")
        metadata = embeddings.list_metadata_for_vault(app_user_id=user.id,
                                                       vault_id=vault.id)
        self.assertEqual([(item.chunk_id, item.document_model, item.dimension,
                           item.content_hash) for item in metadata],
                         [(chunk.id, "doc-model", 2, "content-hash")])
        self.assertEqual(embeddings.complete_generation(app_user_id=user.id,
            vault_id=vault.id, current_chunk_ids={chunk.id},
            chunk_index_signature="signature", document_model="doc-model",
            query_model="query-model", dimension=2), 0)
        self.assertEqual(embeddings.get_state(app_user_id=user.id,
                         vault_id=vault.id).dimension, 2)
        self.assertEqual(embeddings.list_for_vault(app_user_id=user.id,
                         vault_id=vault.id)[0].values, (0.5, 1.0))
        embeddings.invalidate(app_user_id=user.id, vault_id=vault.id)
        self.assertIsNone(embeddings.get_state(app_user_id=user.id, vault_id=vault.id))
        self.assertEqual(len(embeddings.list_metadata_for_vault(app_user_id=user.id,
                                                                vault_id=vault.id)), 1)

    def test_article_flow_lease_owner_expiry_and_release(self) -> None:
        """Косвенные acquire/release проверяются по persisted owner и expiry."""
        from datetime import timedelta

        user = SQLiteAppUserRepository(self.connection).create()
        lease = SQLiteArticleFlowLeaseRepository(self.connection)
        now = datetime(2026, 11, 2, tzinfo=UTC)
        url = "https://example.org/article"
        self.assertTrue(lease.acquire(app_user_id=user.id, normalized_url=url, owner="first",
                                      now=now, expires_at=now + timedelta(minutes=2)))
        self.assertFalse(lease.acquire(app_user_id=user.id, normalized_url=url, owner="second",
                                       now=now + timedelta(minutes=1),
                                       expires_at=now + timedelta(minutes=3)))
        self.assertEqual(self.connection.execute(
            "SELECT owner FROM obsidian_article_flow_leases WHERE app_user_id = ?", (user.id,)
        ).fetchone()[0], "first")
        lease.release(app_user_id=user.id, normalized_url=url, owner="second")
        self.assertEqual(self.connection.execute(
            "SELECT owner FROM obsidian_article_flow_leases WHERE app_user_id = ?", (user.id,)
        ).fetchone()[0], "first")
        self.assertTrue(lease.acquire(app_user_id=user.id, normalized_url=url, owner="second",
                                      now=now + timedelta(minutes=2),
                                      expires_at=now + timedelta(minutes=3)))
        lease.release(app_user_id=user.id, normalized_url=url, owner="first")
        self.assertEqual(self.connection.execute(
            "SELECT owner FROM obsidian_article_flow_leases WHERE app_user_id = ?", (user.id,)
        ).fetchone()[0], "second")
        lease.release(app_user_id=user.id, normalized_url=url, owner="second")
        self.assertIsNone(self.connection.execute(
            "SELECT owner FROM obsidian_article_flow_leases WHERE app_user_id = ?", (user.id,)
        ).fetchone())

    def test_vault_lease_get_exclusive_owner_and_expiry(self) -> None:
        """Lease get/run_exclusive проверяют owner, продление и отказ без commit."""
        from datetime import timedelta

        user = SQLiteAppUserRepository(self.connection).create()
        self._github(user.id)
        vault = SQLiteObsidianVaultRepository(self.connection).replace(self._vault(user.id))
        leases = SQLiteVaultSyncLeaseRepository(self.connection)
        now = datetime.now(UTC)
        acquired = leases.acquire(app_user_id=user.id, vault_id=vault.id,
            owner="first", now=now, expires_at=now + timedelta(minutes=2))
        self.assertEqual(leases.get(app_user_id=user.id, vault_id=vault.id).owner, "first")
        self.assertIsNone(leases.get(app_user_id=user.id + 999, vault_id=vault.id))
        calls = []

        def commit() -> GitHubVaultCommitResult:
            """Запоминает единственную допустимую удалённую запись."""
            calls.append("commit")
            return GitHubVaultCommitResult("commit", "tree", "blob")

        with self.assertRaises(VaultLeaseLostError):
            leases.run_exclusive(app_user_id=user.id, vault_id=vault.id,
                owner="wrong", clock=lambda: now, duration=timedelta(minutes=3),
                operation=commit)
        self.assertEqual(calls, [])
        result = leases.run_exclusive(app_user_id=user.id, vault_id=vault.id,
            owner="first", clock=lambda: now, duration=timedelta(minutes=3),
            operation=commit)
        self.assertEqual((result.commit_sha, calls), ("commit", ["commit"]))
        renewed = leases.get(app_user_id=user.id, vault_id=vault.id)
        self.assertEqual(renewed.owner, "first")
        self.assertEqual(renewed.expires_at, now + timedelta(minutes=3))
        self.assertFalse(leases.release(app_user_id=user.id, vault_id=vault.id, owner="wrong"))
        self.assertEqual(leases.get(app_user_id=user.id, vault_id=vault.id).owner, "first")
        self.assertTrue(leases.release(app_user_id=user.id, vault_id=vault.id, owner="first"))
        self.assertIsNone(leases.get(app_user_id=user.id, vault_id=vault.id))
        self.assertEqual(acquired.owner, "first")

    def test_chat_mode_save_and_preference_upsert_preserve_loser(self) -> None:
        """Косвенные writes фиксируют scope, прежнее значение и CAS loser."""
        users = SQLiteAppUserRepository(self.connection)
        first, second = users.create(), users.create()
        preferences = SQLiteUserCapabilityPreferenceRepository(self.connection)
        self.assertIsNone(preferences.upsert(UserCapabilityPreference(first.id, "obsidian", True)))
        self.assertTrue(preferences.upsert(UserCapabilityPreference(first.id, "obsidian", False)))
        self.assertEqual(preferences.get_for_user(first.id),
                         (UserCapabilityPreference(first.id, "obsidian", False),))
        self.assertEqual(preferences.get_for_user(second.id), ())
        modes = SQLiteChatModeRepository(self.connection)
        key = ChatModeKey(first.id, "telegram", "chat-1")
        other = ChatModeKey(second.id, "telegram", "chat-1")
        modes.save(ChatMode(key, "obsidian"))
        self.assertEqual((modes.get(key).capability_id, modes.get_state(key).revision),
                         ("obsidian", 1))
        self.assertIsNone(modes.get(other))
        self.assertFalse(modes.replace_if_revision(key, 0, "ask"))
        self.assertEqual(modes.get(key).capability_id, "obsidian")
        modes.save(ChatMode(key, "ask"))
        self.assertEqual((modes.get(key).capability_id, modes.get_state(key).revision),
                         ("ask", 2))

    def test_incoming_message_indirect_reads_and_fk_rejection(self) -> None:
        """Прямые get/find подтверждают owner, duplicate и атомарный FK отказ."""
        user = SQLiteAppUserRepository(self.connection).create()
        messages = SQLiteIncomingMessageRepository(self.connection)
        incoming = ArticleIncomingMessage(channel="telegram", chat_id="chat",
                                          message_id="message", text="Article", app_user_id=user.id)
        saved = messages.save(incoming)
        self.assertEqual(messages.get_by_id(saved.id).text, "Article")
        self.assertEqual(messages.find_by_external_id(channel="telegram", chat_id="chat",
                                                      message_id="message").id, saved.id)
        self.assertIsNone(messages.find_by_external_id(channel="vk", chat_id="chat",
                                                       message_id="message"))
        duplicate = messages.save(ArticleIncomingMessage(
            channel="telegram", chat_id="chat", message_id="message",
            text="Changed", app_user_id=user.id,
        ))
        self.assertEqual((duplicate.id, duplicate.text), (saved.id, "Article"))
        with self.assertRaises(sqlite3.IntegrityError):
            messages.link_to_article(incoming_message_id=saved.id, article_id=999999)
        self.assertIsNone(messages.get_by_id(saved.id).article_id)

    def test_external_identity_direct_reads_reassign_touch_and_token(self) -> None:
        """Прямые postconditions закрывают косвенные identity и token paths."""
        from datetime import timedelta

        users = SQLiteAppUserRepository(self.connection)
        first, second = users.create(), users.create()
        identities = SQLiteExternalIdentityRepository(self.connection)
        original = IncomingIdentity("telegram", "42", "42", username="first")
        created = identities.create(app_user_id=first.id, identity=original)
        self.assertEqual(identities.find(original).id, created.id)
        self.assertEqual(identities.find_private_chat("telegram", "42").app_user_id, first.id)
        self.assertTrue(identities.has_chat_address("telegram", "42"))
        self.assertEqual([item.id for item in identities.list_for_user(first.id)], [created.id])
        self.assertEqual([item.id for item in identities.list_all_private_chats()], [created.id])
        self.assertEqual(identities.list_for_user(second.id), ())
        changed = IncomingIdentity("telegram", "42", "43", username="new")
        identities.touch(changed)
        self.assertEqual((identities.find(original).external_chat_id,
                          identities.find(original).username), ("43", "new"))
        self.assertIsNone(identities.find_private_chat("telegram", "42"))
        self.assertFalse(identities.has_chat_address("telegram", "42"))
        reassigned = identities.reassign(app_user_id=second.id, identity=IncomingIdentity(
            "telegram", "42", "42", username="second"))
        self.assertEqual((reassigned.id, reassigned.app_user_id), (created.id, second.id))
        self.assertEqual((identities.count_for_user(first.id), identities.count_for_user(second.id)), (0, 1))
        self.assertEqual([item.id for item in identities.list_for_user(second.id)], [created.id])
        tokens = SQLiteIdentityLinkTokenRepository(self.connection)
        now = datetime(2026, 11, 2, tzinfo=UTC)
        tokens.create(app_user_id=first.id, token_hash="token-hash",
                      expires_at=now + timedelta(minutes=2))
        self.assertEqual(tokens.find_active(token_hash="token-hash", now=now), first.id)
        self.assertIsNone(tokens.find_active(token_hash="token-hash",
                                              now=now + timedelta(minutes=2)))
        self.assertIsNone(tokens.find_active(token_hash="missing", now=now))

    def test_identity_lifecycle_register_link_and_token_consumption(self) -> None:
        """Lifecycle сохраняет owner и погашает код только вместе с новой identity."""
        from datetime import timedelta

        lifecycle = SQLiteIdentityLifecycleRepository(self.connection)
        identities = SQLiteExternalIdentityRepository(self.connection)
        tokens = SQLiteIdentityLinkTokenRepository(self.connection)
        now = datetime(2026, 11, 2, tzinfo=UTC)
        original = IncomingIdentity("telegram", "42", "42", username="first")
        first = lifecycle.register_with_name(original, display_name="Original")
        self.assertEqual(identities.find(original).app_user_id, first.id)
        repeated = lifecycle.register_with_name(original, display_name="Ignored")
        self.assertEqual((repeated.id, repeated.display_name), (first.id, "Original"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 1)
        tokens.create(app_user_id=first.id, token_hash="link-token",
                      expires_at=now + timedelta(minutes=2))
        linked_identity = IncomingIdentity("vk", "77", "77")
        self.assertEqual(lifecycle.link(identity=linked_identity, token_hash="link-token",
                                        now=now).id, first.id)
        self.assertEqual(identities.find(linked_identity).app_user_id, first.id)
        self.assertIsNone(tokens.find_active(token_hash="link-token", now=now))
        self.assertEqual(identities.count_for_user(first.id), 2)

    def test_identity_rebind_save_find_decide_cas_and_owner(self) -> None:
        """Rebind хранит адрес, версию, погашает token и переносит identity."""
        from datetime import timedelta

        users = SQLiteAppUserRepository(self.connection)
        source, target = users.create(), users.create()
        identities = SQLiteExternalIdentityRepository(self.connection)
        identity = IncomingIdentity("telegram", "42", "42", username="source")
        identities.create(app_user_id=source.id, identity=identity)
        now = datetime(2026, 11, 2, tzinfo=UTC)
        tokens = SQLiteIdentityLinkTokenRepository(self.connection)
        tokens.create(app_user_id=target.id, token_hash="rebind-token",
                      expires_at=now + timedelta(minutes=10))
        confirmations = SQLiteIdentityRebindConfirmationRepository(self.connection)
        pending = confirmations.save(identity=identity, token_hash="rebind-token",
            source_app_user_id=source.id, target_app_user_id=target.id,
            expires_at=now + timedelta(minutes=10), now=now)
        self.assertEqual((pending.source_app_user_id, pending.target_app_user_id),
                         (source.id, target.id))
        self.assertEqual(confirmations.find(identity=identity, now=now).id, pending.id)
        wrong_origin = IncomingIdentity("telegram", "42", "43")
        self.assertIsNone(confirmations.find(identity=wrong_origin, now=now))
        self.assertIsNone(confirmations.decide(identity=wrong_origin,
            confirmation_id=pending.id, version=pending.version, approve=True, now=now))
        self.assertIsNone(confirmations.decide(identity=identity,
            confirmation_id=pending.id, version=pending.version + 1,
            approve=True, now=now))
        self.assertEqual(identities.find(identity).app_user_id, source.id)
        self.assertEqual(tokens.find_active(token_hash="rebind-token", now=now), target.id)
        self.assertEqual(confirmations.decide(identity=identity,
            confirmation_id=pending.id, version=pending.version, approve=True,
            now=now), target.id)
        self.assertEqual(identities.find(identity).app_user_id, target.id)
        self.assertIsNone(tokens.find_active(token_hash="rebind-token", now=now))
        self.assertIsNone(users.get_by_id(source.id))
        self.assertIsNone(confirmations.find(identity=identity, now=now))


if __name__ == "__main__":
    unittest.main()
