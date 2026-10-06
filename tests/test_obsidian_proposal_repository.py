"""Интеграционные тесты SQLite-жизненного цикла Obsidian proposal."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import (
    PendingObsidianProposalError,
    SQLiteObsidianProposalRepository,
)
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
    ObsidianProposalStatus,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultNote
from tests.sqlite_helpers import ensure_app_user


class SQLiteObsidianProposalRepositoryTests(unittest.TestCase):
    """Проверяет pending uniqueness и атомарную локальную финализацию."""

    def test_add_completion_updates_note_vault_article_and_history(self) -> None:
        with _database() as connection:
            repository = SQLiteObsidianProposalRepository(connection)
            pending = repository.save_pending(_proposal(ObsidianProposalAction.ADD))

            applied = repository.complete_applied(
                proposal_id=pending.id or 0,
                app_user_id=1,
                applied_commit_sha="commit-new",
                head_commit_sha="commit-new",
                tree_sha="tree-new",
                note=VaultNote(
                    app_user_id=1,
                    vault_id=1,
                    path="Tech/New.md",
                    blob_sha="blob-new",
                    markdown="# New\nText",
                    title="New",
                    tags=("knowledge-catcher",),
                    wikilinks=("Tech",),
                ),
            )

            article = connection.execute(
                "SELECT status, cleaned_text FROM articles WHERE id = 1"
            ).fetchone()
            vault = connection.execute(
                "SELECT head_commit_sha, tree_sha FROM obsidian_vaults WHERE id = 1"
            ).fetchone()
            note = connection.execute(
                "SELECT path, blob_sha, markdown FROM obsidian_notes WHERE vault_id = 1"
            ).fetchone()

            self.assertEqual(applied.status, ObsidianProposalStatus.APPLIED)
            self.assertEqual(tuple(article), ("reviewed", None))
            self.assertEqual(tuple(vault), ("commit-new", "tree-new"))
            self.assertEqual(tuple(note), ("Tech/New.md", "blob-new", "# New\nText"))

    def test_only_one_pending_proposal_is_allowed_per_user(self) -> None:
        with _database() as connection:
            repository = SQLiteObsidianProposalRepository(connection)
            repository.save_pending(_proposal(ObsidianProposalAction.SKIP))

            with self.assertRaises(PendingObsidianProposalError):
                repository.save_pending(_proposal(ObsidianProposalAction.SKIP))

    def test_skip_clears_source_text_without_creating_github_result(self) -> None:
        with _database() as connection:
            repository = SQLiteObsidianProposalRepository(connection)
            pending = repository.save_pending(_proposal(ObsidianProposalAction.SKIP))

            applied = repository.complete_applied(
                proposal_id=pending.id or 0,
                app_user_id=1,
                applied_commit_sha=None,
                head_commit_sha=None,
                tree_sha=None,
                note=None,
            )

            article = connection.execute(
                "SELECT status, cleaned_text FROM articles WHERE id = 1"
            ).fetchone()
            self.assertEqual(applied.status, ObsidianProposalStatus.APPLIED)
            self.assertIsNone(applied.applied_commit_sha)
            self.assertEqual(tuple(article), ("reviewed", None))

    def test_applied_history_survives_vault_replacement(self) -> None:
        with _database() as connection:
            repository = SQLiteObsidianProposalRepository(connection)
            pending = repository.save_pending(_proposal(ObsidianProposalAction.SKIP))
            repository.complete_applied(
                proposal_id=pending.id or 0,
                app_user_id=1,
                applied_commit_sha=None,
                head_commit_sha=None,
                tree_sha=None,
                note=None,
            )

            connection.execute("DELETE FROM obsidian_vaults WHERE id = 1")
            connection.commit()

            saved = repository.get_latest_applied_for_article(
                app_user_id=1,
                article_id=1,
            )
            self.assertIsNotNone(saved)
            self.assertEqual(saved.status, ObsidianProposalStatus.APPLIED)


class _DatabaseContext:
    def __init__(self) -> None:
        self.directory = TemporaryDirectory(prefix="obs-proposals-")
        self.connection_context = connect_database(
            Path(self.directory.name) / "test.db"
        )

    def __enter__(self):
        connection = self.connection_context.__enter__()
        apply_migrations(connection)
        ensure_app_user(connection)
        connection.executescript(
            """
            INSERT INTO github_accounts (app_user_id, github_user_id, login)
            VALUES (1, 100, 'test');
            INSERT INTO github_installations (app_user_id, installation_id)
            VALUES (1, 200);
            INSERT INTO obsidian_vaults (
                id, app_user_id, installation_id, repository_id, owner,
                repository, branch, head_commit_sha, tree_sha
            ) VALUES (
                1, 1, 200, 300, 'owner', 'vault', 'main', 'base-commit', 'base-tree'
            );
            INSERT INTO articles (
                id, app_user_id, source_url, normalized_url, title,
                cleaned_text, text_hash, status
            ) VALUES (
                1, 1, 'https://example.com/a', 'https://example.com/a',
                'A', 'source text', 'hash', 'analyzed'
            );
            INSERT INTO analysis_results (
                id, app_user_id, article_id, llm_model, prompt_version, result_text
            ) VALUES (1, 1, 1, 'model', 'v1', 'summary');
            """
        )
        connection.commit()
        return connection

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.connection_context.__exit__(exc_type, exc_value, traceback)
        self.directory.cleanup()


def _database() -> _DatabaseContext:
    return _DatabaseContext()


def _proposal(action: ObsidianProposalAction) -> ObsidianProposal:
    kwargs = {}
    if action is ObsidianProposalAction.ADD:
        kwargs = {"target_path": "Tech/New.md", "proposed_markdown": "# New\nText"}
    return ObsidianProposal(
        app_user_id=1,
        article_id=1,
        analysis_id=1,
        vault_id=1,
        action=action,
        reasoning="Тестовое решение.",
        base_commit_sha="base-commit",
        base_tree_sha="base-tree",
        **kwargs,
    )


if __name__ == "__main__":
    unittest.main()
