"""Подтверждение, optimistic concurrency и применение Obsidian-предложения."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Callable
from contextlib import nullcontext

from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
from uuid import uuid4

from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from compadre_assistant.capabilities.obsidian.domain.articles.ports import ProcessingErrorRecorder
from compadre_assistant.capabilities.obsidian.domain.articles.stages import ProcessingStage
from compadre_assistant.capabilities.obsidian.domain.reviews.ports import ObsidianProposalRepository
from compadre_assistant.capabilities.obsidian.domain.search.errors import EmbeddingProviderError
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import (
    GitHubGatewayError,
    GitHubVaultCommitResult,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.markdown import parse_markdown
from compadre_assistant.capabilities.obsidian.domain.vaults.lease_scope import (
    VaultLeaseLostError, VaultLeaseScope, active_vault_lease,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.ports import (
    GitHubVaultWriteGateway,
    ObsidianVaultRepository,
    VaultSyncLeaseRepository,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import VaultSyncManager
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
    ObsidianProposalStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote


LOGGER = logging.getLogger(__name__)


class ObsidianConfirmationStatus(StrEnum):
    """Описывает безопасный итог ответа на pending-предложение."""

    FOREIGN_ORIGIN = "foreign_origin"  # Содержимое чужого чата не возвращается.
    STALE = "stale"  # Версия действия уже изменилась; нужен актуальный ответ.
    NO_PENDING = "no_pending"  # Подтверждать или отменять нечего.
    PENDING = "pending"  # Ожидается адресное решение либо однозначный локальный ответ.
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
    """Применяет конкретное адресное предложение пользователя под SQLite lease."""

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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._proposal_repository = proposal_repository
        self._vault_repository = vault_repository
        self._lease_repository = lease_repository
        self._github_gateway = github_gateway
        self._vault_sync_manager = vault_sync_manager
        self._error_recorder = error_recorder
        self._lease_duration = lease_duration
        self._clock = clock

    def list_pending(self, app_user_id: int, origin: PendingOrigin) -> tuple[ObsidianProposal, ...]:
        """Возвращает актуальные ожидания только этого исходного чата."""
        return self._proposal_repository.list_pending(app_user_id, origin)

    def get_active_for_article(
        self, *, app_user_id: int, article_id: int
    ) -> ObsidianProposal | None:
        """Находит активный цикл статьи; presentation проверяет исходный адрес."""
        return self._proposal_repository.get_active_for_article(
            app_user_id=app_user_id, article_id=article_id
        )

    def _validate(
        self, app_user_id: int, proposal_id: int, origin: PendingOrigin, version: int
    ) -> ObsidianProposal | ObsidianConfirmationResult:
        pending = self._proposal_repository.get_by_id(proposal_id, app_user_id)
        if pending is None:
            return ObsidianConfirmationResult(ObsidianConfirmationStatus.NO_PENDING)
        if pending.origin != origin:
            return ObsidianConfirmationResult(ObsidianConfirmationStatus.FOREIGN_ORIGIN)
        if pending.version != version:
            return ObsidianConfirmationResult(ObsidianConfirmationStatus.STALE)
        if pending.status not in {ObsidianProposalStatus.PENDING, ObsidianProposalStatus.EXECUTING}:
            return ObsidianConfirmationResult(ObsidianConfirmationStatus.NO_PENDING)
        if (
            pending.status is ObsidianProposalStatus.EXECUTING
            and pending.execution_expires_at is not None
            and pending.execution_expires_at > datetime.now(UTC)
        ):
            return ObsidianConfirmationResult(
                ObsidianConfirmationStatus.IN_PROGRESS, proposal=pending
            )
        return pending

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

    def cancel(
        self, app_user_id: int, *, proposal_id: int, origin: PendingOrigin, version: int
    ) -> ObsidianConfirmationResult:
        """Отменяет адресное pending атомарной проверкой статуса, адреса и версии."""
        pending = self._validate(app_user_id, proposal_id, origin, version)
        if isinstance(pending, ObsidianConfirmationResult):
            return pending
        cancelled = self._proposal_repository.mark_cancelled(
            proposal_id=proposal_id,
            app_user_id=app_user_id,
            origin=origin,
            version=version,
        )
        return ObsidianConfirmationResult(
            (
                ObsidianConfirmationStatus.CANCELLED
                if cancelled
                else ObsidianConfirmationStatus.IN_PROGRESS
            ),
            proposal=cancelled,
        )

    def confirm(
        self, app_user_id: int, *, proposal_id: int, origin: PendingOrigin, version: int
    ) -> ObsidianConfirmationResult:
        """Захватывает конкретное действие и выполняет SHA preflight под общим lease.

        Args:
            app_user_id: Внутренний владелец операции.
            proposal_id: Устойчивый ID proposal из адресного действия.
            origin: Чат текущего события; обязан совпадать с исходным.
            version: Версия, показанная пользователю при выборе решения.

        Returns:
            Итог без раскрытия proposal чужому исходному чату.
        """
        pending = self._validate(app_user_id, proposal_id, origin, version)
        if isinstance(pending, ObsidianConfirmationResult):
            return pending
        now = self._clock()
        owner = f"obsidian-write-{uuid4().hex}"
        vault = self._vault_repository.get_by_id(app_user_id=app_user_id, vault_id=pending.vault_id)
        lease = None
        if pending.action is not ObsidianProposalAction.SKIP and vault is not None:
            lease = self._lease_repository.acquire(
                app_user_id=app_user_id,
                vault_id=pending.vault_id,
                owner=owner,
                now=now,
                expires_at=now + self._lease_duration,
            )
            if lease is None:
                return ObsidianConfirmationResult(
                    ObsidianConfirmationStatus.IN_PROGRESS, proposal=pending
                )
        claimed = None
        try:
            scope = (
                active_vault_lease(VaultLeaseScope(
                    app_user_id, pending.vault_id, owner, self._clock, self._lease_duration,
                )) if lease is not None else nullcontext()
            )
            with scope:
                claimed = self._proposal_repository.claim(
                    proposal_id=proposal_id,
                    app_user_id=app_user_id,
                    origin=origin,
                    version=version,
                    now=now,
                    expires_at=now + self._lease_duration,
                )
                if claimed is None:
                    return ObsidianConfirmationResult(ObsidianConfirmationStatus.STALE)
                if pending.action is ObsidianProposalAction.SKIP:
                    applied = self._proposal_repository.complete_applied(
                        proposal_id=proposal_id,
                        app_user_id=app_user_id,
                        version=claimed.version,
                        applied_commit_sha=None,
                        head_commit_sha=None,
                        tree_sha=None,
                        note=None,
                    )
                    if applied is None:
                        raise RuntimeError("Claimed skip changed before completion")
                    return ObsidianConfirmationResult(
                        ObsidianConfirmationStatus.APPLIED, proposal=applied
                    )
                if vault is None or vault.id is None:
                    return self._conflict(claimed)
                result = self._commit_pending(claimed, vault, owner=owner)
        except VaultLeaseLostError as error:
            result = ObsidianConfirmationResult(
                ObsidianConfirmationStatus.IN_PROGRESS, proposal=claimed, error=error,
            )
        finally:
            restored = None
            if claimed is not None:
                try:
                    release_scope = (
                        active_vault_lease(VaultLeaseScope(
                            app_user_id, pending.vault_id, owner, self._clock,
                            self._lease_duration,
                        )) if lease is not None else nullcontext()
                    )
                    with release_scope:
                        restored = self._proposal_repository.release_claim(claimed)
                except VaultLeaseLostError:
                    # Истёкший claim восстановит новый worker после SHA preflight.
                    pass
            if lease is not None:
                self._lease_repository.release(
                    app_user_id=app_user_id, vault_id=pending.vault_id, owner=owner
                )
        if restored is not None:
            result = replace(result, proposal=restored)
        return (
            self._refresh_applied_indexes(result)
            if result.status is ObsidianConfirmationStatus.APPLIED
            else result
        )

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
            sync_result = self._vault_sync_manager.sync_if_stale(result.proposal.app_user_id)
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
        *,
        owner: str,
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
                state.target_blob_sha is not None and state.target_markdown == proposed_markdown
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
                committed = self._lease_repository.run_exclusive(
                    app_user_id=pending.app_user_id,
                    vault_id=pending.vault_id,
                    owner=owner,
                    clock=self._clock,
                    duration=self._lease_duration,
                    operation=lambda: self._github_gateway.commit_vault_markdown(
                        vault,
                        target_path=pending.target_path or "",
                        markdown=proposed_markdown,
                        expected_blob_sha=pending.target_blob_sha,
                    ),
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
                version=pending.version,
                applied_commit_sha=committed.commit_sha,
                head_commit_sha=committed.commit_sha,
                tree_sha=committed.tree_sha,
                note=note,
            )
            if applied is None:
                raise RuntimeError("Pending proposal changed before local completion")
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
            version=pending.version,
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
