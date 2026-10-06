"""Тесты доменных правил GitHub и Obsidian vault."""

from datetime import UTC, datetime, timedelta
import unittest

from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import (
    GitHubInstallation,
    ObsidianVault,
    VaultActionConfirmation,
    VaultConfirmationAction,
    VaultNote,
    VaultSyncLease,
)


class VaultDomainTest(unittest.TestCase):
    """Проверяет инварианты моделей локального каталога vault."""

    def test_root_vault_and_empty_markdown_note_are_valid(self) -> None:
        """Корень repository и пустой Markdown являются допустимыми данными."""
        installation = GitHubInstallation(app_user_id=1, installation_id=101)
        vault = ObsidianVault(
            id=1,
            app_user_id=installation.app_user_id,
            installation_id=installation.installation_id,
            repository_id=501,
            owner="octocat",
            repository="notes",
            branch="main",
        )
        note = VaultNote(
            app_user_id=1,
            vault_id=vault.id,
            path="Inbox/empty.md",
            blob_sha="blob-1",
            markdown="",
        )

        self.assertEqual(vault.root_path, "")
        self.assertEqual(note.markdown, "")

    def test_note_rejects_unsafe_or_non_markdown_path(self) -> None:
        """Заметка принимает только относительный путь Markdown-файла."""
        for path in ("../secret.md", "/absolute.md", "folder\\note.md", "note.txt"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    _note(path=path)

    def test_note_rejects_duplicate_metadata(self) -> None:
        """Tags и wikilinks не содержат дубликаты внутри одной заметки."""
        with self.assertRaises(ValueError):
            _note(tags=("python", "python"))
        with self.assertRaises(ValueError):
            _note(wikilinks=("Index", "Index"))

    def test_lease_expiration_must_follow_acquisition(self) -> None:
        """Lease не может истечь до момента его захвата."""
        now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        with self.assertRaises(ValueError):
            VaultSyncLease(
                app_user_id=1,
                vault_id=1,
                owner="tg-catcher",
                acquired_at=now,
                expires_at=now - timedelta(seconds=1),
            )

    def test_replace_confirmation_requires_same_user_vault(self) -> None:
        """Предложенный vault обязателен и принадлежит тому же пользователю."""
        now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        with self.assertRaises(ValueError):
            VaultActionConfirmation(
                app_user_id=1,
                action=VaultConfirmationAction.REPLACE,
                expires_at=now + timedelta(minutes=10),
            )
        with self.assertRaises(ValueError):
            VaultActionConfirmation(
                app_user_id=1,
                action=VaultConfirmationAction.REPLACE,
                replacement=ObsidianVault(
                    app_user_id=2,
                    installation_id=202,
                    repository_id=502,
                    owner="octocat",
                    repository="other",
                    branch="main",
                ),
                expires_at=now + timedelta(minutes=10),
            )


def _note(
    *,
    path: str = "note.md",
    tags: tuple[str, ...] = (),
    wikilinks: tuple[str, ...] = (),
) -> VaultNote:
    """Создаёт заметку для проверки доменных ограничений."""
    return VaultNote(
        app_user_id=1,
        vault_id=1,
        path=path,
        blob_sha="blob-1",
        markdown="# Note",
        tags=tags,
        wikilinks=wikilinks,
    )


if __name__ == "__main__":
    unittest.main()
