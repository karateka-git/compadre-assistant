from __future__ import annotations

import sqlite3
from pathlib import Path

from obs_chat_bot.capabilities.obsidian.application.vaults.ports import GitHubRepositoryGateway
from obs_chat_bot.capabilities.obsidian.application.vaults.vault_selection import (
    GitHubVaultSelectionService,
    VaultDisconnectResult,
    VaultSelectionManager,
    VaultSelectionResult,
)
from obs_chat_bot.data.sqlite.connection import connect_database
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
        repository_url: str,
        root_path: str = "",
    ) -> VaultSelectionResult:
        """Проверяет GitHub repository и выбирает его как vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).select(
                app_user_id=app_user_id,
                repository_url=repository_url,
                root_path=root_path,
            )

    def has_replacement_confirmation(self, app_user_id: int) -> bool:
        """Проверяет наличие ожидающей замены vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).has_replacement_confirmation(
                app_user_id
            )

    def confirm_replacement(self, app_user_id: int) -> VaultSelectionResult | None:
        """Применяет ожидающую замену vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).confirm_replacement(app_user_id)

    def cancel_replacement(self, app_user_id: int) -> VaultSelectionResult | None:
        """Отменяет ожидающую замену vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).cancel_replacement(app_user_id)

    def request_disconnect(self, app_user_id: int) -> VaultDisconnectResult:
        """Создаёт подтверждение отключения активного vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).request_disconnect(app_user_id)

    def has_disconnect_confirmation(self, app_user_id: int) -> bool:
        """Проверяет наличие ожидающего отключения vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).has_disconnect_confirmation(app_user_id)

    def confirm_disconnect(self, app_user_id: int) -> VaultDisconnectResult | None:
        """Удаляет подтверждённый vault и локальный каталог."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).confirm_disconnect(app_user_id)

    def cancel_disconnect(self, app_user_id: int) -> VaultDisconnectResult | None:
        """Отменяет ожидающее отключение vault."""
        with connect_database(self._database_path) as connection:
            return self._service(connection).cancel_disconnect(app_user_id)

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
