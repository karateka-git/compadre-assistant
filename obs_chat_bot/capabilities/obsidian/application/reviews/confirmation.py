"""Подтверждение, optimistic concurrency и применение Obsidian-предложения."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
from uuid import uuid4

from obs_chat_bot.capabilities.obsidian.application.articles.ports import ProcessingErrorRecorder
from obs_chat_bot.capabilities.obsidian.application.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.application.reviews.ports import ObsidianProposalRepository
from obs_chat_bot.capabilities.obsidian.application.search.errors import EmbeddingProviderError
from obs_chat_bot.capabilities.obsidian.application.vaults.github_models import (
    GitHubGatewayError,
    GitHubVaultCommitResult,
)
from obs_chat_bot.capabilities.obsidian.application.vaults.markdown import parse_markdown
from obs_chat_bot.capabilities.obsidian.application.vaults.ports import (
    GitHubVaultWriteGateway,
    ObsidianVaultRepository,
    VaultSyncLeaseRepository,
)
from obs_chat_bot.capabilities.obsidian.application.vaults.vault_sync import VaultSyncManager
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote


LOGGER = logging.getLogger(__name__)


class ObsidianConfirmationStatus(StrEnum):
    """Описывает безопасный итог ответа на pending-предложение."""

    NO_PENDING = "no_pending"  # Подтверждать или отменять нечего.
    PENDING = "pending"  # Ожидается буквальный ответ `да` или `нет`.
    CANCELLED = "cancelled"  # Пользователь отклонил предложение.
    APPLIED = "applied"  # Commit или skip успешно завершён.
    CONFLICT = "conflict"  # GitHub SHA изменились после подготовки.
    IN_PROGRESS = "in_progress"  # Vault занят sync/write другого канала.
    FAILED = "failed"  # Внешняя запись не завершилась; retry разрешён.


@dataclass(frozen=True, slots=True)
class ObsidianConfirmationResult:
    """Возвращает proposal и безопасную причину результата подтверждения."""

    status: ObsidianConfirmationStatus
    proposal: ObsidianProposal | None = None
    error: Exception | None = None


class ObsidianProposalConfirmationService:
    """Применяет только текущее предложение пользователя под SQLite lease."""

    def __init__(
        self,
        *,
        proposal_repository: ObsidianProposalRepository,
        vault_repository: ObsidianVaultRepository,
        lease_repository: VaultSyncLeaseRepository,
        github_gateway: GitHubVaultWriteGateway,
        vault_sync_manager: VaultSyncManager | None = None,
        error_recorder: ProcessingErrorRecorder | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        self._proposal_repository = proposal_repository
        self._vault_repository = vault_repository
        self._lease_repository = lease_repository
        self._github_gateway = github_gateway
        self._vault_sync_manager = vault_sync_manager
        self._error_recorder = error_recorder
        self._lease_duration = lease_duration

    def get_pending(self, app_user_id: int) -> ObsidianProposal | None:
        """Возвращает ожидающее предложение для маршрутизации chat-сообщения."""
        return self._proposal_repository.get_pending(app_user_id)

    def get_latest_applied(
        self,
        *,
        app_user_id: int,
        article_id: int,
    ) -> ObsidianProposal | None:
        """Возвращает сохранённый результат уже завершённой статьи."""
        return self._proposal_repository.get_latest_applied_for_article(
            app_user_id=app_user_id,
            article_id=article_id,
        )

    def cancel(self, app_user_id: int) -> ObsidianConfirmationResult:
        """Отменяет pending без GitHub-запроса и изменения статуса статьи."""
        pending = self._proposal_repository.get_pending(app_user_id)
        if pending is None or pending.id is None:
            return ObsidianConfirmationResult(
                status=ObsidianConfirmationStatus.NO_PENDING
            )
        cancelled = self._proposal_repository.mark_cancelled(
            proposal_id=pending.id,
            app_user_id=app_user_id,
        )
        return ObsidianConfirmationResult(
            status=(
                ObsidianConfirmationStatus.CANCELLED
                if cancelled is not None
                else ObsidianConfirmationStatus.NO_PENDING
            ),
            proposal=cancelled,
        )

    def confirm(self, app_user_id: int) -> ObsidianConfirmationResult:
        """Проверяет snapshot и применяет `add`, `update` или `skip`.

        GitHub ошибки оставляют proposal в `pending`, статью с полным
        `cleaned_text` и локальный source SHA без изменений, поэтому явное `да`
        можно безопасно повторить.
        """
        pending = self._proposal_repository.get_pending(app_user_id)
        if pending is None or pending.id is None:
            return ObsidianConfirmationResult(
                status=ObsidianConfirmationStatus.NO_PENDING
            )
        if pending.action is ObsidianProposalAction.SKIP:
            applied = self._proposal_repository.complete_applied(
                proposal_id=pending.id,
                app_user_id=app_user_id,
                applied_commit_sha=None,
                head_commit_sha=None,
                tree_sha=None,
                note=None,
            )
            if applied is None:
                raise RuntimeError(
                    "Pending skip proposal changed before local completion"
                )
            return ObsidianConfirmationResult(
                status=ObsidianConfirmationStatus.APPLIED,
                proposal=applied,
            )

        vault = self._vault_repository.get_by_id(
            app_user_id=app_user_id,
            vault_id=pending.vault_id,
        )
        if vault is None or vault.id is None:
            return self._conflict(pending)
        now = datetime.now(UTC)
        owner = f"obsidian-write-{uuid4().hex}"
        lease = self._lease_repository.acquire(
            app_user_id=app_user_id,
            vault_id=vault.id,
            owner=owner,
            now=now,
            expires_at=now + self._lease_duration,
        )
        if lease is None:
            return ObsidianConfirmationResult(
                status=ObsidianConfirmationStatus.IN_PROGRESS,
                proposal=pending,
            )
        try:
            result = self._commit_pending(pending, vault)
        finally:
            self._lease_repository.release(
                app_user_id=app_user_id,
                vault_id=vault.id,
                owner=owner,
            )
        if result.status is not ObsidianConfirmationStatus.APPLIED:
            return result
        return self._refresh_applied_indexes(result)

    def _refresh_applied_indexes(
        self,
        result: ObsidianConfirmationResult,
    ) -> ObsidianConfirmationResult:
        """Сразу достраивает локальные индексы, не меняя итог GitHub commit.

        Ошибка внешнего embedding provider не превращает уже записанную
        заметку в failed proposal. Удалённый embedding marker оставляет
        semantic search в безопасном FTS fallback до следующего retry.
        """
        if self._vault_sync_manager is None or result.proposal is None:
            return result
        try:
            sync_result = self._vault_sync_manager.sync_if_stale(
                result.proposal.app_user_id
            )
            if sync_result.embedding_update_failed:
                return ObsidianConfirmationResult(
                    status=ObsidianConfirmationStatus.APPLIED,
                    proposal=result.proposal,
                    error=EmbeddingProviderError(
                        "Embedding index update failed after successful commit"
                    ),
                )
            return result
        except Exception as error:
            LOGGER.exception(
                "Obsidian post-commit index update failed: proposal_id=%s "
                "app_user_id=%s article_id=%s error_type=%s",
                result.proposal.id,
                result.proposal.app_user_id,
                result.proposal.article_id,
                type(error).__name__,
            )
            if self._error_recorder is not None:
                self._error_recorder.record(
                    article_id=result.proposal.article_id,
                    app_user_id=result.proposal.app_user_id,
                    incoming_message_id=None,
                    stage=ProcessingStage.OBSIDIAN_INDEX,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            return ObsidianConfirmationResult(
                status=ObsidianConfirmationStatus.APPLIED,
                proposal=result.proposal,
                error=error,
            )

    def _commit_pending(
        self,
        pending: ObsidianProposal,
        vault: ObsidianVault,
    ) -> ObsidianConfirmationResult:
        """Выполняет сетевую часть подтверждения внутри захваченного lease."""
        try:
            state = self._github_gateway.inspect_vault_target(
                vault,
                target_path=pending.target_path or "",
            )
            proposed_markdown = pending.proposed_markdown or ""
            exact_snapshot = (
                state.head_commit_sha == pending.base_commit_sha
                and state.tree_sha == pending.base_tree_sha
                and state.target_blob_sha == pending.target_blob_sha
            )
            already_applied = (
                state.target_blob_sha is not None
                and state.target_markdown == proposed_markdown
            )
            if not exact_snapshot and not already_applied:
                return self._conflict(pending)
            if already_applied:
                committed = GitHubVaultCommitResult(
                    commit_sha=state.head_commit_sha,
                    tree_sha=state.tree_sha,
                    blob_sha=state.target_blob_sha or "",
                )
            else:
                committed = self._github_gateway.commit_vault_markdown(
                    vault,
                    target_path=pending.target_path or "",
                    markdown=proposed_markdown,
                    expected_blob_sha=pending.target_blob_sha,
                )
            metadata = parse_markdown(
                pending.target_path or "",
                proposed_markdown,
            )
            note = VaultNote(
                app_user_id=pending.app_user_id,
                vault_id=pending.vault_id,
                path=pending.target_path or "",
                blob_sha=committed.blob_sha,
                markdown=proposed_markdown,
                title=metadata.title,
                frontmatter=metadata.frontmatter,
                tags=metadata.tags,
                wikilinks=metadata.wikilinks,
            )
            applied = self._proposal_repository.complete_applied(
                proposal_id=pending.id or 0,
                app_user_id=pending.app_user_id,
                applied_commit_sha=committed.commit_sha,
                head_commit_sha=committed.commit_sha,
                tree_sha=committed.tree_sha,
                note=note,
            )
            if applied is None:
                raise RuntimeError(
                    "Pending proposal changed before local completion"
                )
            LOGGER.info(
                "Obsidian proposal applied: proposal_id=%s app_user_id=%s "
                "article_id=%s action=%s commit_sha=%s",
                pending.id,
                pending.app_user_id,
                pending.article_id,
                pending.action.value,
                committed.commit_sha,
            )
            return ObsidianConfirmationResult(
                status=ObsidianConfirmationStatus.APPLIED,
                proposal=applied,
            )
        except (GitHubGatewayError, OSError) as error:
            LOGGER.exception(
                "Obsidian proposal commit failed: proposal_id=%s app_user_id=%s "
                "article_id=%s error_type=%s",
                pending.id,
                pending.app_user_id,
                pending.article_id,
                type(error).__name__,
            )
            self._record_error(pending, error)
            return ObsidianConfirmationResult(
                status=ObsidianConfirmationStatus.FAILED,
                proposal=pending,
                error=error,
            )

    def _conflict(self, pending: ObsidianProposal) -> ObsidianConfirmationResult:
        """Закрывает stale proposal, не перезаписывая удалённую заметку."""
        conflicted = self._proposal_repository.mark_conflict(
            proposal_id=pending.id or 0,
            app_user_id=pending.app_user_id,
        )
        return ObsidianConfirmationResult(
            status=ObsidianConfirmationStatus.CONFLICT,
            proposal=conflicted or pending,
        )

    def _record_error(
        self,
        pending: ObsidianProposal,
        error: Exception,
    ) -> None:
        if self._error_recorder is None:
            return
        self._error_recorder.record(
            article_id=pending.article_id,
            app_user_id=pending.app_user_id,
            incoming_message_id=None,
            stage=ProcessingStage.OBSIDIAN_WRITE,
            error_type=type(error).__name__,
            error_message=str(error),
        )
