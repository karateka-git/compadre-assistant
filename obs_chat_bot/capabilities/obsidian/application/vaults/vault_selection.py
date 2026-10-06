from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import time
from typing import Callable, Protocol
from urllib.parse import unquote, urlsplit

from obs_chat_bot.capabilities.obsidian.application.vaults.ports import (
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
        repository_url: str,
        root_path: str = "",
    ) -> VaultSelectionResult:
        """Проверяет GitHub repository и выбирает его как vault."""

    def has_replacement_confirmation(self, app_user_id: int) -> bool:
        """Проверяет наличие ожидающей замены vault."""

    def confirm_replacement(self, app_user_id: int) -> VaultSelectionResult | None:
        """Применяет ожидающую замену или возвращает `None`."""

    def cancel_replacement(self, app_user_id: int) -> VaultSelectionResult | None:
        """Отменяет ожидающую замену или возвращает `None`."""

    def request_disconnect(self, app_user_id: int) -> VaultDisconnectResult:
        """Запрашивает подтверждение удаления активного vault."""

    def has_disconnect_confirmation(self, app_user_id: int) -> bool:
        """Проверяет наличие активного подтверждения отключения."""

    def confirm_disconnect(self, app_user_id: int) -> VaultDisconnectResult | None:
        """Удаляет vault после согласия или возвращает `None`."""

    def cancel_disconnect(self, app_user_id: int) -> VaultDisconnectResult | None:
        """Отменяет отключение либо возвращает `None`."""


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
        repository_url: str,
        root_path: str = "",
    ) -> VaultSelectionResult:
        """Проверяет repository и сохраняет либо предлагает активный vault.

        Args:
            app_user_id: Внутренний ID пользователя приложения.
            repository_url: Полный HTTPS URL GitHub repository.
            root_path: Путь к корню vault внутри repository или пустая строка.

        Returns:
            Результат первого выбора, повтора или запроса подтверждения.

        Raises:
            ValueError: URL или repository path имеют небезопасный формат.
            GitHubAccountNotConnectedError: У пользователя нет installations.
            GitHubRepositoryNotAccessibleError: У App нет права записи repository.
            GitHubVaultPathNotFoundError: Vault path не является каталогом.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        owner, repository = parse_github_repository_url(repository_url)
        normalized_root = normalize_vault_root_path(root_path)
        installations = self._installation_repository.list_for_user(app_user_id)
        if not installations:
            raise GitHubAccountNotConnectedError(
                "GitHub account has no available installations"
            )

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
            raise GitHubRepositoryNotAccessibleError(
                "GitHub repository is not accessible"
            )
        if not inspection.root_path_is_directory:
            raise GitHubVaultPathNotFoundError(
                "GitHub vault path is not a directory"
            )

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
                self._confirmation_repository.delete(app_user_id)
                return VaultSelectionResult(VaultSelectionStatus.SELECTED, saved)
            # Другой adapter мог выбрать vault между чтением и вставкой.
            current = self._vault_repository.get_for_user(app_user_id)
            if current is None:
                raise RuntimeError("Concurrent vault selection could not be read")
        if _same_vault(current, candidate):
            self._confirmation_repository.delete(app_user_id)
            return VaultSelectionResult(VaultSelectionStatus.ALREADY_SELECTED, current)

        now = self._clock()
        self._confirmation_repository.save(
            VaultActionConfirmation(
                app_user_id=app_user_id,
                action=VaultConfirmationAction.REPLACE,
                replacement=candidate,
                expires_at=now + self._confirmation_ttl,
                created_at=now,
            )
        )
        return VaultSelectionResult(
            VaultSelectionStatus.REPLACEMENT_CONFIRMATION_REQUIRED,
            candidate,
        )

    def has_replacement_confirmation(self, app_user_id: int) -> bool:
        """Проверяет наличие активного подтверждения замены vault."""
        confirmation = self._active_replacement(app_user_id)
        return confirmation is not None

    def confirm_replacement(self, app_user_id: int) -> VaultSelectionResult | None:
        """Заменяет vault предложенным подключением после явного согласия."""
        confirmation = self._active_replacement(app_user_id)
        if confirmation is None or confirmation.replacement is None:
            return None
        saved = self._vault_repository.replace(confirmation.replacement)
        self._confirmation_repository.delete(app_user_id)
        return VaultSelectionResult(VaultSelectionStatus.REPLACED, saved)

    def cancel_replacement(self, app_user_id: int) -> VaultSelectionResult | None:
        """Отменяет замену, сохраняя текущий активный vault."""
        confirmation = self._active_replacement(app_user_id)
        if confirmation is None:
            return None
        current = self._vault_repository.get_for_user(app_user_id)
        self._confirmation_repository.delete(app_user_id)
        if current is None:
            return None
        return VaultSelectionResult(VaultSelectionStatus.CANCELLED, current)

    def request_disconnect(self, app_user_id: int) -> VaultDisconnectResult:
        """Создаёт короткоживущее подтверждение отключения активного vault."""
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        current = self._vault_repository.get_for_user(app_user_id)
        if current is None:
            self._confirmation_repository.delete(app_user_id)
            return VaultDisconnectResult(VaultDisconnectStatus.NOT_CONNECTED)
        now = self._clock()
        self._confirmation_repository.save(
            VaultActionConfirmation(
                app_user_id=app_user_id,
                action=VaultConfirmationAction.DISCONNECT,
                expires_at=now + self._confirmation_ttl,
                created_at=now,
            )
        )
        return VaultDisconnectResult(
            VaultDisconnectStatus.CONFIRMATION_REQUIRED,
            current,
        )

    def has_disconnect_confirmation(self, app_user_id: int) -> bool:
        """Проверяет наличие активного подтверждения отключения vault."""
        return self._active_disconnect(app_user_id) is not None

    def confirm_disconnect(self, app_user_id: int) -> VaultDisconnectResult | None:
        """Удаляет активный vault и зависимые локальные данные после согласия."""
        if self._active_disconnect(app_user_id) is None:
            return None
        current = self._vault_repository.get_for_user(app_user_id)
        if current is None:
            self._confirmation_repository.delete(app_user_id)
            return VaultDisconnectResult(VaultDisconnectStatus.NOT_CONNECTED)
        self._vault_repository.delete_for_user(app_user_id)
        self._confirmation_repository.delete(app_user_id)
        return VaultDisconnectResult(VaultDisconnectStatus.DISCONNECTED, current)

    def cancel_disconnect(self, app_user_id: int) -> VaultDisconnectResult | None:
        """Отменяет ожидающее отключение и сохраняет активный vault."""
        if self._active_disconnect(app_user_id) is None:
            return None
        current = self._vault_repository.get_for_user(app_user_id)
        self._confirmation_repository.delete(app_user_id)
        if current is None:
            return VaultDisconnectResult(VaultDisconnectStatus.NOT_CONNECTED)
        return VaultDisconnectResult(VaultDisconnectStatus.CANCELLED, current)

    def _active_replacement(
        self,
        app_user_id: int,
    ) -> VaultActionConfirmation | None:
        confirmation = self._confirmation_repository.find_active(
            app_user_id=app_user_id,
            now=self._clock(),
        )
        if (
            confirmation is None
            or confirmation.action is not VaultConfirmationAction.REPLACE
        ):
            return None
        return confirmation

    def _active_disconnect(
        self,
        app_user_id: int,
    ) -> VaultActionConfirmation | None:
        confirmation = self._confirmation_repository.find_active(
            app_user_id=app_user_id,
            now=self._clock(),
        )
        if (
            confirmation is None
            or confirmation.action is not VaultConfirmationAction.DISCONNECT
        ):
            return None
        return confirmation


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
