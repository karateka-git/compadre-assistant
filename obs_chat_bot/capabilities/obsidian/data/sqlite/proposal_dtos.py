"""Форма хранения предложения Obsidian в SQLite."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObsidianProposalDto:
    """Хранит значения колонок таблицы `obsidian_proposals`."""

    app_user_id: int
    article_id: int
    analysis_id: int
    vault_id: int
    action: str
    reasoning: str
    base_commit_sha: str
    base_tree_sha: str
    status: str
    target_path: str | None = None
    proposed_markdown: str | None = None
    target_blob_sha: str | None = None
    id: int | None = None
    applied_commit_sha: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
