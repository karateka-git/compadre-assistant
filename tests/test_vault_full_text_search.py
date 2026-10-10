"""Интеграционные тесты SQLite FTS5-поиска Этапа 10.3."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from document_chunker import (
    ChunkingPolicy,
    DocumentChunker,
    DocumentFormat,
    MarkdownDocumentParser,
)
from compadre_assistant.capabilities.obsidian.domain.search.full_text import VaultFullTextSearchService
from compadre_assistant.capabilities.obsidian.domain.search.indexing import VaultChunkIndexer
from compadre_assistant.capabilities.obsidian.data.chunking.document_note_chunker import DocumentVaultNoteChunker
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import (
    SQLiteVaultChunkIndexRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_full_text_search_repository import (
    SQLiteVaultFullTextSearchRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote
from tests.sqlite_helpers import ensure_app_user


class VaultFullTextSearchTest(unittest.TestCase):
    """Проверяет поля FTS, актуальность поколения и tenant isolation."""

    def test_search_indexes_title_path_tags_headings_and_chunk_text(self) -> None:
        """Каждое согласованное поле заметки участвует в точном поиске."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            notes = SQLiteVaultNoteRepository(connection)
            saved_notes = (
                notes.upsert(
                    _note(
                        vault.id,
                        path="title.md",
                        title="Квантовый заголовок",
                    )
                ),
                notes.upsert(
                    _note(vault.id, path="folders/roadmap-marker.md")
                ),
                notes.upsert(
                    _note(vault.id, path="tag.md", tags=("телеметрия",))
                ),
                notes.upsert(
                    _note(
                        vault.id,
                        path="heading.md",
                        markdown="# Оркестрация\nНейтральный текст.",
                    )
                ),
                notes.upsert(
                    _note(
                        vault.id,
                        path="text.md",
                        markdown="# Раздел\nИспользуется репликация данных.",
                    )
                ),
            )
            search = _build_search(connection, notes=saved_notes, vault=vault)

            cases = {
                "квантовый": "title.md",
                "roadmap": "folders/roadmap-marker.md",
                "телеметрия": "tag.md",
                "оркестрация": "heading.md",
                "репликация": "text.md",
            }
            for query, expected_path in cases.items():
                with self.subTest(query=query):
                    hits = search.search(
                        app_user_id=1,
                        vault_id=vault.id,
                        query=query,
                    )
                    self.assertTrue(hits)
                    self.assertEqual(hits[0].chunk.note_path, expected_path)
                    self.assertGreaterEqual(hits[0].score, 0)
            self.assertFalse(
                search.search(
                    app_user_id=1,
                    vault_id=vault.id,
                    query="реплик",
                )
            )

    def test_metadata_and_chunk_changes_refresh_fts_without_stale_rows(self) -> None:
        """Triggers обновляют title/tags, а chunk diff заменяет старый текст."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            notes = SQLiteVaultNoteRepository(connection)
            original = notes.upsert(
                _note(
                    vault.id,
                    title="Старое имя",
                    tags=("старый-тег",),
                    markdown="# Раздел\nСтарое содержимое.",
                )
            )
            chunker = _chunker()
            indexer = VaultChunkIndexer(
                chunker=chunker,
                repository=SQLiteVaultChunkIndexRepository(connection),
            )
            indexer.rebuild(app_user_id=1, vault_id=vault.id, notes=(original,))
            search = VaultFullTextSearchService(
                repository=SQLiteVaultFullTextSearchRepository(connection),
                chunker=chunker,
            )
            changed = notes.upsert(
                _note(
                    vault.id,
                    title="Новое имя",
                    tags=("новый-тег",),
                    markdown="# Раздел\nНовое содержимое.",
                )
            )
            indexer.invalidate(app_user_id=1, vault_id=vault.id)
            indexer.index_note(changed)
            indexer.mark_current(app_user_id=1, vault_id=vault.id)

            for query in ("новое", "новый", "содержимое"):
                self.assertTrue(
                    search.search(
                        app_user_id=1,
                        vault_id=vault.id,
                        query=query,
                    )
                )
            for query in ("старое", "старый"):
                self.assertFalse(
                    search.search(
                        app_user_id=1,
                        vault_id=vault.id,
                        query=query,
                    )
                )
            notes.delete_paths(
                app_user_id=1,
                vault_id=vault.id,
                paths={"note.md"},
            )
            self.assertFalse(
                search.search(
                    app_user_id=1,
                    vault_id=vault.id,
                    query="новое",
                )
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM obsidian_note_chunks_fts"
                ).fetchone()[0],
                0,
            )

    def test_bm25_prefers_title_over_chunk_text(self) -> None:
        """Согласованные веса поднимают совпадение в title выше body-текста."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            notes = SQLiteVaultNoteRepository(connection)
            title_note = notes.upsert(
                _note(
                    vault.id,
                    path="title.md",
                    title="Приоритет",
                )
            )
            text_note = notes.upsert(
                _note(
                    vault.id,
                    path="text.md",
                    markdown="# Раздел\nПриоритет упомянут в обычном тексте.",
                )
            )
            search = _build_search(
                connection,
                notes=(title_note, text_note),
                vault=vault,
            )

            hits = search.search(
                app_user_id=1,
                vault_id=vault.id,
                query="приоритет",
            )

            self.assertEqual(len(hits), 2)
            self.assertEqual(hits[0].chunk.note_path, "title.md")
            self.assertGreater(hits[0].score, hits[1].score)

    def test_search_rejects_stale_generation_and_isolates_users(self) -> None:
        """Чужие chunks и generation другой signature не попадают в выдачу."""
        with self._database() as connection:
            first_vault = _prepare_vault(connection)
            second_vault = _prepare_vault(
                connection,
                app_user_id=2,
                installation_id=202,
                repository_id=502,
            )
            notes = SQLiteVaultNoteRepository(connection)
            first_note = notes.upsert(
                _note(first_vault.id, markdown="# Общая тема\nПервый пользователь.")
            )
            second_note = notes.upsert(
                _note(
                    second_vault.id,
                    app_user_id=2,
                    path="secret.md",
                    markdown="# Общая тема\nСекрет второго пользователя.",
                )
            )
            chunker = _chunker()
            indexer = VaultChunkIndexer(
                chunker=chunker,
                repository=SQLiteVaultChunkIndexRepository(connection),
            )
            indexer.rebuild(
                app_user_id=1,
                vault_id=first_vault.id,
                notes=(first_note,),
            )
            indexer.rebuild(
                app_user_id=2,
                vault_id=second_vault.id,
                notes=(second_note,),
            )
            repository = SQLiteVaultFullTextSearchRepository(connection)

            own_hits = VaultFullTextSearchService(
                repository=repository,
                chunker=chunker,
            ).search(
                app_user_id=1,
                vault_id=first_vault.id,
                query="общая",
            )
            stale_hits = repository.search(
                app_user_id=1,
                vault_id=first_vault.id,
                query="общая",
                expected_index_signature="different-signature",
                limit=10,
            )
            SQLiteVaultChunkIndexRepository(connection).invalidate(
                app_user_id=1,
                vault_id=first_vault.id,
            )
            invalid_hits = VaultFullTextSearchService(
                repository=repository,
                chunker=chunker,
            ).search(
                app_user_id=1,
                vault_id=first_vault.id,
                query="общая",
            )

            self.assertTrue(own_hits)
            self.assertEqual({hit.chunk.app_user_id for hit in own_hits}, {1})
            self.assertNotIn("Секрет", " ".join(hit.chunk.text for hit in own_hits))
            self.assertEqual(stale_hits, ())
            self.assertEqual(invalid_hits, ())

    def test_query_is_plain_text_not_raw_fts_syntax(self) -> None:
        """Кавычки и FTS-операторы во входе не ломают SQL и не исполняются."""
        with self._database() as connection:
            vault = _prepare_vault(connection)
            note = SQLiteVaultNoteRepository(connection).upsert(
                _note(vault.id, markdown="# Python\nПоиск безопасен.")
            )
            search = _build_search(connection, notes=(note,), vault=vault)

            hits = search.search(
                app_user_id=1,
                vault_id=vault.id,
                query='python" OR * NEAR(',
            )

            self.assertTrue(hits)
            self.assertEqual(hits[0].chunk.note_path, "note.md")

    @contextmanager
    def _database(self):
        """Открывает временную SQLite-БД с начальной development-схемой."""
        with TemporaryDirectory(prefix="compadre-assistant-fts-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                yield connection


def _build_search(
    connection,
    *,
    notes: tuple[VaultNote, ...],
    vault: ObsidianVault,
) -> VaultFullTextSearchService:
    """Индексирует заметки и собирает application-сервис FTS5."""
    if vault.id is None:
        raise ValueError("vault must be saved")
    chunker = _chunker()
    VaultChunkIndexer(
        chunker=chunker,
        repository=SQLiteVaultChunkIndexRepository(connection),
    ).rebuild(app_user_id=vault.app_user_id, vault_id=vault.id, notes=notes)
    return VaultFullTextSearchService(
        repository=SQLiteVaultFullTextSearchRepository(connection),
        chunker=chunker,
    )


def _chunker() -> DocumentVaultNoteChunker:
    """Собирает реальный Markdown chunker с компактной test policy."""
    return DocumentVaultNoteChunker(
        DocumentChunker(
            parsers={DocumentFormat.MARKDOWN: MarkdownDocumentParser()},
            policy=ChunkingPolicy(
                minimum_size_chars=1,
                target_size_chars=80,
                maximum_size_chars=160,
            ),
        )
    )


def _prepare_vault(
    connection,
    *,
    app_user_id: int = 1,
    installation_id: int = 101,
    repository_id: int = 501,
) -> ObsidianVault:
    """Создаёт пользователя, GitHub installation и активный vault."""
    ensure_app_user(connection, app_user_id=app_user_id)
    connection.execute(
        "INSERT INTO github_accounts (app_user_id, github_user_id, login) "
        "VALUES (?, ?, ?)",
        (app_user_id, 700 + app_user_id, f"octocat-{app_user_id}"),
    )
    SQLiteGitHubInstallationRepository(connection).replace_for_user(
        app_user_id=app_user_id,
        installation_ids={installation_id},
    )
    return SQLiteObsidianVaultRepository(connection).replace(
        ObsidianVault(
            app_user_id=app_user_id,
            installation_id=installation_id,
            repository_id=repository_id,
            owner=f"owner-{app_user_id}",
            repository=f"notes-{app_user_id}",
            branch="main",
        )
    )


def _note(
    vault_id: int | None,
    *,
    app_user_id: int = 1,
    path: str = "note.md",
    title: str | None = None,
    tags: tuple[str, ...] = (),
    markdown: str = "# Note\nНейтральный текст.",
) -> VaultNote:
    """Создаёт тестовую Markdown-заметку с выбранными метаданными."""
    if vault_id is None:
        raise ValueError("vault_id must be saved")
    return VaultNote(
        app_user_id=app_user_id,
        vault_id=vault_id,
        path=path,
        blob_sha=f"blob-{path}",
        markdown=markdown,
        title=title,
        tags=tags,
    )


if __name__ == "__main__":
    unittest.main()
