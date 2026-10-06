from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class GitHubDeviceAuthorization:
    """Содержит временный Device Flow challenge для показа пользователю."""

    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int

    def __post_init__(self) -> None:
        if not self.device_code.strip():
            raise ValueError("device_code must not be empty")
        if not self.user_code.strip():
            raise ValueError("user_code must not be empty")
        if not self.verification_uri.strip():
            raise ValueError("verification_uri must not be empty")
        if self.expires_in <= 0:
            raise ValueError("expires_in must be positive")
        if self.interval <= 0:
            raise ValueError("interval must be positive")


@dataclass(frozen=True, slots=True)
class GitHubUserAccessToken:
    """Обертывает временный user access token без раскрытия в `repr`."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("value must not be empty")


@dataclass(frozen=True, slots=True)
class GitHubAuthenticatedAccount:
    """Содержит публичную identity авторизованного GitHub-аккаунта."""

    github_user_id: int
    login: str

    def __post_init__(self) -> None:
        if self.github_user_id <= 0:
            raise ValueError("github_user_id must be positive")
        if not self.login.strip():
            raise ValueError("login must not be empty")


@dataclass(frozen=True, slots=True)
class GitHubInstallationAccessToken:
    """Обертывает краткоживущий installation token и срок его действия."""

    value: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("value must not be empty")


class GitHubDevicePollStatus(StrEnum):
    """Результат одной проверки Device Flow authorization."""

    AUTHORIZED = "authorized"  # Пользователь подтвердил код.
    PENDING = "pending"  # Пользователь ещё не подтвердил код.
    SLOW_DOWN = "slow_down"  # GitHub требует увеличить interval polling.
    EXPIRED = "expired"  # Device code больше не действует.
    DENIED = "denied"  # Пользователь явно отклонил авторизацию.


@dataclass(frozen=True, slots=True)
class GitHubDevicePollResult:
    """Содержит статус Device Flow и token только после авторизации."""

    status: GitHubDevicePollStatus
    access_token: GitHubUserAccessToken | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, GitHubDevicePollStatus):
            raise TypeError("status must be a GitHubDevicePollStatus")
        if self.status is GitHubDevicePollStatus.AUTHORIZED:
            if self.access_token is None:
                raise ValueError("access_token is required for authorized status")
        elif self.access_token is not None:
            raise ValueError("access_token is only allowed for authorized status")


class GitHubConnectionStartStatus(StrEnum):
    """Результат запуска фоновой GitHub-авторизации."""

    STARTED = "started"  # Новый Device Flow запущен.
    ALREADY_PENDING = "already_pending"  # Для пользователя уже ожидается код.
    PREPARING = "preparing"  # GitHub ещё выдаёт первый Device Flow challenge.
    IN_PROGRESS = "in_progress"  # Device Flow уже идёт в другом процессе.


class GitHubConnectionCompletionStatus(StrEnum):
    """Итог фоновой GitHub-авторизации для уведомления пользователя."""

    CONNECTED = "connected"  # Найдена хотя бы одна доступная installation.
    NO_INSTALLATIONS = "no_installations"  # App не установлено в доступный account.
    DENIED = "denied"  # Пользователь отклонил Device Flow.
    EXPIRED = "expired"  # Одноразовый Device Flow code истёк.
    FAILED = "failed"  # Авторизация или сохранение завершились ошибкой.


@dataclass(frozen=True, slots=True)
class GitHubConnectionCompletion:
    """Описывает безопасный итог фонового подключения без token и code."""

    status: GitHubConnectionCompletionStatus
    installation_count: int = 0
    account_login: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, GitHubConnectionCompletionStatus):
            raise TypeError("status must be a GitHubConnectionCompletionStatus")
        if self.installation_count < 0:
            raise ValueError("installation_count must not be negative")
        if (
            self.status is GitHubConnectionCompletionStatus.CONNECTED
            and self.installation_count == 0
        ):
            raise ValueError("connected status requires at least one installation")
        if (
            self.status is not GitHubConnectionCompletionStatus.CONNECTED
            and self.installation_count != 0
        ):
            raise ValueError("installation_count is only allowed for connected status")
        if self.status is GitHubConnectionCompletionStatus.CONNECTED:
            if self.account_login is None or not self.account_login.strip():
                raise ValueError("connected status requires account_login")
        elif self.account_login is not None:
            raise ValueError("account_login is only allowed for connected status")


@dataclass(frozen=True, slots=True)
class GitHubConnectionStartResult:
    """Возвращает пользователю installation URL и Device Flow challenge."""

    status: GitHubConnectionStartStatus
    installation_url: str
    authorization: GitHubDeviceAuthorization | None = None


class GitHubGatewayError(RuntimeError):
    """Ошибка безопасного взаимодействия с GitHub authentication API."""


@dataclass(frozen=True, slots=True)
class GitHubVaultTargetState:
    """Фиксирует актуальные SHA ветки и целевого Markdown перед commit."""

    head_commit_sha: str
    tree_sha: str
    target_blob_sha: str | None
    target_markdown: str | None = None

    def __post_init__(self) -> None:
        if not self.head_commit_sha.strip() or not self.tree_sha.strip():
            raise ValueError("GitHub target state requires commit and tree SHA")
        if self.target_blob_sha is not None and not self.target_blob_sha.strip():
            raise ValueError("target_blob_sha must not be empty")
        if self.target_blob_sha is None and self.target_markdown is not None:
            raise ValueError("target_markdown requires target_blob_sha")


@dataclass(frozen=True, slots=True)
class GitHubVaultCommitResult:
    """Содержит новые SHA после прямого Contents API commit."""

    commit_sha: str
    tree_sha: str
    blob_sha: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.commit_sha, "commit_sha"),
            (self.tree_sha, "tree_sha"),
            (self.blob_sha, "blob_sha"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class GitHubRepositoryInspection:
    """Описывает доступный repository и результат проверки vault path."""

    installation_id: int
    repository_id: int
    owner: str
    repository: str
    default_branch: str
    root_path_is_directory: bool

    def __post_init__(self) -> None:
        if self.installation_id <= 0:
            raise ValueError("installation_id must be positive")
        if self.repository_id <= 0:
            raise ValueError("repository_id must be positive")
        for value, name in (
            (self.owner, "owner"),
            (self.repository, "repository"),
            (self.default_branch, "default_branch"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.root_path_is_directory, bool):
            raise TypeError("root_path_is_directory must be a bool")


class GitHubVaultSnapshotStatus(StrEnum):
    """Описывает результат проверки удалённого состояния vault."""

    NOT_MODIFIED = "not_modified"  # ETag ветки не изменился.
    TREE_UNCHANGED = "tree_unchanged"  # Commit изменился, но дерево vault прежнее.
    CHANGED = "changed"  # Состав или содержимое Markdown-файлов изменились.


@dataclass(frozen=True, slots=True)
class GitHubMarkdownFile:
    """Описывает Markdown blob из полного удалённого manifest.

    `markdown` отсутствует у неизменённого blob, уже сохранённого локально.
    """

    path: str
    blob_sha: str
    markdown: str | None = None

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/") or "\\" in self.path:
            raise ValueError("path must be repository-relative")
        if not self.path.lower().endswith(".md"):
            raise ValueError("path must point to a Markdown file")
        if not self.blob_sha.strip():
            raise ValueError("blob_sha must not be empty")


@dataclass(frozen=True, slots=True)
class GitHubInstructionFile:
    """Описывает обязательный текстовый instruction-файл из GitHub tree.

    `content` отсутствует у неизменённого blob, уже сохранённого локально.
    """

    position: int
    path: str
    blob_sha: str
    content: str | None = None

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("position must not be negative")
        if not self.path or self.path.startswith("/") or "\\" in self.path:
            raise ValueError("path must be repository-relative")
        if not self.blob_sha.strip():
            raise ValueError("blob_sha must not be empty")


@dataclass(frozen=True, slots=True)
class GitHubVaultSnapshot:
    """Содержит снимок ветки, Markdown и обязательных instruction-файлов."""

    status: GitHubVaultSnapshotStatus
    head_commit_sha: str | None = None
    tree_sha: str | None = None
    head_etag: str | None = None
    files: tuple[GitHubMarkdownFile, ...] = ()
    instructions: tuple[GitHubInstructionFile, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, GitHubVaultSnapshotStatus):
            raise TypeError("status must be a GitHubVaultSnapshotStatus")
        if self.status is GitHubVaultSnapshotStatus.CHANGED:
            if not self.head_commit_sha or not self.tree_sha:
                raise ValueError("changed snapshot requires commit and tree SHA")
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("files must not contain duplicate paths")
        instruction_paths = [file.path for file in self.instructions]
        if len(instruction_paths) != len(set(instruction_paths)):
            raise ValueError("instructions must not contain duplicate paths")
        positions = [file.position for file in self.instructions]
        if positions != list(range(len(positions))):
            raise ValueError("instruction positions must be contiguous")
