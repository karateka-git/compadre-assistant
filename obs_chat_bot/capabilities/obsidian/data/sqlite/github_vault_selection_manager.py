from __future__ import annotations

import sqlite3
from pathlib import Path

from obs_chat_bot.capabilities.obsidian.domain.pending import PendingOrigin
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultActionConfirmation, VaultConfirmationAction

from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import GitHubRepositoryGateway
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_selection import (
    GitHubVaultSelectionService,
    VaultDisconnectResult,
    VaultSelectionManager,
    VaultSelectionResult,
)
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_confirmation_repository import (
    SQLiteVaultActionConfirmationRepository,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault


class SQLiteGitHubVaultSelectionManager(VaultSelectionManager):
    """Выполняет операции vault через отдельные короткие SQLite-соединения.

    Менеджер безопасно используется как в worker входящего сообщения, так и в
    background thread Device Flow после закрытия исходного соединения.
    """

    def __init__(
        self,
        *,
        database_path: Path,
        github_gateway: GitHubRepositoryGateway,
    ) -> None:
        self._database_path = database_path
        self._github_gateway = github_gateway

    def get_selected(self, app_user_id: int) -> ObsidianVault | None:
        """Возвращает активный vault пользователя."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).get_selected(app_user_id)

    def select(
        self,
        *,
        app_user_id: int,
        origin: PendingOrigin,
        repository_url: str,
        root_path: str = "",
    ) -> VaultSelectionResult:
        """Проверяет GitHub repository и выбирает его как vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).select(
                app_user_id=app_user_id,
                origin=origin,
                repository_url=repository_url,
                root_path=root_path,
            )

    def get_pending(self, app_user_id: int, origin: PendingOrigin) -> VaultActionConfirmation | None:
        """Показывает только ожидание исходного чата через короткое соединение."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).get_pending(app_user_id, origin)

    def request_disconnect(self, app_user_id: int, origin: PendingOrigin) -> VaultDisconnectResult:
        """Резервирует конфигурацию для адресного отключения."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).request_disconnect(app_user_id, origin)

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
        """Выполняет проверенное решение на собственном коротком соединении."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).decide(
                app_user_id,
                confirmation_id=confirmation_id,
                origin=origin,
                version=version,
                action=action,
                approve=approve,
            )

    def _service(
        self,
        connection: sqlite3.Connection,
    ) -> GitHubVaultSelectionService:
        """Собирает application-сервис на текущем коротком соединении."""
        return GitHubVaultSelectionService(
            installation_repository=SQLiteGitHubInstallationRepository(connection),
            vault_repository=SQLiteObsidianVaultRepository(connection),
            confirmation_repository=SQLiteVaultActionConfirmationRepository(
                connection
            ),
            github_gateway=self._github_gateway,
        )
