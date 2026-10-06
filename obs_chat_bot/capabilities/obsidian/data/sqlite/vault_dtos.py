from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitHubInstallationDto:
    """Представляет разрешённую GitHub App installation в SQLite."""

    app_user_id: int
    installation_id: int
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ObsidianVaultDto:
    """Представляет подключение Obsidian vault в форме SQLite."""

    app_user_id: int
    installation_id: int
    repository_id: int
    owner: str
    repository: str
    branch: str
    root_path: str
    id: int | None = None
    head_commit_sha: str | None = None
    tree_sha: str | None = None
    head_etag: str | None = None
    last_checked_at: str | None = None
    last_synced_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class VaultNoteDto:
    """Представляет Markdown-заметку и её metadata в форме SQLite."""

    app_user_id: int
    vault_id: int
    path: str
    blob_sha: str
    markdown: str
    title: str | None
    frontmatter: str | None
    tags: tuple[str, ...]
    wikilinks: tuple[str, ...]
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class VaultInstructionDto:
    """Представляет instruction-файл vault в форме SQLite."""

    app_user_id: int
    vault_id: int
    position: int
    path: str
    blob_sha: str
    content: str
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class VaultSyncLeaseDto:
    """Представляет межпроцессный lease синхронизации в SQLite."""

    app_user_id: int
    vault_id: int
    owner: str
    acquired_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class VaultActionConfirmationDto:
    """Представляет ожидающее действие над vault в форме SQLite."""

    app_user_id: int
    action: str
    expires_at: str
    installation_id: int | None
    repository_id: int | None
    owner: str | None
    repository: str | None
    branch: str | None
    root_path: str | None
    created_at: str | None = None
