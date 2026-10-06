"""Тесты подтверждения Obsidian-предложений и optimistic concurrency."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from obs_chat_bot.capabilities.obsidian.application.reviews.confirmation import (
    ObsidianConfirmationStatus,
    ObsidianProposalConfirmationService,
)
from obs_chat_bot.capabilities.obsidian.application.vaults.github_models import (
    GitHubGatewayError,
    GitHubVaultCommitResult,
    GitHubVaultTargetState,
)
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
    ObsidianProposalStatus,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote, VaultSyncLease


class ObsidianProposalConfirmationTests(unittest.TestCase):
    """Проверяет skip, commit, конфликт и безопасный повтор записи."""

    def setUp(self) -> None:
        self.vault = ObsidianVault(
            id=4,
            app_user_id=3,
            installation_id=11,
            repository_id=12,
            owner="owner",
            repository="vault",
            branch="main",
            head_commit_sha="base-commit",
            tree_sha="base-tree",
        )

    def test_skip_finishes_without_github_request(self) -> None:
        repository = ProposalRepositoryFake(_proposal(ObsidianProposalAction.SKIP))
        gateway = GitHubGatewayFake(_state())

        result = _service(repository, self.vault, gateway).confirm(3)

        self.assertEqual(result.status, ObsidianConfirmationStatus.APPLIED)
        self.assertEqual(repository.pending.status, ObsidianProposalStatus.APPLIED)
        self.assertEqual(gateway.inspect_calls, 0)
        self.assertTrue(repository.completed_without_note)

    def test_exact_snapshot_creates_commit_and_completes_locally(self) -> None:
        repository = ProposalRepositoryFake(_proposal(ObsidianProposalAction.ADD))
        gateway = GitHubGatewayFake(_state())
        sync = VaultSyncManagerFake()

        result = _service(repository, self.vault, gateway, sync=sync).confirm(3)

        self.assertEqual(result.status, ObsidianConfirmationStatus.APPLIED)
        self.assertEqual(gateway.commit_calls, 1)
        self.assertEqual(repository.note.path, "Tech/New.md")
        self.assertEqual(repository.note.blob_sha, "new-blob")
        self.assertEqual(sync.calls, [3])

    def test_update_commits_pending_markdown_and_indexes_marker_metadata(self) -> None:
        """Confirmation коммитит финальный pending и индексирует его frontmatter."""
        markdown = (
            "---\ntags: [knowledge-catcher, docker]\n---\n"
            "# Updated\nНовый текст"
        )
        proposal = replace(
            _proposal(ObsidianProposalAction.UPDATE),
            proposed_markdown=markdown,
        )
        repository = ProposalRepositoryFake(proposal)
        gateway = GitHubGatewayFake(
            _state(target_blob_sha="old-blob", target_markdown="# Old")
        )

        result = _service(repository, self.vault, gateway).confirm(3)

        self.assertEqual(result.status, ObsidianConfirmationStatus.APPLIED)
        self.assertEqual(gateway.committed_markdown, markdown)
        self.assertEqual(repository.note.markdown, markdown)
        self.assertEqual(repository.note.tags, ("knowledge-catcher", "docker"))
        self.assertIn("tags:", repository.note.frontmatter)

    def test_embedding_failure_does_not_undo_successful_commit(self) -> None:
        """Post-commit индексация оставляет proposal применённым при сбое API."""
        repository = ProposalRepositoryFake(_proposal(ObsidianProposalAction.ADD))
        gateway = GitHubGatewayFake(_state())
        sync = VaultSyncManagerFake(error=RuntimeError("embedding unavailable"))

        result = _service(repository, self.vault, gateway, sync=sync).confirm(3)

        self.assertEqual(result.status, ObsidianConfirmationStatus.APPLIED)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertEqual(repository.pending.status, ObsidianProposalStatus.APPLIED)
        self.assertEqual(gateway.commit_calls, 1)
        self.assertEqual(sync.calls, [3])

    def test_changed_head_closes_stale_proposal_without_commit(self) -> None:
        repository = ProposalRepositoryFake(_proposal(ObsidianProposalAction.ADD))
        gateway = GitHubGatewayFake(
            GitHubVaultTargetState(
                head_commit_sha="other-commit",
                tree_sha="other-tree",
                target_blob_sha=None,
            )
        )

        result = _service(repository, self.vault, gateway).confirm(3)

        self.assertEqual(result.status, ObsidianConfirmationStatus.CONFLICT)
        self.assertEqual(repository.pending.status, ObsidianProposalStatus.CONFLICT)
        self.assertEqual(gateway.commit_calls, 0)

    def test_network_failure_leaves_proposal_pending_for_retry(self) -> None:
        proposal = _proposal(ObsidianProposalAction.UPDATE)
        repository = ProposalRepositoryFake(proposal)
        gateway = GitHubGatewayFake(
            _state(target_blob_sha="old-blob", target_markdown="# Old"),
            error=GitHubGatewayError("timeout"),
        )

        result = _service(repository, self.vault, gateway).confirm(3)

        self.assertEqual(result.status, ObsidianConfirmationStatus.FAILED)
        self.assertEqual(repository.pending.status, ObsidianProposalStatus.PENDING)
        self.assertEqual(gateway.commit_calls, 1)

    def test_retry_after_lost_response_recognizes_already_written_markdown(self) -> None:
        proposal = _proposal(ObsidianProposalAction.UPDATE)
        repository = ProposalRepositoryFake(proposal)
        gateway = GitHubGatewayFake(
            GitHubVaultTargetState(
                head_commit_sha="new-commit",
                tree_sha="new-tree",
                target_blob_sha="new-blob",
                target_markdown=proposal.proposed_markdown,
            )
        )

        result = _service(repository, self.vault, gateway).confirm(3)

        self.assertEqual(result.status, ObsidianConfirmationStatus.APPLIED)
        self.assertEqual(gateway.commit_calls, 0)
        self.assertEqual(result.proposal.applied_commit_sha, "new-commit")


class ProposalRepositoryFake:
    """Хранит один proposal и результат локальной финализации."""

    def __init__(self, proposal: ObsidianProposal) -> None:
        self.pending = proposal
        self.note: VaultNote | None = None
        self.completed_without_note = False

    def get_pending(self, app_user_id: int) -> ObsidianProposal | None:
        if (
            self.pending.app_user_id == app_user_id
            and self.pending.status is ObsidianProposalStatus.PENDING
        ):
            return self.pending
        return None

    def get_latest_applied_for_article(self, **_kwargs):
        return None

    def mark_cancelled(self, *, proposal_id: int, app_user_id: int):
        self.pending = replace(
            self.pending,
            status=ObsidianProposalStatus.CANCELLED,
        )
        return self.pending

    def mark_conflict(self, *, proposal_id: int, app_user_id: int):
        self.pending = replace(
            self.pending,
            status=ObsidianProposalStatus.CONFLICT,
        )
        return self.pending

    def complete_applied(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        applied_commit_sha: str | None,
        head_commit_sha: str | None,
        tree_sha: str | None,
        note: VaultNote | None,
    ) -> ObsidianProposal:
        self.note = note
        self.completed_without_note = note is None
        from datetime import UTC, datetime

        self.pending = replace(
            self.pending,
            status=ObsidianProposalStatus.APPLIED,
            applied_commit_sha=applied_commit_sha,
            completed_at=datetime.now(UTC),
        )
        return self.pending


class VaultRepositoryFake:
    """Возвращает один активный vault."""

    def __init__(self, vault: ObsidianVault) -> None:
        self.vault = vault

    def get_by_id(self, *, app_user_id: int, vault_id: int):
        if self.vault.app_user_id == app_user_id and self.vault.id == vault_id:
            return self.vault
        return None


class LeaseRepositoryFake:
    """Выдаёт и освобождает один внутрипроцессный lease."""

    def acquire(self, **kwargs):
        return VaultSyncLease(
            app_user_id=kwargs["app_user_id"],
            vault_id=kwargs["vault_id"],
            owner=kwargs["owner"],
            acquired_at=kwargs["now"],
            expires_at=kwargs["expires_at"],
        )

    def release(self, **_kwargs):
        return True


class GitHubGatewayFake:
    """Возвращает заданный remote state и считает write-вызовы."""

    def __init__(self, state: GitHubVaultTargetState, *, error=None) -> None:
        self.state = state
        self.error = error
        self.inspect_calls = 0
        self.commit_calls = 0
        self.committed_markdown: str | None = None

    def inspect_vault_target(self, vault, *, target_path):
        self.inspect_calls += 1
        return self.state

    def commit_vault_markdown(self, vault, **kwargs):
        self.commit_calls += 1
        self.committed_markdown = kwargs["markdown"]
        if self.error is not None:
            raise self.error
        return GitHubVaultCommitResult(
            commit_sha="new-commit",
            tree_sha="new-tree",
            blob_sha="new-blob",
        )


class VaultSyncManagerFake:
    """Имитирует немедленное локальное обновление индексов после commit."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[int] = []

    def sync_if_stale(self, app_user_id: int):
        self.calls.append(app_user_id)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(embedding_update_failed=False)


def _service(
    repository,
    vault,
    gateway,
    *,
    sync: VaultSyncManagerFake | None = None,
) -> ObsidianProposalConfirmationService:
    return ObsidianProposalConfirmationService(
        proposal_repository=repository,
        vault_repository=VaultRepositoryFake(vault),
        lease_repository=LeaseRepositoryFake(),
        github_gateway=gateway,
        vault_sync_manager=sync,
    )


def _proposal(action: ObsidianProposalAction) -> ObsidianProposal:
    kwargs = {}
    if action is ObsidianProposalAction.ADD:
        kwargs = {"target_path": "Tech/New.md", "proposed_markdown": "# New"}
    elif action is ObsidianProposalAction.UPDATE:
        kwargs = {
            "target_path": "Tech/New.md",
            "proposed_markdown": "# Updated",
            "target_blob_sha": "old-blob",
        }
    return ObsidianProposal(
        id=8,
        app_user_id=3,
        article_id=7,
        analysis_id=9,
        vault_id=4,
        action=action,
        reasoning="Тестовое решение.",
        base_commit_sha="base-commit",
        base_tree_sha="base-tree",
        **kwargs,
    )


def _state(
    *,
    target_blob_sha: str | None = None,
    target_markdown: str | None = None,
) -> GitHubVaultTargetState:
    return GitHubVaultTargetState(
        head_commit_sha="base-commit",
        tree_sha="base-tree",
        target_blob_sha=target_blob_sha,
        target_markdown=target_markdown,
    )


if __name__ == "__main__":
    unittest.main()
