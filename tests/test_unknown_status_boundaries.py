"""Проверяет неизвестные значения на границах данных и безопасный отказ."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import sqlite3
from unittest import TestCase
from unittest.mock import Mock

from compadre_assistant.capabilities.obsidian.data.sqlite.article_dtos import ArticleDto
from compadre_assistant.capabilities.obsidian.data.sqlite.article_mappers import article_from_dto
from compadre_assistant.capabilities.obsidian.data.sqlite.proposal_dtos import ObsidianProposalDto
from compadre_assistant.capabilities.obsidian.data.sqlite.proposal_mappers import proposal_from_dto
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import SQLiteObsidianProposalRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_dtos import VaultActionConfirmationDto
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_mappers import vault_confirmation_from_dto
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_confirmation_repository import SQLiteVaultActionConfirmationRepository
from compadre_assistant.capabilities.obsidian.domain.articles.processing import (
    ProcessArticleUrlCommand, ProcessArticleUrlError, ProcessArticleUrlUseCase,
)
from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import (
    AnalyzeArticleCommand, AnalyzeArticleError, AnalyzeArticleUseCase,
)
from compadre_assistant.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin, PendingOperationError
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import (
    ObsidianConfirmationResult, ObsidianConfirmationStatus, ObsidianProposalConfirmationService,
)
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposalAction, ObsidianProposalStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import (
    VaultConfirmationAction, VaultConfirmationStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_selection import GitHubVaultSelectionService
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageResult
from compadre_assistant.capabilities.obsidian.presentation.responses import format_obsidian_message_result
from compadre_assistant.capabilities.obsidian.presentation.results import ObsidianResultType
from compadre_assistant.core.data.sqlite.user_identity_dtos import IdentityRebindConfirmationDto
from compadre_assistant.core.data.sqlite.user_identity_mappers import rebind_confirmation_from_dto
from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteIdentityRebindConfirmationRepository
from compadre_assistant.core.domain.users.entities import IdentityRebindStatus, IncomingIdentity
from tests.response_assertions import content_text


class UnknownStatusBoundaryTests(TestCase):
    """Проверяет fallback mapper и отсутствие предметных побочных эффектов."""

    def test_article_mapper_and_reprocess_leave_saved_article_unchanged(self) -> None:
        """Неизвестный статус статьи не запускает повторный HTTP pipeline."""
        article = article_from_dto(ArticleDto(
            id=7, app_user_id=1, source_url="https://example.com/post",
            normalized_url="https://example.com/post", title=None, cleaned_text="text",
            text_hash=None, status="future_stage", created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ))
        repository = Mock()
        repository.find_by_normalized_url.return_value = article
        fetcher = Mock()
        extractor = Mock()
        use_case = ProcessArticleUrlUseCase(
            article_repository=repository, html_fetcher=fetcher, text_extractor=extractor,
        )
        with self.assertRaises(ProcessArticleUrlError):
            use_case.execute(ProcessArticleUrlCommand(article.source_url, force_reprocess=True))
        self.assertIs(article.status, ArticleStatus.UNKNOWN)
        repository.update_status.assert_not_called()
        repository.update_content.assert_not_called()
        fetcher.fetch.assert_not_called()
        extractor.extract.assert_not_called()

        repository.get_by_id.return_value = article
        results = Mock()
        analyzer = Mock()
        analysis = AnalyzeArticleUseCase(
            article_repository=repository, analyzer=analyzer,
            analysis_result_repository=results,
        )
        with self.assertRaises(AnalyzeArticleError):
            analysis.execute(AnalyzeArticleCommand(article_id=7, app_user_id=1, force=True))
        repository.update_status.assert_not_called()
        results.save.assert_not_called()
        analyzer.analyze.assert_not_called()

    def test_proposal_mapper_and_confirmation_reject_unknown_action_or_status(self) -> None:
        """Неизвестные поля proposal сохраняют DTO и не захватывают lease."""
        dto = ObsidianProposalDto(
            id=8, app_user_id=3, article_id=7, analysis_id=9, vault_id=4,
            origin_channel="telegram", origin_chat_id="chat", action="merge",
            status="pending", reasoning="Future action", base_commit_sha="base",
            base_tree_sha="tree", target_path="Tech/A.md", proposed_markdown="# A",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        repository = Mock()
        vault_repository = Mock()
        lease_repository = Mock()
        github = Mock()
        service = ObsidianProposalConfirmationService(
            proposal_repository=repository, vault_repository=vault_repository,
            lease_repository=lease_repository, github_gateway=github,
        )
        for current_dto in (dto, replace(dto, action="add", status="future_status")):
            with self.subTest(action=current_dto.action, status=current_dto.status):
                proposal = proposal_from_dto(current_dto)
                repository.get_by_id.return_value = proposal
                result = service.confirm(
                    3, proposal_id=8, origin=PendingOrigin("telegram", "chat"), version=1,
                )
                self.assertIs(result.status, ObsidianConfirmationStatus.NO_PENDING)
        self.assertIs(proposal_from_dto(dto).action, ObsidianProposalAction.UNKNOWN)
        self.assertIs(proposal_from_dto(replace(dto, status="future_status")).status,
                      ObsidianProposalStatus.UNKNOWN)
        repository.claim.assert_not_called()
        repository.mark_cancelled.assert_not_called()
        lease_repository.acquire.assert_not_called()
        github.commit_vault_markdown.assert_not_called()

        historical = proposal_from_dto(replace(dto, status="applied"))
        rendered = format_obsidian_message_result(ObsidianMessageResult(
            type=ObsidianResultType.ARTICLE_REVIEWED_RESULT,
            obsidian_confirmation=ObsidianConfirmationResult(
                ObsidianConfirmationStatus.APPLIED, proposal=historical,
            ),
        ))
        self.assertIn("недоступен текущей версии", content_text(rendered))
        self.assertNotIn("обновлена заметка", content_text(rendered))

    def test_unknown_proposal_url_stops_before_all_preflight_effects(self) -> None:
        """Повтор URL с будущим proposal не запускает lease, sync или запись."""
        from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
        from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
        from compadre_assistant.capabilities.obsidian.domain.articles.incoming_messages import ArticleIncomingMessage
        from compadre_assistant.core.domain.users.entities import AppUser

        dto = ObsidianProposalDto(
            id=8, app_user_id=3, article_id=7, analysis_id=9, vault_id=4,
            origin_channel="telegram", origin_chat_id="chat", action="merge",
            status="pending", reasoning="Future action", base_commit_sha="base",
            base_tree_sha="tree", created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        for action, status, article_status in (
            ("merge", "pending", ArticleStatus.NEEDS_OBSIDIAN_REVIEW),
            ("add", "future_status", ArticleStatus.NEEDS_OBSIDIAN_REVIEW),
            ("merge", "applied", ArticleStatus.REVIEWED),
        ):
            with self.subTest(action=action, status=status):
                article = Article(id=7, app_user_id=3, source_url="https://example.com/a",
                                  normalized_url="https://example.com/a", status=article_status)
                urls, service, lease, sync, messages = (Mock() for _ in range(5))
                urls.get_saved_for_url.return_value = article
                proposal = proposal_from_dto(replace(dto, action=action, status=status))
                service.get_active_for_article.return_value = None if status == "applied" else proposal
                service.get_latest_applied.return_value = proposal
                handler = ObsidianMessageHandler(
                    article_url_use_case=urls, obsidian_confirmation_service=service,
                    article_flow_lease_repository=lease, vault_sync_manager=sync,
                    incoming_message_repository=messages,
                )
                result = handler._process_article_url(
                    ArticleIncomingMessage(app_user_id=3, channel="telegram", chat_id="chat",
                                           message_id="1", text=article.source_url), AppUser(id=3),
                )
                self.assertIn("недоступно текущей версии", result.text_override)
                self.assertEqual(lease.mock_calls, [])
                self.assertEqual(sync.mock_calls, [])
                self.assertEqual(messages.mock_calls, [])
                urls.execute.assert_not_called()

    def test_unknown_proposal_presentation_has_no_confirmation_buttons(self) -> None:
        """Нейтральный ответ о неизвестном действии не предлагает его исполнить."""
        from compadre_assistant.capabilities.obsidian.presentation.processing import _present_result
        dto = ObsidianProposalDto(
            id=8, app_user_id=3, article_id=7, analysis_id=9, vault_id=4,
            origin_channel="telegram", origin_chat_id="chat", action="merge",
            status="pending", reasoning="Future action", base_commit_sha="base",
            base_tree_sha="tree", created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        response = _present_result(ObsidianMessageResult(
            type=ObsidianResultType.ARTICLE_REVIEWED_RESULT,
            obsidian_confirmation=ObsidianConfirmationResult(
                ObsidianConfirmationStatus.PENDING, proposal=proposal_from_dto(dto),
            ),
        ))
        self.assertEqual(response.actions, ())

    def test_vault_mapper_and_selection_reject_unknown_before_decision(self) -> None:
        """Неизвестное действие и статус vault не проходят до SQLite CAS."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        dto = VaultActionConfirmationDto(
            id=5, app_user_id=1, origin_channel="telegram", origin_chat_id="chat",
            base_vault_id=2, version=1, status="pending", action="archive",
            expires_at=(now + timedelta(minutes=10)).isoformat(),
            installation_id=None, repository_id=None, owner=None, repository=None,
            branch=None, root_path=None, created_at=now.isoformat(),
        )
        confirmation = vault_confirmation_from_dto(dto)
        unknown_status = vault_confirmation_from_dto(
            replace(dto, action="disconnect", status="future")
        )
        self.assertIs(confirmation.action, VaultConfirmationAction.UNKNOWN)
        self.assertIs(unknown_status.status, VaultConfirmationStatus.UNKNOWN)
        repository = Mock()
        service = GitHubVaultSelectionService(
            installation_repository=Mock(), vault_repository=Mock(),
            confirmation_repository=repository, github_gateway=Mock(), clock=lambda: now,
        )
        for pending in (confirmation, unknown_status):
            repository.get_by_id.return_value = pending
            repository.find_active.return_value = pending
            with self.subTest(action=pending.action, status=pending.status):
                self.assertIsNone(service.get_pending(1, PendingOrigin("telegram", "chat")))
                with self.assertRaises(PendingOperationError):
                    service.decide(
                        1, confirmation_id=5, origin=PendingOrigin("telegram", "chat"),
                        version=1, action=pending.action, approve=False,
                    )
        repository.decide.assert_not_called()

        storage = SQLiteVaultActionConfirmationRepository(Mock())
        for pending in (confirmation, unknown_status):
            with self.assertRaises(PendingOperationError):
                storage.decide(pending, approve=False, now=now)
        storage._connection.execute.assert_not_called()

    def test_identity_mapper_retains_unknown_status(self) -> None:
        """Незнакомое состояние rebind не превращается в pending."""
        confirmation = rebind_confirmation_from_dto(IdentityRebindConfirmationDto(
            id=4, source_app_user_id=1, target_app_user_id=2,
            channel="telegram", external_user_id="11", origin_chat_id="11",
            token_hash="hash", expires_at="2026-01-01T00:10:00Z",
            version=1, status="future_status",
        ))
        self.assertIs(confirmation.status, IdentityRebindStatus.UNKNOWN)

    def test_unknown_proposal_action_cannot_complete_or_change_row(self) -> None:
        """Даже прямой вызов SQL completion оставляет новую строку неизменной."""
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE obsidian_proposals (id INTEGER, app_user_id INTEGER, "
            "version INTEGER, status TEXT, action TEXT, article_id INTEGER, vault_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO obsidian_proposals VALUES (8, 3, 2, 'executing', 'merge', 7, 4)"
        )
        repository = SQLiteObsidianProposalRepository(connection)
        result = repository.complete_applied(
            proposal_id=8, app_user_id=3, version=2, applied_commit_sha=None,
            head_commit_sha=None, tree_sha=None, note=None,
        )
        self.assertIsNone(result)
        self.assertEqual(
            tuple(connection.execute("SELECT status, action, version FROM obsidian_proposals").fetchone()),
            ("executing", "merge", 2),
        )
        connection.close()

    def test_unknown_identity_status_does_not_consume_token_or_rebind(self) -> None:
        """CAS по pending не затрагивает строку будущей схемы и identity."""
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE identity_rebind_confirmations (id INTEGER, version INTEGER, "
            "status TEXT, expires_at TEXT, channel TEXT, external_user_id TEXT, "
            "origin_chat_id TEXT, source_app_user_id INTEGER)"
        )
        connection.execute(
            "CREATE TABLE external_identities (app_user_id INTEGER, channel TEXT, external_user_id TEXT)"
        )
        connection.execute(
            "INSERT INTO identity_rebind_confirmations VALUES "
            "(4, 1, 'future_status', '2027-01-01T00:00:00+00:00', 'telegram', '11', '11', 1)"
        )
        connection.execute("INSERT INTO external_identities VALUES (1, 'telegram', '11')")
        repository = SQLiteIdentityRebindConfirmationRepository(connection)
        identity = IncomingIdentity("telegram", "11", "11")
        self.assertIsNone(repository.decide(
            identity=identity, confirmation_id=4, version=1, approve=True,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        ))
        self.assertEqual(connection.execute(
            "SELECT status, version FROM identity_rebind_confirmations"
        ).fetchone()[:], ("future_status", 1))
        self.assertEqual(connection.execute(
            "SELECT app_user_id FROM external_identities"
        ).fetchone()[0], 1)
        connection.close()
