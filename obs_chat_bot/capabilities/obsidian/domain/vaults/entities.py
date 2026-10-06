from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class VaultConfirmationAction(StrEnum):
    """Действие, ожидающее явного подтверждения пользователя."""

    REPLACE = "replace"  # Заменить активный vault предложенным подключением.
    DISCONNECT = "disconnect"  # Удалить активный vault и локальный каталог.


@dataclass(frozen=True, slots=True)
class GitHubInstallation:
    """Разрешает пользователю работать с конкретной установкой GitHub App."""

    app_user_id: int
    installation_id: int
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.installation_id <= 0:
            raise ValueError("installation_id must be positive")


@dataclass(frozen=True, slots=True)
class GitHubAccount:
    """Описывает один GitHub-аккаунт, подключённый к пользователю."""

    app_user_id: int
    github_user_id: int
    login: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.github_user_id <= 0:
            raise ValueError("github_user_id must be positive")
        _require_text(self.login, "login")


@dataclass(frozen=True, slots=True)
class ObsidianVault:
    """Описывает единственный активный GitHub vault пользователя."""

    app_user_id: int
    installation_id: int
    repository_id: int
    owner: str
    repository: str
    branch: str
    root_path: str = ""
    id: int | None = None
    head_commit_sha: str | None = None
    tree_sha: str | None = None
    head_etag: str | None = None
    last_checked_at: datetime | None = None
    last_synced_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.installation_id <= 0:
            raise ValueError("installation_id must be positive")
        if self.repository_id <= 0:
            raise ValueError("repository_id must be positive")
        _require_text(self.owner, "owner")
        _require_text(self.repository, "repository")
        _require_text(self.branch, "branch")
        _validate_repository_path(self.root_path, allow_root=True)
        _validate_optional_text(self.head_commit_sha, "head_commit_sha")
        _validate_optional_text(self.tree_sha, "tree_sha")
        _validate_optional_text(self.head_etag, "head_etag")
        if self.id is not None and self.id <= 0:
            raise ValueError("id must be positive")


@dataclass(frozen=True, slots=True)
class VaultNote:
    """Представляет Markdown-заметку из локальной копии Obsidian vault."""

    app_user_id: int
    vault_id: int
    path: str
    blob_sha: str
    markdown: str
    title: str | None = None
    frontmatter: str | None = None
    tags: tuple[str, ...] = ()
    wikilinks: tuple[str, ...] = ()
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.vault_id <= 0:
            raise ValueError("vault_id must be positive")
        _validate_repository_path(self.path, allow_root=False)
        if not self.path.lower().endswith(".md"):
            raise ValueError("path must point to a Markdown file")
        _require_text(self.blob_sha, "blob_sha")
        _validate_optional_text(self.title, "title")
        _validate_values(self.tags, "tags")
        _validate_values(self.wikilinks, "wikilinks")
        if self.id is not None and self.id <= 0:
            raise ValueError("id must be positive")


@dataclass(frozen=True, slots=True)
class VaultInstruction:
    """Представляет обязательный текстовый файл с правилами конкретного vault."""

    app_user_id: int
    vault_id: int
    position: int
    path: str
    blob_sha: str
    content: str
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if self.position < 0:
            raise ValueError("position must not be negative")
        _validate_repository_path(self.path, allow_root=False)
        _require_text(self.blob_sha, "blob_sha")
        if self.id is not None and self.id <= 0:
            raise ValueError("id must be positive")


@dataclass(frozen=True, slots=True)
class VaultSyncLease:
    """Представляет временное право процесса синхронизировать один vault."""

    app_user_id: int
    vault_id: int
    owner: str
    expires_at: datetime
    acquired_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if self.vault_id <= 0:
            raise ValueError("vault_id must be positive")
        _require_text(self.owner, "owner")
        if self.acquired_at is not None and self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must be later than acquired_at")


@dataclass(frozen=True, slots=True)
class VaultActionConfirmation:
    """Хранит ожидающее подтверждение замены или отключения vault."""

    app_user_id: int
    action: VaultConfirmationAction
    expires_at: datetime
    replacement: ObsidianVault | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if not isinstance(self.action, VaultConfirmationAction):
            raise TypeError("action must be a VaultConfirmationAction")
        if self.action is VaultConfirmationAction.REPLACE:
            if self.replacement is None:
                raise ValueError("replacement is required for replace action")
            if self.replacement.app_user_id != self.app_user_id:
                raise ValueError("replacement must belong to app_user_id")
        elif self.replacement is not None:
            raise ValueError("replacement is only allowed for replace action")


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_optional_text(value: str | None, field_name: str) -> None:
    if value is not None:
        _require_text(value, field_name)


def _validate_values(values: tuple[str, ...], field_name: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} must not contain empty values")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")


def _validate_repository_path(path: str, *, allow_root: bool) -> None:
    if not path:
        if allow_root:
            return
        raise ValueError("path must not be empty")
    if path.startswith("/") or "\\" in path:
        raise ValueError("path must be repository-relative and use forward slashes")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("path contains an invalid segment")
