from tests.response_assertions import content_text, response_text
"""Проверки шага 2.4 с настоящими SQLite repositories и автономными gateways."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import sqlite3
import unittest

from compadre_assistant.bootstrap import create_user_identity_service
from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import (
    SQLiteArticleAnalysisResultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.article_flow_lease_repository import (
    SQLiteArticleFlowLeaseRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import (
    SQLiteArticleRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import (
    SQLiteObsidianProposalRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_confirmation_repository import (
    SQLiteVaultActionConfirmationRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import (
    SQLiteVaultInstructionRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import (
    SQLiteVaultNoteRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import (
    SQLiteVaultSyncLeaseRepository,
)
from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleUseCase
from compadre_assistant.capabilities.obsidian.domain.articles.extracted import ExtractedArticle
from compadre_assistant.capabilities.obsidian.domain.articles.html import ArticleHtml
from compadre_assistant.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlUseCase
from compadre_assistant.capabilities.obsidian.domain.pending import (
    PendingOrigin,
    PendingOperationError,
    VaultResourceBusyError,
)
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import (
    ObsidianConfirmationStatus,
    ObsidianProposalConfirmationService,
)
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposalAction,
    ObsidianProposalStatus,
    ObsidianReviewPlan,
)
from compadre_assistant.capabilities.obsidian.domain.reviews.proposal import PrepareObsidianReviewUseCase
from compadre_assistant.capabilities.obsidian.domain.search.models import VaultSearchResult
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import (
    VaultActionConfirmation,
    VaultConfirmationAction,
    VaultNote,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import (
    GitHubVaultTargetState,
    GitHubVaultCommitResult,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_selection import (
    GitHubVaultSelectionService,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import (
    VaultSyncService,
    VaultSyncStatus,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.domain.users.identity import InvalidLinkCodeError, IdentityAlreadyBoundError
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from sqlite_helpers import ensure_app_user

TG = PendingOrigin("telegram", "tg-chat")
VK = PendingOrigin("vk", "vk-chat")


class OperationScopesTest(unittest.TestCase):
    """Проверяет исходный чат, адресные решения, dedup и общие ресурсы."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="operation-scopes-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.db"
        self.gateway = MemoryGitHub()
        self.fetcher = Fetcher()
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            ensure_app_user(connection)
            ensure_app_user(connection, app_user_id=2)
            connection.executescript("""
                INSERT INTO external_identities (app_user_id, channel, external_user_id, external_chat_id)
                VALUES (1, 'telegram', 'tg-user', 'tg-chat'), (1, 'vk', 'vk-user', 'vk-chat');
                INSERT INTO github_accounts (app_user_id, github_user_id, login) VALUES (1, 100, 'owner');
                INSERT INTO github_installations (app_user_id, installation_id) VALUES (1, 200);
                INSERT INTO obsidian_vaults (id, app_user_id, installation_id, repository_id, owner,
                    repository, branch, head_commit_sha, tree_sha)
                VALUES (1, 1, 200, 300, 'owner', 'vault', 'main', 'base-commit', 'base-tree');
                INSERT INTO obsidian_vault_instructions (app_user_id, vault_id, position, path, blob_sha, content)
                VALUES (1, 1, 0, 'rules.md', 'rules-sha', 'Создавай отдельные заметки.');
            """)
            SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                UserCapabilityPreference(1, "obsidian", True)
            )

    def service(self, connection):
        """Собирает применение proposals через общий SQLite lease."""
        return ObsidianProposalConfirmationService(
            proposal_repository=SQLiteObsidianProposalRepository(connection),
            vault_repository=SQLiteObsidianVaultRepository(connection),
            lease_repository=SQLiteVaultSyncLeaseRepository(connection),
            github_gateway=self.gateway,
        )

    def dispatcher(self, connection):
        """Собирает настоящий fetch/analysis/review с fake внешними границами."""
        articles = SQLiteArticleRepository(connection)
        handler = ObsidianMessageHandler(
            article_url_use_case=ProcessArticleUrlUseCase(
                article_repository=articles, html_fetcher=self.fetcher, text_extractor=Extractor()
            ),
            article_analysis_use_case=AnalyzeArticleUseCase(
                article_repository=articles,
                analyzer=Analyzer(),
                analysis_result_repository=SQLiteArticleAnalysisResultRepository(connection),
            ),
            obsidian_review_use_case=PrepareObsidianReviewUseCase(
                article_repository=articles,
                vault_repository=SQLiteObsidianVaultRepository(connection),
                instruction_repository=SQLiteVaultInstructionRepository(connection),
                note_repository=SQLiteVaultNoteRepository(connection),
                search=Search(),
                generator=Generator(),
                proposal_repository=SQLiteObsidianProposalRepository(connection),
            ),
            obsidian_confirmation_service=self.service(connection),
            vault_selection_manager=GitHubVaultSelectionService(
                installation_repository=SQLiteGitHubInstallationRepository(connection),
                vault_repository=SQLiteObsidianVaultRepository(connection),
                confirmation_repository=SQLiteVaultActionConfirmationRepository(connection),
                github_gateway=self.gateway,
            ),
            article_flow_lease_repository=SQLiteArticleFlowLeaseRepository(connection),
        )
        return IncomingMessageDispatcher(
            registry=CapabilityRegistry((handler,)),
            user_identity_service=create_user_identity_service(connection),
            chat_mode_repository=SQLiteChatModeRepository(connection),
            capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
        )

    def process(self, text, origin=TG):
        """Имитирует отдельное соединение worker для каждого события канала."""
        user = "tg-user" if origin.channel == "telegram" else "vk-user"
        with connect_database(self.path) as connection:
            return self.dispatcher(connection).execute(
                IncomingEvent(
                    origin.channel, origin.chat_id, str(datetime.now(UTC).timestamp()), text, user
                )
            )

    def pending(self, origin=TG):
        """Читает текущие ожидания через отдельное соединение."""
        with connect_database(self.path) as connection:
            return SQLiteObsidianProposalRepository(connection).list_pending(1, origin)

    def prepare(self, url="https://example.com/a", origin=TG):
        """Входит в Obsidian и подготавливает отдельную статью."""
        response = self.process("/obsidian " + url, origin)
        self.assertIsNone(response.error)
        self.assertEqual(len(response.actions), 2)
        return response

    def test_tg_vk_and_same_channel_chats_prepare_independent_articles(self):
        """Три чата одного владельца независимо проходят полный article flow."""
        self.prepare(origin=TG)
        self.prepare("https://example.com/b", VK)
        other = PendingOrigin("telegram", "other-tg-chat")
        self.prepare("https://example.com/c", other)
        self.assertEqual([len(self.pending(origin)) for origin in (TG, VK, other)], [1, 1, 1])
        local = self.process("/obsidian", VK)
        self.assertIn("Notes/2.md", response_text(local))
        self.assertNotIn("Notes/1.md", response_text(local))
        self.assertEqual(len(local.actions), 2)

    def test_two_local_proposals_require_id_and_do_not_consider_remote_pending(self):
        """Локальная неоднозначность не включает ожидания другого канала."""
        first = self.prepare()
        self.prepare("https://example.com/b", TG)
        remote = self.prepare("https://example.com/c", VK)
        ambiguous = self.process("да", TG)
        self.assertIn("неоднозначен", response_text(ambiguous))
        self.assertEqual(self.gateway.commits, 0)
        cancelled = self.process(first.actions[1].command, TG)
        self.assertIn("отменено", response_text(cancelled))
        self.assertEqual(len(self.pending(TG)), 1)
        self.process(remote.actions[1].command, VK)
        self.assertEqual(len(self.pending(VK)), 0)
        self.process("да", TG)
        self.assertEqual(self.gateway.commits, 1)

    def test_foreign_owner_or_chat_cannot_inspect_or_cancel_pending(self):
        """Связанный VK и другой TG-чат получают отказ без пути, reasoning и Markdown."""
        response = self.prepare()
        for origin in (VK, PendingOrigin("telegram", "other")):
            self.process("/obsidian", origin)
            denied = self.process(response.actions[0].command, origin)
            self.assertIn("исходном чате", response_text(denied))
            self.assertNotIn("Notes/1.md", response_text(denied))
            self.assertNotIn("Новый материал", response_text(denied))
            self.assertEqual(denied.actions, ())
        with connect_database(self.path) as connection:
            pending = self.pending()[0]
            result = self.service(connection).confirm(
                2, proposal_id=pending.id, origin=TG, version=pending.version
            )
            self.assertEqual(result.status, ObsidianConfirmationStatus.NO_PENDING)
            self.assertIsNone(result.proposal)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.gateway.commits, 0)

    def test_exit_preserves_pending_and_profile_commands_remain_available(self):
        """Выход сохраняет адресное ожидание и требует явного входа перед решением."""
        response = self.prepare()
        self.process("/exit")
        self.assertIn("Войди снова", response_text(self.process(response.actions[0].command)))
        self.assertIn("Профиль", response_text(self.process("/status")))
        self.assertEqual(len(self.pending()), 1)
        reopened = self.process("/obsidian")
        self.assertEqual(reopened.actions, response.actions)
        self.process(reopened.actions[1].command)
        self.assertEqual(len(self.pending()), 0)

    def test_duplicate_normalized_article_reuses_proposal_and_origin(self):
        """Повтор статьи не создаёт второй proposal и не раскрывает чужое ожидание."""
        first = self.prepare("https://example.com/a?utm_source=tg")
        second = self.process("https://example.com/a?utm_source=other")
        self.assertEqual(first.actions, second.actions)
        remote = self.process("/obsidian https://example.com/a", VK)
        self.assertIn("исходном чате", response_text(remote))
        self.assertEqual(remote.actions, ())
        self.assertEqual(self.fetcher.calls, 1)
        with connect_database(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM obsidian_proposals").fetchone()[0], 1
            )

    def test_simultaneous_same_url_is_reserved_for_one_full_flow(self):
        """Второе SQLite-соединение не выполняет fetch/analysis для занятого URL."""
        entered, release = Event(), Event()
        self.fetcher.entered, self.fetcher.release = entered, release
        with ThreadPoolExecutor(max_workers=1) as executor:
            original = executor.submit(self.process, "/obsidian https://example.com/a", TG)
            self.assertTrue(entered.wait(2))
            try:
                duplicate = self.process("/obsidian https://example.com/a", VK)
                self.assertIn("уже обрабатывается", response_text(duplicate))
                self.assertEqual(duplicate.actions, ())
            finally:
                release.set()
            self.assertIsNone(original.result(timeout=3).error)
        self.assertEqual(self.fetcher.calls, 1)
        self.assertEqual(len(self.pending()), 1)

    def test_different_article_runs_while_other_chat_is_fetching(self):
        """Захваченный article flow не резервирует остальные статьи профиля."""
        entered, release = Event(), Event()
        blocked_fetcher = Fetcher()
        blocked_fetcher.entered, blocked_fetcher.release = entered, release

        def run_first():
            with connect_database(self.path) as connection:
                dispatcher = self.dispatcher(connection)
                dispatcher._registry.by_id("obsidian")._article_url_use_case._html_fetcher = (
                    blocked_fetcher
                )
                return dispatcher.execute(
                    IncomingEvent(
                        "telegram",
                        TG.chat_id,
                        "first",
                        "/obsidian https://example.com/a",
                        "tg-user",
                    )
                )

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(run_first)
            self.assertTrue(entered.wait(2))
            try:
                self.prepare("https://example.com/b", VK)
                self.assertEqual(len(self.pending(VK)), 1)
            finally:
                release.set()
            self.assertIsNone(first.result(timeout=3).error)
        self.assertEqual(len(self.pending(TG)), 1)

    def test_force_reanalysis_preserves_existing_pending_and_its_origin(self):
        """Повторный анализ статьи не подменяет локальное или чужое ожидание."""
        response = self.prepare()
        pending = self.pending()[0]
        for origin in (TG, VK):
            self.process("/obsidian", origin)
            denied = self.process(f"/reanalyze {pending.article_id}", origin)
            self.assertIn("исходном чате", response_text(denied))
            self.assertEqual(denied.actions, ())
        self.assertEqual(self.pending()[0].id, pending.id)
        self.assertEqual(self.process("/obsidian").actions, response.actions)
        self.assertEqual(self.fetcher.calls, 1)

    def test_unknown_command_does_not_treat_its_argument_as_article_url(self):
        """Неизвестная slash-команда не запускает предметный pipeline через URL аргумента."""
        self.process("/obsidian")
        reply = self.process("/unknown https://example.com/a")
        self.assertIn("Не знаю такой команды Obsidian", response_text(reply))
        self.assertEqual(self.fetcher.calls, 0)
        self.assertFalse(self.pending())

    def test_lease_busy_keeps_both_proposals_then_stale_sha_conflicts(self):
        """Sync/write общий lease; второе предложение требует нового review после записи первого."""
        first = self.prepare()
        second = self.prepare("https://example.com/b", VK)
        with connect_database(self.path) as connection:
            leases = SQLiteVaultSyncLeaseRepository(connection)
            now = datetime.now(UTC)
            leases.acquire(
                app_user_id=1,
                vault_id=1,
                owner="sync-worker",
                now=now,
                expires_at=now + timedelta(minutes=5),
            )
            busy = self.process(second.actions[0].command, VK)
            self.assertIn("занят", response_text(busy))
            self.assertEqual(len(self.pending(VK)), 1)
            self.assertEqual(len(self.pending(TG)), 1)
            leases.release(app_user_id=1, vault_id=1, owner="sync-worker")
        self.process(first.actions[0].command)
        self.assertEqual(self.gateway.commits, 1)
        conflict = self.process(second.actions[0].command, VK)
        self.assertIn("Хранилище изменилось", response_text(conflict))
        self.assertEqual(self.gateway.commits, 1)
        self.assertFalse(self.pending(VK))
        refreshed = self.prepare("https://example.com/b", VK)
        self.assertNotEqual(refreshed.actions[0].command, second.actions[0].command)
        self.process(second.actions[0].command, VK)
        self.assertEqual(self.gateway.commits, 1)
        self.process(refreshed.actions[0].command, VK)
        self.assertEqual(self.gateway.commits, 2)

    def test_repeat_confirmation_and_atomic_claim_do_not_write_twice(self):
        """Двойной адресный ответ и конкурирующие claims завершают конкретный ID один раз."""
        response = self.prepare()
        pending = self.pending()[0]
        with connect_database(self.path) as first, connect_database(self.path) as second:
            now = datetime.now(UTC)
            arguments = dict(
                proposal_id=pending.id,
                app_user_id=1,
                origin=TG,
                version=pending.version,
                now=now,
                expires_at=now + timedelta(minutes=5),
            )
            claimed = SQLiteObsidianProposalRepository(first).claim(**arguments)
            self.assertIsNotNone(claimed)
            self.assertIsNone(SQLiteObsidianProposalRepository(second).claim(**arguments))
            self.assertIsNone(
                SQLiteObsidianProposalRepository(second).mark_cancelled(
                    proposal_id=pending.id, app_user_id=1, origin=TG, version=pending.version
                )
            )
            restored = SQLiteObsidianProposalRepository(first).release_claim(claimed)
        stale = self.process(response.actions[0].command)
        self.assertIn("устарело", response_text(stale))
        current = self.process("/obsidian")
        self.process(current.actions[0].command)
        self.process(current.actions[0].command)
        self.assertEqual(self.gateway.commits, 1)
        self.assertGreater(restored.version, pending.version)

    def test_concurrent_confirmations_keep_remote_write_sequential(self):
        """Два worker не записывают одновременно и не теряют ожидающее второе решение."""
        first = self.prepare()
        second = self.prepare("https://example.com/b", VK)
        entered, release = Event(), Event()
        self.gateway.entered, self.gateway.release = entered, release
        with ThreadPoolExecutor(max_workers=1) as executor:
            applying = executor.submit(self.process, first.actions[0].command, TG)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(self.gateway.commits, 0)
            finally:
                release.set()
            self.assertIsNone(applying.result(timeout=3).error)
        self.assertEqual(self.gateway.commits, 1)
        self.assertIn("Хранилище изменилось", response_text(self.process(second.actions[0].command, VK)))
        self.assertEqual(self.gateway.commits, 1)

    def test_expired_lease_taken_during_preflight_blocks_remote_commit(self):
        """После takeover между SHA preflight и commit старый worker не пишет GitHub."""
        self.prepare()
        pending = self.pending()[0]
        original_inspect = self.gateway.inspect_vault_target

        def takeover(vault, *, target_path):
            state = original_inspect(vault, target_path=target_path)
            with connect_database(self.path) as second:
                second.execute(
                    "UPDATE obsidian_vault_sync_leases SET expires_at = ? WHERE vault_id = 1",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
                )
                second.commit()
                acquired = SQLiteVaultSyncLeaseRepository(second).acquire(
                    app_user_id=1, vault_id=1, owner="vk-worker",
                    now=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
                self.assertIsNotNone(acquired)
            return state

        self.gateway.inspect_vault_target = takeover
        with connect_database(self.path) as first:
            result = self.service(first).confirm(
                1, proposal_id=pending.id, origin=TG, version=pending.version,
            )
        self.assertEqual(result.status, ObsidianConfirmationStatus.IN_PROGRESS)
        self.assertEqual(self.gateway.commits, 0)
        with connect_database(self.path) as connection:
            vault = SQLiteObsidianVaultRepository(connection).get_for_user(1)
            self.assertEqual(vault.head_commit_sha, "base-commit")
            self.assertEqual(SQLiteVaultNoteRepository(connection).list_for_vault(
                app_user_id=1, vault_id=1,
            ), [])

    def test_remote_commit_holds_fence_during_delayed_http_call(self):
        """Другой процесс не захватывает даже истёкший TTL до ответа GitHub."""
        first = self.prepare()
        with connect_database(self.path) as connection:
            connection.executescript("""
                INSERT INTO github_accounts (app_user_id, github_user_id, login)
                VALUES (2, 101, 'other');
                INSERT INTO github_installations (app_user_id, installation_id)
                VALUES (2, 201);
                INSERT INTO obsidian_vaults (id, app_user_id, installation_id,
                    repository_id, owner, repository, branch)
                VALUES (2, 2, 201, 301, 'other', 'vault', 'main');
            """)
            pending_disconnect = SQLiteVaultActionConfirmationRepository(connection).save(
                VaultActionConfirmation(
                    app_user_id=1, origin=TG, base_vault_id=1,
                    action=VaultConfirmationAction.DISCONNECT,
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                ),
                now=datetime.now(UTC),
            )
        entered, release, attempted = Event(), Event(), Event()
        self.gateway.entered, self.gateway.release = entered, release

        def contender():
            with closing(sqlite3.connect(self.path, timeout=5)) as connection:
                connection.row_factory = sqlite3.Row
                attempted.set()
                return SQLiteVaultSyncLeaseRepository(connection).acquire(
                    app_user_id=1, vault_id=1, owner="vk-worker",
                    now=datetime.now(UTC) + timedelta(minutes=10),
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            applying = executor.submit(self.process, first.actions[0].command, TG)
            self.assertTrue(entered.wait(2))
            competing = executor.submit(contender)
            self.assertTrue(attempted.wait(2))
            self.assertIsNone(competing.result(timeout=3))
            with connect_database(self.path) as independent:
                SQLiteVaultNoteRepository(independent).upsert(
                    VaultNote(2, 2, "other.md", "other-blob", "# Other")
                )
                self.assertEqual(len(SQLiteVaultNoteRepository(independent).list_for_vault(
                    app_user_id=2, vault_id=2,
                )), 1)
                with self.assertRaises(VaultResourceBusyError):
                    SQLiteVaultActionConfirmationRepository(independent).decide(
                        pending_disconnect, approve=True, now=datetime.now(UTC),
                    )
            release.set()
            self.assertIsNone(applying.result(timeout=3).error)
        self.assertEqual(self.gateway.commits, 1)
        self.assertIsNotNone(contender())

    def test_configuration_pending_is_local_and_replace_disconnect_are_exclusive(self):
        """Конфликт за конфигурацию не подменяет ожидание и не блокирует другую статью."""
        with connect_database(self.path) as connection:
            confirmations = SQLiteVaultActionConfirmationRepository(connection)
            candidate = replace(
                SQLiteObsidianVaultRepository(connection).get_for_user(1),
                id=None,
                repository="secret-replacement",
                repository_id=301,
            )
            now = datetime.now(UTC)
            pending = confirmations.save(
                VaultActionConfirmation(
                    app_user_id=1,
                    origin=TG,
                    base_vault_id=1,
                    action=VaultConfirmationAction.REPLACE,
                    replacement=candidate,
                    expires_at=now + timedelta(minutes=10),
                ),
                now=now,
            )
            with self.assertRaises(VaultResourceBusyError):
                confirmations.save(
                    VaultActionConfirmation(
                        app_user_id=1,
                        origin=VK,
                        base_vault_id=1,
                        action=VaultConfirmationAction.DISCONNECT,
                        expires_at=now + timedelta(minutes=10),
                    ),
                    now=now,
                )
            self.assertEqual(confirmations.find_active(app_user_id=1, now=now).id, pending.id)
            with self.assertRaises(PendingOperationError):
                confirmations.decide(replace(pending, origin=VK), approve=True, now=now)
        other = self.process("/obsidian", VK)
        self.assertNotIn("secret-replacement", response_text(other))
        self.assertFalse(other.actions)
        self.prepare("https://example.com/b", VK)
        conflict = self.process("/github_disconnect", VK)
        self.assertIn("Конфигурация vault", response_text(conflict))
        self.assertNotIn("secret-replacement", response_text(conflict))
        self.assertEqual(conflict.actions, ())

    def test_configuration_decision_checks_ttl_version_and_shared_write_lease(self):
        """Занятый ресурс и неверная версия сохраняют адресное ожидание конфигурации."""
        self.process("/obsidian")
        response = self.process("/github_disconnect")
        with connect_database(self.path) as connection:
            repository = SQLiteVaultActionConfirmationRepository(connection)
            now = datetime.now(UTC)
            pending = repository.find_active(app_user_id=1, now=now)
            with self.assertRaises(PendingOperationError):
                repository.decide(replace(pending, version=999), approve=True, now=now)
            with self.assertRaises(PendingOperationError):
                repository.decide(pending, approve=True, now=pending.expires_at)
            leases = SQLiteVaultSyncLeaseRepository(connection)
            leases.acquire(
                app_user_id=1,
                vault_id=1,
                owner="write",
                now=now,
                expires_at=now + timedelta(minutes=5),
            )
            with self.assertRaises(VaultResourceBusyError):
                repository.decide(pending, approve=True, now=now)
            self.assertEqual(
                repository.find_active(app_user_id=1, now=now).version, pending.version
            )
            leases.release(app_user_id=1, vault_id=1, owner="write")
        self.process(response.actions[0].command)
        self.process(response.actions[0].command)
        with connect_database(self.path) as connection:
            self.assertIsNone(SQLiteObsidianVaultRepository(connection).get_for_user(1))

    def test_identity_confirmation_does_not_intercept_obsidian_yes(self):
        """Профиль требует адресного core-действия, простой ответ относится к Obsidian."""
        self.prepare()
        with connect_database(self.path) as connection:
            service = create_user_identity_service(connection)
            target = service.register_with_name(IncomingIdentity("telegram", "target-user", "target-chat"), display_name="Анатолий")
            code = service.create_link_code(target.id)
            pending = service.request_rebind_confirmation(
                code=code.code, identity=IncomingIdentity("telegram", "tg-user", TG.chat_id)
            )
        self.process("да")
        self.assertEqual(self.gateway.commits, 1)
        with connect_database(self.path) as connection:
            service = create_user_identity_service(connection)
            self.assertIsNotNone(
                service.get_pending_rebind(IncomingIdentity("telegram", "tg-user", TG.chat_id))
            )
            with self.assertRaises(InvalidLinkCodeError):
                service.confirm_rebind(
                    IncomingIdentity("telegram", "tg-user", "other"),
                    confirmation_id=pending.id,
                    version=pending.version,
                )
            with self.assertRaises(InvalidLinkCodeError):
                service.confirm_rebind(
                    IncomingIdentity("telegram", "tg-user", TG.chat_id),
                    confirmation_id=pending.id,
                    version=999,
                )
        response = self.process(f"/confirm core identity {pending.id} {pending.version} yes")
        self.assertIn("перепривязал", response_text(response))
        with connect_database(self.path) as connection:
            user = create_user_identity_service(connection).resolve(
                IncomingIdentity("telegram", "tg-user", TG.chat_id)
            )
            self.assertEqual(user.id, target.id)
        repeated = self.process(f"/confirm core identity {pending.id} {pending.version} yes")
        self.assertIn("устарело или истекло", response_text(repeated))

    def test_superseded_identity_operation_never_confirms_new_request(self):
        """Новое ожидание identity имеет новый ID; команда старого ID не переносит профиль."""
        identity = IncomingIdentity("telegram", "tg-user", TG.chat_id)
        with connect_database(self.path) as connection:
            service = create_user_identity_service(connection)
            code = service.create_link_code(2)
            previous = service.request_rebind_confirmation(code=code.code, identity=identity)
            current = service.request_rebind_confirmation(code=code.code, identity=identity)
            self.assertNotEqual(current.id, previous.id)
            with self.assertRaises(InvalidLinkCodeError):
                service.confirm_rebind(
                    identity, confirmation_id=previous.id, version=previous.version
                )
            self.assertEqual(service.resolve(identity).id, 1)
            self.assertEqual(service.get_pending_rebind(identity).id, current.id)
            with self.assertRaises(IdentityAlreadyBoundError):
                service.request_rebind_confirmation(
                    code=code.code, identity=IncomingIdentity("telegram", "tg-user", "other-chat")
                )
            self.assertEqual(service.get_pending_rebind(identity).id, current.id)
            self.assertEqual(
                service.confirm_rebind(
                    identity, confirmation_id=current.id, version=current.version
                ).id,
                2,
            )


class Fetcher:
    """Автономно извлекает источник, допускающий управляемую конкурентную проверку."""

    def __init__(self):
        self.calls = 0
        self.entered = self.release = None

    def fetch(self, url):
        """Ожидает test barrier при проверке полного article claim."""
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("Test release timed out")
        return ArticleHtml(source_url=url, final_url=url, content="<p>Материал</p>")


class Extractor:
    """Извлекает постоянный текст без внешней библиотеки."""

    def extract(self, html):
        """Возвращает минимальный полноценный результат extraction."""
        return ExtractedArticle(
            source_url=html.source_url,
            final_url=html.final_url,
            title="Статья",
            cleaned_text="Новый материал статьи.",
        )


class Analyzer:
    """Формирует анализ с правильным внутренним владельцем статьи."""

    def analyze(self, article):
        """Возвращает независимый анализ статьи без LLM API."""
        return ArticleAnalysisResult(
            article_id=article.id,
            app_user_id=article.app_user_id,
            llm_model="fake",
            prompt_version="v1",
            result_text="## Кратко\nНовый материал.",
        )


class Search:
    """Имитирует пустой retrieval с настоящей моделью поискового результата."""

    def search(self, *, query, vault_id, **_kwargs):
        """Позволяет review выбрать отдельную заметку для статьи."""
        return VaultSearchResult(
            query=query, vault_id=vault_id, hits=(), lexical_candidates=0, vector_candidates=0
        )


class Generator:
    """Готовит уникальный путь статьи и полноценный Markdown."""

    def plan(self, context):
        """Выбирает отдельный файл для каждого article ID."""
        return ObsidianReviewPlan(
            ObsidianProposalAction.ADD, "Новый материал", f"Notes/{context.article.id}.md"
        )

    def write_markdown(self, context):
        """Возвращает Markdown с заголовком статьи."""
        return f"# Заметка {context.article.id}\nМатериал."


class MemoryGitHub:
    """Хранит удалённые SHA и Markdown для проверки optimistic concurrency."""

    def __init__(self):
        self.head, self.tree = "base-commit", "base-tree"
        self.notes = {}
        self.commits = 0
        self.entered = self.release = None

    def inspect_vault_target(self, vault, *, target_path):
        """Возвращает актуальный remote snapshot в момент SHA preflight."""
        note = self.notes.get(target_path)
        return GitHubVaultTargetState(
            self.head, self.tree, note[0] if note else None, note[1] if note else None
        )

    def commit_vault_markdown(self, vault, *, target_path, markdown, expected_blob_sha):
        """Изменяет snapshot и считает реальные write-вызовы."""
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("Test write release timed out")
        self.commits += 1
        self.head, self.tree = f"commit-{self.commits}", f"tree-{self.commits}"
        blob = f"blob-{self.commits}"
        self.notes[target_path] = (blob, markdown)
        return GitHubVaultCommitResult(self.head, self.tree, blob)


if __name__ == "__main__":
    unittest.main()
