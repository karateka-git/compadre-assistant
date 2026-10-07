from __future__ import annotations

from dataclasses import dataclass

from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import time
from typing import Callable, Protocol
from urllib.parse import unquote, urlsplit

from obs_chat_bot.capabilities.obsidian.domain.pending import PendingOrigin, PendingOperationError
from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import (
    GitHubInstallationRepository,
    GitHubRepositoryGateway,
    ObsidianVaultRepository,
    VaultActionConfirmationRepository,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import (
    ObsidianVault,
    VaultActionConfirmation,
    VaultConfirmationAction,
)


DEFAULT_CONFIRMATION_TTL = timedelta(minutes=10)
LOGGER = logging.getLogger(__name__)


class VaultSelectionStatus(StrEnum):
    """Результат выбора GitHub repository в качестве Obsidian vault."""

    SELECTED = "selected"  # Первый vault сохранён без подтверждения.
    ALREADY_SELECTED = "already_selected"  # Повторно выбран тот же vault.
    REPLACEMENT_CONFIRMATION_REQUIRED = (
        "replacement_confirmation_required"  # Требуется заменить активный vault.
    )
    REPLACED = "replaced"  # Ожидающая замена подтверждена.
    CANCELLED = "cancelled"  # Ожидающая замена отменена.


class VaultDisconnectStatus(StrEnum):
    """Результат запроса или подтверждения отключения vault."""

    NOT_CONNECTED = "not_connected"  # Активный vault отсутствует.
    CONFIRMATION_REQUIRED = "confirmation_required"  # Ожидается ответ да/нет.
    DISCONNECTED = "disconnected"  # Vault и его локальные данные удалены.
    CANCELLED = "cancelled"  # Пользователь сохранил текущее подключение.


@dataclass(frozen=True, slots=True)
class VaultSelectionResult:
    """Содержит статус выбора и соответствующий активный или предложенный vault."""

    status: VaultSelectionStatus
    vault: ObsidianVault
    confirmation: VaultActionConfirmation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, VaultSelectionStatus):
            raise TypeError("status must be a VaultSelectionStatus")
        if not isinstance(self.vault, ObsidianVault):
            raise TypeError("vault must be an ObsidianVault")


@dataclass(frozen=True, slots=True)
class VaultDisconnectResult:
    """Содержит итог управления отключением и прежний или активный vault."""

    status: VaultDisconnectStatus
    vault: ObsidianVault | None = None
    confirmation: VaultActionConfirmation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, VaultDisconnectStatus):
            raise TypeError("status must be a VaultDisconnectStatus")
        requires_vault = self.status is not VaultDisconnectStatus.NOT_CONNECTED
        if requires_vault and self.vault is None:
            raise ValueError("vault is required for this disconnect status")
        if not requires_vault and self.vault is not None:
            raise ValueError("vault is not allowed for not connected status")


class VaultSelectionError(RuntimeError):
    """Базовая ожидаемая ошибка выбора Obsidian vault."""


class GitHubAccountNotConnectedError(VaultSelectionError):
    """GitHub App не имеет сохранённых installations пользователя."""


class GitHubRepositoryNotAccessibleError(VaultSelectionError):
    """Repository отсутствует или не разрешён подключённой GitHub App."""


class GitHubVaultPathNotFoundError(VaultSelectionError):
    """Указанный vault path отсутствует или не является каталогом."""


class VaultSelectionManager(Protocol):
    """Описывает выбор vault и подтверждение замены из общего chat-flow."""

    def get_selected(self, app_user_id: int) -> ObsidianVault | None:
        """Возвращает активный vault пользователя или `None`."""

    def select(
        self,
        *,
        app_user_id: int,
        origin: PendingOrigin,
        repository_url: str,
        root_path: str = "",
    ) -> VaultSelectionResult:
        """Проверяет GitHub repository и выбирает его как vault."""

    def get_pending(self, app_user_id: int, origin: PendingOrigin) -> VaultActionConfirmation | None:
        """Показывает только активное ожидание исходного чата."""

    def request_disconnect(self, app_user_id: int, origin: PendingOrigin) -> VaultDisconnectResult:
        """Создаёт адресное ожидание отключения конфигурации."""

    def decide(
        self,
        app_user_id: int,
        *,
        confirmation_id: int,
        origin: PendingOrigin,
        version: int,
        action: VaultConfirmationAction,
        approve: bool
    ) -> VaultSelectionResult | VaultDisconnectResult:
        """Исполняет конкретное актуальное решение над конфигурацией vault."""


class GitHubVaultSelectionService(VaultSelectionManager):
    """Проверяет доступ GitHub App и управляет единственным vault пользователя."""

    def __init__(
        self,
        *,
        installation_repository: GitHubInstallationRepository,
        vault_repository: ObsidianVaultRepository,
        confirmation_repository: VaultActionConfirmationRepository,
        github_gateway: GitHubRepositoryGateway,
        clock: Callable[[], datetime] | None = None,
        confirmation_ttl: timedelta = DEFAULT_CONFIRMATION_TTL,
    ) -> None:
        if confirmation_ttl <= timedelta(0):
            raise ValueError("confirmation_ttl must be positive")
        self._installation_repository = installation_repository
        self._vault_repository = vault_repository
        self._confirmation_repository = confirmation_repository
        self._github_gateway = github_gateway
        self._clock = clock or (lambda: datetime.now(UTC))
        self._confirmation_ttl = confirmation_ttl

    def get_selected(self, app_user_id: int) -> ObsidianVault | None:
        """Возвращает активный vault пользователя или `None`."""
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        return self._vault_repository.get_for_user(app_user_id)

    def select(
        self,
        *,
        app_user_id: int,
        origin: PendingOrigin,
        repository_url: str,
        root_path: str = "",
    ) -> VaultSelectionResult:
        """Проверяет repository и сохраняет либо предлагает активный vault.

        Args:
            app_user_id: Внутренний ID пользователя приложения.
            origin: Исходный чат будущего ожидания замены.
            repository_url: Полный HTTPS URL GitHub repository.
            root_path: Путь к корню vault внутри repository или пустая строка.

        Returns:
            Результат первого выбора, повтора или запроса подтверждения.

        Raises:
            ValueError: URL или repository path имеют небезопасный формат.
            GitHubAccountNotConnectedError: У пользователя нет installations.
            GitHubRepositoryNotAccessibleError: У App нет права записи repository.
            GitHubVaultPathNotFoundError: Vault path не является каталогом.
            VaultResourceBusyError: Другое ожидание резервирует конфигурацию vault.
            PendingOperationError: Конфигурация изменилась во время проверки GitHub.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        owner, repository = parse_github_repository_url(repository_url)
        normalized_root = normalize_vault_root_path(root_path)
        installations = self._installation_repository.list_for_user(app_user_id)
        if not installations:
            raise GitHubAccountNotConnectedError("GitHub account has no available installations")

        inspection_started_at = time.monotonic()
        LOGGER.info(
            "GitHub repository inspection started: app_user_id=%s "
            "repository=%s/%s installation_count=%s",
            app_user_id,
            owner,
            repository,
            len(installations),
        )
        inspection = None
        try:
            for installation in installations:
                inspection = self._github_gateway.inspect_repository(
                    installation_id=installation.installation_id,
                    owner=owner,
                    repository=repository,
                    root_path=normalized_root,
                )
                if inspection is not None:
                    break
        except Exception:
            LOGGER.exception(
                "GitHub repository inspection failed: app_user_id=%s "
                "repository=%s/%s duration_seconds=%.3f",
                app_user_id,
                owner,
                repository,
                time.monotonic() - inspection_started_at,
            )
            raise
        LOGGER.info(
            "GitHub repository inspection completed: app_user_id=%s "
            "repository=%s/%s accessible=%s duration_seconds=%.3f",
            app_user_id,
            owner,
            repository,
            inspection is not None,
            time.monotonic() - inspection_started_at,
        )
        if inspection is None:
            raise GitHubRepositoryNotAccessibleError("GitHub repository is not accessible")
        if not inspection.root_path_is_directory:
            raise GitHubVaultPathNotFoundError("GitHub vault path is not a directory")

        candidate = ObsidianVault(
            app_user_id=app_user_id,
            installation_id=inspection.installation_id,
            repository_id=inspection.repository_id,
            owner=inspection.owner,
            repository=inspection.repository,
            branch=inspection.default_branch,
            root_path=normalized_root,
        )
        current = self._vault_repository.get_for_user(app_user_id)
        if current is None:
            saved = self._vault_repository.create_if_absent(candidate)
            if saved is not None:
                return VaultSelectionResult(VaultSelectionStatus.SELECTED, saved)
            # Другой adapter мог выбрать vault между чтением и вставкой.
            current = self._vault_repository.get_for_user(app_user_id)
            if current is None:
                raise RuntimeError("Concurrent vault selection could not be read")
        if _same_vault(current, candidate):
            return VaultSelectionResult(VaultSelectionStatus.ALREADY_SELECTED, current)

        now = self._clock()
        confirmation = self._confirmation_repository.save(
            VaultActionConfirmation(
                app_user_id=app_user_id,
                action=VaultConfirmationAction.REPLACE,
                origin=origin,
                base_vault_id=current.id or 0,
                replacement=candidate,
                expires_at=now + self._confirmation_ttl,
                created_at=now,
            ),
            now=now,
        )
        return VaultSelectionResult(
            VaultSelectionStatus.REPLACEMENT_CONFIRMATION_REQUIRED,
            candidate,
            confirmation,
        )

    def get_pending(self, app_user_id: int, origin: PendingOrigin) -> VaultActionConfirmation | None:
        """Читает актуальное ожидание только для исходного чата."""
        pending = self._confirmation_repository.find_active(app_user_id=app_user_id, now=self._clock())
        return pending if pending is not None and pending.origin == origin else None

    def request_disconnect(self, app_user_id: int, origin: PendingOrigin) -> VaultDisconnectResult:
        """Резервирует конфигурацию vault для подтверждаемого отключения."""
        current = self.get_selected(app_user_id)
        if current is None:
            return VaultDisconnectResult(VaultDisconnectStatus.NOT_CONNECTED)
        now = self._clock()
        confirmation = self._confirmation_repository.save(
            VaultActionConfirmation(
                app_user_id=app_user_id,
                origin=origin,
                base_vault_id=current.id or 0,
                action=VaultConfirmationAction.DISCONNECT,
                expires_at=now + self._confirmation_ttl,
                created_at=now,
            ),
            now=now,
        )
        return VaultDisconnectResult(VaultDisconnectStatus.CONFIRMATION_REQUIRED, current, confirmation)

    def decide(
        self,
        app_user_id: int,
        *,
        confirmation_id: int,
        origin: PendingOrigin,
        version: int,
        action: VaultConfirmationAction,
        approve: bool
    ) -> VaultSelectionResult | VaultDisconnectResult:
        """Проверяет адресную операцию и атомарно завершает изменение конфигурации.

        Args:
            app_user_id: Внутренний владелец операции.
            confirmation_id: Неизменный ID сохранённого ожидания.
            origin: Чат текущего события; должен совпадать с исходным.
            version: Версия операции из выбранного действия.
            action: Тип ожидаемого изменения конфигурации.
            approve: Применить изменение либо отменить ожидание.

        Returns:
            Результат замены либо отключения vault.

        Raises:
            PendingOperationError: ID, исходный чат, TTL, статус или версия уже не подходят.
            VaultResourceBusyError: Vault занят синхронизацией или записью.
        """
        pending = self._confirmation_repository.get_by_id(confirmation_id, app_user_id)
        if pending is None or pending.origin != origin:
            raise PendingOperationError("Заверши действие в исходном чате; операция здесь недоступна.")
        if pending.version != version or pending.action is not action:
            raise PendingOperationError(
                "Действие устарело. Войди снова в /obsidian для актуального ожидания."
            )
        vault = self._confirmation_repository.decide(pending, approve=approve, now=self._clock())
        if action is VaultConfirmationAction.REPLACE:
            if vault is None:
                raise PendingOperationError("Конфигурация vault уже изменилась.")
            return VaultSelectionResult(
                VaultSelectionStatus.REPLACED if approve else VaultSelectionStatus.CANCELLED, vault
            )
        if vault is None:
            return VaultDisconnectResult(VaultDisconnectStatus.NOT_CONNECTED)
        return VaultDisconnectResult(
            VaultDisconnectStatus.DISCONNECTED if approve else VaultDisconnectStatus.CANCELLED, vault
        )


def parse_github_repository_url(value: str) -> tuple[str, str]:
    """Разбирает канонический HTTPS URL GitHub repository.

    Args:
        value: URL вида `https://github.com/owner/repository`.

    Returns:
        Пару `(owner, repository)` без необязательного суффикса `.git`.

    Raises:
        ValueError: URL не указывает ровно на один GitHub repository.
    """
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("repository_url must be a plain GitHub HTTPS URL")
    parts = unquote(parsed.path).strip("/").split("/")
    if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
        raise ValueError("repository_url must point to owner/repository")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not repository or any(character.isspace() for character in owner + repository):
        raise ValueError("repository_url contains an invalid name")
    return owner, repository


def normalize_vault_root_path(value: str) -> str:
    """Нормализует относительный POSIX-путь к корню vault.

    Args:
        value: Путь из команды пользователя; пустой означает корень repository.

    Returns:
        Путь без начального и конечного `/`.

    Raises:
        ValueError: Путь содержит обратный слеш или небезопасный сегмент.
    """
    path = unquote(value.strip()).strip("/")
    if not path:
        return ""
    if "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("vault path must be repository-relative")
    return path


def _same_vault(first: ObsidianVault, second: ObsidianVault) -> bool:
    return (
        first.installation_id,
        first.repository_id,
        first.owner,
        first.repository,
        first.branch,
        first.root_path,
    ) == (
        second.installation_id,
        second.repository_id,
        second.owner,
        second.repository,
        second.branch,
        second.root_path,
    )
