"""Типизированные планы и предложения изменения Obsidian vault."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ObsidianProposalAction(StrEnum):
    """Действие, рекомендованное после сопоставления статьи с vault."""

    ADD = "add"  # Создать новую Markdown-заметку.
    UPDATE = "update"  # Заменить содержимое существующей Markdown-заметки.
    SKIP = "skip"  # Не переносить материал статьи в vault.


class ObsidianProposalStatus(StrEnum):
    """Описывает жизненный цикл сохранённого предложения."""

    PENDING = "pending"  # Ожидает явного ответа пользователя.
    CANCELLED = "cancelled"  # Пользователь ответил `нет`.
    CONFLICT = "conflict"  # Исходный GitHub snapshot изменился.
    APPLIED = "applied"  # Изменение или подтверждённый skip завершены.


@dataclass(frozen=True, slots=True)
class ObsidianReviewPlan:
    """Фиксирует выбранное действие и путь до чтения соседних заметок."""

    action: ObsidianProposalAction
    reasoning: str
    target_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ObsidianProposalAction):
            raise TypeError("action must be an ObsidianProposalAction")
        if not self.reasoning.strip():
            raise ValueError("reasoning must not be empty")
        if self.action is ObsidianProposalAction.SKIP:
            if self.target_path is not None:
                raise ValueError("skip plan must not have target_path")
            return
        _validate_markdown_path(self.target_path)


@dataclass(frozen=True, slots=True)
class ObsidianProposal:
    """Описывает полное предлагаемое изменение на конкретном снимке vault."""

    app_user_id: int
    article_id: int
    analysis_id: int
    vault_id: int
    action: ObsidianProposalAction
    reasoning: str
    base_commit_sha: str
    base_tree_sha: str
    target_path: str | None = None
    proposed_markdown: str | None = None
    target_blob_sha: str | None = None
    id: int | None = None
    status: ObsidianProposalStatus = ObsidianProposalStatus.PENDING
    applied_commit_sha: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if min(
            self.app_user_id,
            self.article_id,
            self.analysis_id,
            self.vault_id,
        ) <= 0:
            raise ValueError("proposal IDs must be positive")
        if not isinstance(self.action, ObsidianProposalAction):
            raise TypeError("action must be an ObsidianProposalAction")
        if not isinstance(self.status, ObsidianProposalStatus):
            raise TypeError("status must be an ObsidianProposalStatus")
        if self.id is not None and self.id <= 0:
            raise ValueError("id must be positive")
        if not self.reasoning.strip():
            raise ValueError("reasoning must not be empty")
        if not self.base_commit_sha.strip() or not self.base_tree_sha.strip():
            raise ValueError("proposal source SHAs must not be empty")
        if self.action is ObsidianProposalAction.SKIP:
            if any(
                value is not None
                for value in (
                    self.target_path,
                    self.proposed_markdown,
                    self.target_blob_sha,
                )
            ):
                raise ValueError("skip proposal must not contain a vault change")
            if self.applied_commit_sha is not None:
                raise ValueError("skip proposal must not have applied_commit_sha")
        else:
            _validate_markdown_path(self.target_path)
            if self.proposed_markdown is None or not self.proposed_markdown.strip():
                raise ValueError("add/update proposal must contain proposed_markdown")
            if self.action is ObsidianProposalAction.ADD:
                if self.target_blob_sha is not None:
                    raise ValueError("add proposal must not have target_blob_sha")
            elif self.target_blob_sha is None or not self.target_blob_sha.strip():
                raise ValueError("update proposal must contain target_blob_sha")
        if self.status is ObsidianProposalStatus.APPLIED:
            if self.completed_at is None:
                raise ValueError("applied proposal must contain completed_at")
            if (
                self.action is not ObsidianProposalAction.SKIP
                and (
                    self.applied_commit_sha is None
                    or not self.applied_commit_sha.strip()
                )
            ):
                raise ValueError("applied add/update must contain commit SHA")
        elif self.applied_commit_sha is not None:
            raise ValueError("only applied proposal may contain commit SHA")


def _validate_markdown_path(path: str | None) -> None:
    """Проверяет безопасный repository-relative путь Markdown-заметки."""
    if path is None or not path.strip():
        raise ValueError("add/update plan must contain target_path")
    if path.startswith("/") or "\\" in path or not path.lower().endswith(".md"):
        raise ValueError("target_path must be a repository-relative Markdown path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("target_path contains an invalid segment")
