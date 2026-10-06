"""Тесты application-сценария выбора Obsidian vault из GitHub."""

from datetime import UTC, datetime
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from obs_chat_bot.capabilities.obsidian.application.vaults.github_models import GitHubRepositoryInspection
from obs_chat_bot.capabilities.obsidian.application.vaults.vault_selection import (
    GitHubRepositoryNotAccessibleError,
    GitHubVaultPathNotFoundError,
    GitHubVaultSelectionService,
    VaultDisconnectStatus,
    VaultSelectionStatus,
    normalize_vault_root_path,
    parse_github_repository_url,
)
from obs_chat_bot.data.sqlite.connection import connect_database
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_vault_selection_manager import (
    SQLiteGitHubVaultSelectionManager,
)
from obs_chat_bot.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_confirmation_repository import (
    SQLiteVaultActionConfirmationRepository,
)
from tests.sqlite_helpers import ensure_app_user


NOW = datetime(2026, 7, 23, 6, 0, tzinfo=UTC)


class FakeGitHubRepositoryGateway:
    """Возвращает inspections для заданных installation IDs."""

    def __init__(
        self,
        inspections: dict[int, GitHubRepositoryInspection | None],
    ) -> None:
        self.inspections = inspections
        self.calls: list[tuple[int, str, str, str]] = []

    def inspect_repository(
        self,
        *,
        installation_id: int,
        owner: str,
        repository: str,
        root_path: str,
    ) -> GitHubRepositoryInspection | None:
        """Запоминает запрос и возвращает настроенный inspection."""
        self.calls.append((installation_id, owner, repository, root_path))
        return self.inspections.get(installation_id)


class GitHubVaultSelectionServiceTest(unittest.TestCase):
    """Проверяет выбор, повтор и подтверждаемую замену vault."""

    def test_select_finds_repository_in_installations_and_saves_vault(self) -> None:
        """Первый доступный repository сразу становится активным vault."""
        with _database() as connection:
            _prepare_installations(connection, {101, 102})
            gateway = FakeGitHubRepositoryGateway(
                {
                    101: None,
                    102: _inspection(installation_id=102),
                }
            )
            service = _service(connection, gateway)

            result = service.select(
                app_user_id=1,
                repository_url="https://github.com/Octocat/Notes.git/",
                root_path="/Vault/Personal/",
            )

            self.assertEqual(result.status, VaultSelectionStatus.SELECTED)
            self.assertEqual(result.vault.installation_id, 102)
            self.assertEqual(result.vault.root_path, "Vault/Personal")
            self.assertEqual(
                gateway.calls,
                [
                    (101, "Octocat", "Notes", "Vault/Personal"),
                    (102, "Octocat", "Notes", "Vault/Personal"),
                ],
            )

    def test_select_same_vault_does_not_replace_saved_row(self) -> None:
        """Повтор той же команды сохраняет ID и source-состояние vault."""
        with _database() as connection:
            _prepare_installations(connection, {101})
            gateway = FakeGitHubRepositoryGateway({101: _inspection()})
            service = _service(connection, gateway)
            first = service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/notes",
                root_path="Vault",
            )

            repeated = service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/notes",
                root_path="Vault",
            )

            self.assertEqual(repeated.status, VaultSelectionStatus.ALREADY_SELECTED)
            self.assertEqual(repeated.vault.id, first.vault.id)

    def test_different_vault_waits_for_confirmation_and_can_be_cancelled(self) -> None:
        """Новый repository не заменяет текущий до ответа пользователя."""
        with _database() as connection:
            _prepare_installations(connection, {101})
            gateway = FakeGitHubRepositoryGateway(
                {
                    101: _inspection(repository_id=501, repository="notes"),
                }
            )
            service = _service(connection, gateway)
            original = service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/notes",
                root_path="Vault",
            )
            gateway.inspections[101] = _inspection(
                repository_id=502,
                repository="second",
            )

            pending = service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/second",
                root_path="Second",
            )
            current_before_cancel = SQLiteObsidianVaultRepository(
                connection
            ).get_for_user(1)
            cancelled = service.cancel_replacement(1)

            self.assertEqual(
                pending.status,
                VaultSelectionStatus.REPLACEMENT_CONFIRMATION_REQUIRED,
            )
            self.assertEqual(current_before_cancel.id, original.vault.id)
            self.assertEqual(cancelled.status, VaultSelectionStatus.CANCELLED)
            self.assertEqual(cancelled.vault.id, original.vault.id)

    def test_confirm_replacement_atomically_switches_active_vault(self) -> None:
        """Ответ `да` применяет сохранённое предложение замены."""
        with _database() as connection:
            _prepare_installations(connection, {101})
            gateway = FakeGitHubRepositoryGateway({101: _inspection()})
            service = _service(connection, gateway)
            first = service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/notes",
                root_path="Vault",
            )
            gateway.inspections[101] = _inspection(
                repository_id=502,
                repository="second",
            )
            service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/second",
            )

            replaced = service.confirm_replacement(1)

            self.assertEqual(replaced.status, VaultSelectionStatus.REPLACED)
            self.assertEqual(replaced.vault.repository, "second")
            self.assertNotEqual(replaced.vault.id, first.vault.id)
            self.assertFalse(service.has_replacement_confirmation(1))

    def test_disconnect_requires_confirmation_and_preserves_user_data(self) -> None:
        """Подтверждение удаляет только vault-каталог, сохраняя профиль и статьи."""
        with _database() as connection:
            _prepare_installations(connection, {101})
            service = _service(
                connection,
                FakeGitHubRepositoryGateway({101: _inspection()}),
            )
            selected = service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/notes",
            )
            connection.execute(
                """
                INSERT INTO obsidian_notes (
                    app_user_id, vault_id, path, blob_sha, markdown
                )
                VALUES (1, ?, 'Note.md', 'blob-1', '# Note')
                """,
                (selected.vault.id,),
            )
            connection.execute(
                """
                INSERT INTO articles (
                    app_user_id, source_url, normalized_url, status
                )
                VALUES (
                    1,
                    'https://example.com/article',
                    'https://example.com/article',
                    'new'
                )
                """
            )
            connection.commit()

            requested = service.request_disconnect(1)
            disconnected = service.confirm_disconnect(1)

            self.assertEqual(
                requested.status,
                VaultDisconnectStatus.CONFIRMATION_REQUIRED,
            )
            self.assertEqual(
                disconnected.status,
                VaultDisconnectStatus.DISCONNECTED,
            )
            self.assertIsNone(service.get_selected(1))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM obsidian_notes").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM github_accounts").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
                1,
            )

    def test_disconnect_can_be_cancelled_and_requires_active_vault(self) -> None:
        """Ответ `нет` сохраняет vault, а повтор без vault не требует подтверждения."""
        with _database() as connection:
            _prepare_installations(connection, {101})
            service = _service(
                connection,
                FakeGitHubRepositoryGateway({101: _inspection()}),
            )
            service.select(
                app_user_id=1,
                repository_url="https://github.com/octocat/notes",
            )

            service.request_disconnect(1)
            cancelled = service.cancel_disconnect(1)

            self.assertEqual(cancelled.status, VaultDisconnectStatus.CANCELLED)
            self.assertIsNotNone(service.get_selected(1))
            self.assertFalse(service.has_disconnect_confirmation(1))

    def test_select_distinguishes_repository_and_path_failures(self) -> None:
        """Недоступный repository и отсутствующий каталог имеют разные ошибки."""
        with _database() as connection:
            _prepare_installations(connection, {101})
            gateway = FakeGitHubRepositoryGateway({101: None})
            service = _service(connection, gateway)
            with self.assertRaises(GitHubRepositoryNotAccessibleError):
                service.select(
                    app_user_id=1,
                    repository_url="https://github.com/octocat/missing",
                )

            gateway.inspections[101] = _inspection(root_path_is_directory=False)
            with self.assertRaises(GitHubVaultPathNotFoundError):
                service.select(
                    app_user_id=1,
                    repository_url="https://github.com/octocat/notes",
                    root_path="missing",
                )

    def test_repository_url_and_vault_path_reject_unsafe_values(self) -> None:
        """Parser принимает только GitHub HTTPS URL и относительный POSIX path."""
        self.assertEqual(
            parse_github_repository_url("https://github.com/octocat/notes.git"),
            ("octocat", "notes"),
        )
        self.assertEqual(
            normalize_vault_root_path("/Notes/Personal/"),
            "Notes/Personal",
        )
        for value in (
            "http://github.com/octocat/notes",
            "https://example.com/octocat/notes",
            "https://github.com/octocat/notes/issues",
            "https://github.com/octocat/notes?tab=readme",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_github_repository_url(value)
        with self.assertRaises(ValueError):
            normalize_vault_root_path("Notes/../Secrets")

    def test_sqlite_manager_selects_vault_after_request_connection_is_closed(
        self,
    ) -> None:
        """Background callback открывает собственное SQLite-соединение."""
        with TemporaryDirectory(prefix="obs-chat-bot-vault-background-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
                _prepare_installations(connection, {101})

            manager = SQLiteGitHubVaultSelectionManager(
                database_path=database_path,
                github_gateway=FakeGitHubRepositoryGateway({101: _inspection()}),
            )
            results = []
            worker = Thread(
                target=lambda: results.append(
                    manager.select(
                        app_user_id=1,
                        repository_url="https://github.com/octocat/notes",
                    )
                )
            )
            worker.start()
            worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0].status, VaultSelectionStatus.SELECTED)
            self.assertEqual(manager.get_selected(1).repository, "notes")


@contextmanager
def _database():
    """Создаёт временную SQLite-базу и возвращает её context manager."""
    with TemporaryDirectory(prefix="obs-chat-bot-vault-select-") as directory:
        path = Path(directory) / "test.db"
        with connect_database(path) as connection:
            apply_migrations(connection)
            yield connection


def _prepare_installations(connection, installation_ids: set[int]) -> None:
    """Создаёт пользователя, GitHub account и installations."""
    ensure_app_user(connection)
    connection.execute(
        "INSERT INTO github_accounts (app_user_id, github_user_id, login) "
        "VALUES (1, 777, 'octocat')"
    )
    connection.commit()
    SQLiteGitHubInstallationRepository(connection).replace_for_user(
        app_user_id=1,
        installation_ids=installation_ids,
    )


def _inspection(
    *,
    installation_id: int = 101,
    repository_id: int = 501,
    repository: str = "notes",
    root_path_is_directory: bool = True,
) -> GitHubRepositoryInspection:
    """Создаёт доступный тестовый GitHub repository."""
    return GitHubRepositoryInspection(
        installation_id=installation_id,
        repository_id=repository_id,
        owner="octocat",
        repository=repository,
        default_branch="main",
        root_path_is_directory=root_path_is_directory,
    )


def _service(connection, gateway) -> GitHubVaultSelectionService:
    """Собирает service с SQLite repositories и фиксированным временем."""
    return GitHubVaultSelectionService(
        installation_repository=SQLiteGitHubInstallationRepository(connection),
        vault_repository=SQLiteObsidianVaultRepository(connection),
        confirmation_repository=SQLiteVaultActionConfirmationRepository(connection),
        github_gateway=gateway,
        clock=lambda: NOW,
    )


if __name__ == "__main__":
    unittest.main()
