"""Интеграционные тесты SQLite embedding-индекса Этапа 10.5."""

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
from obs_chat_bot.capabilities.obsidian.domain.search.errors import EmbeddingProviderError
from obs_chat_bot.capabilities.obsidian.domain.search.indexing import (
    VaultChunkIndexer,
    VaultEmbeddingIndexer,
)
from obs_chat_bot.capabilities.obsidian.domain.search.full_text import VaultFullTextSearchService
from obs_chat_bot.capabilities.obsidian.domain.search.hybrid import VaultHybridSearchService
from obs_chat_bot.capabilities.obsidian.domain.search.vector import VaultVectorSearchService
from obs_chat_bot.capabilities.obsidian.data.chunking.document_note_chunker import DocumentVaultNoteChunker
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.capabilities.obsidian.data.sqlite.embedding_mappers import (
    decode_float32_vector,
    encode_float32_vector,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import (
    SQLiteVaultChunkIndexRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_embedding_index_repository import (
    SQLiteVaultEmbeddingIndexRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_full_text_search_repository import (
    SQLiteVaultFullTextSearchRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository
from obs_chat_bot.capabilities.obsidian.domain.search.entities import ArticleSearchQuery, EmbeddingVector
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault, VaultNote
from tests.sqlite_helpers import ensure_app_user


class RecordingEmbeddingProvider:
    """Возвращает детерминированные vectors и запоминает оплачиваемые texts."""

    def __init__(
        self,
        *,
        document_model: str = "doc-model",
        query_model: str = "query-model",
        fail: bool = False,
    ) -> None:
        self.document_model = document_model
        self.query_model = query_model
        self.fail = fail
        self.document_calls: list[tuple[str, ...]] = []
        self.document_contexts = []
        self.query_calls: list[str] = []

    def iter_document_batches(
        self,
        texts: tuple[str, ...],
        *,
        context=None,
    ):
        """Возвращает трёхмерные vectors с координатой по длине текста."""
        self.document_calls.append(texts)
        self.document_contexts.append(context)
        if self.fail:
            raise EmbeddingProviderError("provider unavailable")
        yield tuple(
            EmbeddingVector(
                model=self.document_model,
                values=(float(len(text)), 0.25, -0.5),
            )
            for text in texts
        )

    def embed_query(self, text: str, *, context=None) -> EmbeddingVector:
        """Возвращает совместимый тестовый query vector."""
        self.query_calls.append(text)
        return EmbeddingVector(model=self.query_model, values=(1.0, 0.25, -0.5))


class VaultEmbeddingIndexRepositoryTest(unittest.TestCase):
    """Проверяет float32 storage, профиль и инкрементальный reuse."""

    def test_initial_index_stores_float32_blob_and_profile(self) -> None:
        """Первая индексация сохраняет vectors и обе модели поколения."""
        with self._database() as connection:
            vault, chunks, embeddings = _prepare_indexes(connection)
            provider = RecordingEmbeddingProvider()
            indexer = VaultEmbeddingIndexer(
                chunk_repository=chunks,
                embedding_repository=embeddings,
                provider=provider,
            )

            update = indexer.update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=_chunker().index_signature,
            )

            saved = embeddings.list_for_vault(app_user_id=1, vault_id=vault.id)
            state = embeddings.get_state(app_user_id=1, vault_id=vault.id)
            raw = connection.execute(
                "SELECT vector, dimension FROM obsidian_chunk_embeddings"
            ).fetchone()
            self.assertEqual(update.embedded, len(saved))
            self.assertEqual(len(provider.document_calls), 1)
            self.assertEqual(provider.document_contexts[0].app_user_id, 1)
            self.assertEqual(provider.document_contexts[0].vault_id, vault.id)
            self.assertEqual(state.document_model, "doc-model")
            self.assertEqual(state.query_model, "query-model")
            self.assertEqual(state.dimension, 3)
            self.assertEqual(len(raw["vector"]), raw["dimension"] * 4)
            self.assertEqual(saved[0].values[1:], (0.25, -0.5))

    def test_second_index_does_not_request_unchanged_chunks(self) -> None:
        """Тот же profile и content hashes не создают новый внешний запрос."""
        with self._database() as connection:
            vault, chunks, embeddings = _prepare_indexes(connection)
            provider = RecordingEmbeddingProvider()
            indexer = VaultEmbeddingIndexer(
                chunk_repository=chunks,
                embedding_repository=embeddings,
                provider=provider,
            )
            signature = _chunker().index_signature
            first = indexer.update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=signature,
            )

            second = indexer.update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=signature,
            )

            self.assertEqual(len(provider.document_calls), 1)
            self.assertEqual(second.embedded, 0)
            self.assertEqual(second.unchanged, first.embedded)

    def test_changed_chunk_only_is_embedded_again(self) -> None:
        """Смена content hash одного сохранённого chunk оплачивает один vector."""
        with self._database() as connection:
            vault, chunks, embeddings = _prepare_indexes(
                connection,
                markdown="# A\nСтабильно.\n## B\nПервый вариант.",
            )
            provider = RecordingEmbeddingProvider()
            indexer = VaultEmbeddingIndexer(
                chunk_repository=chunks,
                embedding_repository=embeddings,
                provider=provider,
            )
            signature = _chunker().index_signature
            indexer.update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=signature,
            )
            notes = SQLiteVaultNoteRepository(connection)
            note = notes.upsert(
                _note(
                    vault.id,
                    markdown="# A\nСтабильно.\n## B\nВторой вариант.",
                )
            )
            chunks.invalidate(app_user_id=1, vault_id=vault.id)
            VaultChunkIndexer(chunker=_chunker(), repository=chunks).index_note(note)
            VaultChunkIndexer(chunker=_chunker(), repository=chunks).mark_current(
                app_user_id=1,
                vault_id=vault.id,
            )

            update = indexer.update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=signature,
            )

            self.assertEqual(len(provider.document_calls), 2)
            self.assertGreater(update.embedded, 0)
            self.assertLess(update.embedded, update.embedded + update.unchanged)

    def test_query_model_change_reuses_document_vectors(self) -> None:
        """Новый query model обновляет профиль без повторной vectorization corpus."""
        with self._database() as connection:
            vault, chunks, embeddings = _prepare_indexes(connection)
            first_provider = RecordingEmbeddingProvider()
            signature = _chunker().index_signature
            VaultEmbeddingIndexer(
                chunk_repository=chunks,
                embedding_repository=embeddings,
                provider=first_provider,
            ).update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=signature,
            )
            second_provider = RecordingEmbeddingProvider(query_model="query-v2")

            update = VaultEmbeddingIndexer(
                chunk_repository=chunks,
                embedding_repository=embeddings,
                provider=second_provider,
            ).update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=signature,
            )

            state = embeddings.get_state(app_user_id=1, vault_id=vault.id)
            self.assertEqual(second_provider.document_calls, [])
            self.assertEqual(update.embedded, 0)
            self.assertEqual(state.query_model, "query-v2")

    def test_provider_failure_leaves_generation_invalid(self) -> None:
        """После внешнего сбоя vectors могут остаться, но marker не публикуется."""
        with self._database() as connection:
            vault, chunks, embeddings = _prepare_indexes(connection)
            indexer = VaultEmbeddingIndexer(
                chunk_repository=chunks,
                embedding_repository=embeddings,
                provider=RecordingEmbeddingProvider(fail=True),
            )

            with self.assertRaises(EmbeddingProviderError):
                indexer.update(
                    app_user_id=1,
                    vault_id=vault.id,
                    chunk_index_signature=_chunker().index_signature,
                )

            self.assertIsNone(embeddings.get_state(app_user_id=1, vault_id=vault.id))

    def test_float32_mapper_rejects_wrong_blob_length(self) -> None:
        """Декодирование не принимает повреждённый или обрезанный BLOB."""
        blob = encode_float32_vector((1.5, -2.25))
        self.assertEqual(decode_float32_vector(blob, dimension=2), (1.5, -2.25))
        with self.assertRaises(ValueError):
            decode_float32_vector(blob[:-1], dimension=2)

    def test_sqlite_embeddings_participate_in_hybrid_retrieval(self) -> None:
        """RRF читает реальные float32 BLOB и FTS rows одного поколения."""
        with self._database() as connection:
            vault, chunks, embeddings = _prepare_indexes(
                connection,
                markdown="# Docker\nКонтейнеры и развёртывание приложения.",
            )
            provider = RecordingEmbeddingProvider()
            chunker = _chunker()
            VaultEmbeddingIndexer(
                chunk_repository=chunks,
                embedding_repository=embeddings,
                provider=provider,
            ).update(
                app_user_id=1,
                vault_id=vault.id,
                chunk_index_signature=chunker.index_signature,
            )
            service = VaultHybridSearchService(
                lexical_search=VaultFullTextSearchService(
                    repository=SQLiteVaultFullTextSearchRepository(connection),
                    chunker=chunker,
                ),
                vector_search=VaultVectorSearchService(
                    chunk_repository=chunks,
                    embedding_repository=embeddings,
                    embedding_provider=provider,
                    chunker=chunker,
                ),
            )

            result = service.search(
                query=ArticleSearchQuery(
                    app_user_id=1,
                    article_id=50,
                    analysis_id=60,
                    semantic_text="Название: Docker\nКратко: Развёртывание контейнеров",
                    lexical_text="Docker контейнеры",
                ),
                vault_id=vault.id,
            )

            self.assertTrue(result.hits)
            self.assertEqual({hit.chunk.app_user_id for hit in result.hits}, {1})
            self.assertEqual({hit.chunk.vault_id for hit in result.hits}, {vault.id})
            self.assertEqual(len(provider.query_calls), 1)
            self.assertEqual(result.hits[0].lexical_rank, 1)
            self.assertEqual(result.hits[0].vector_rank, 1)

    @contextmanager
    def _database(self):
        """Открывает новую development-схему для одного теста."""
        with TemporaryDirectory(prefix="obs-chat-bot-embeddings-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                yield connection


def _prepare_indexes(connection, *, markdown: str = "# Note\nТекст заметки."):
    """Создаёт vault, заметку и актуальное поколение chunks."""
    vault = _prepare_vault(connection)
    note = SQLiteVaultNoteRepository(connection).upsert(
        _note(vault.id, markdown=markdown)
    )
    chunks = SQLiteVaultChunkIndexRepository(connection)
    VaultChunkIndexer(chunker=_chunker(), repository=chunks).rebuild(
        app_user_id=1,
        vault_id=vault.id,
        notes=(note,),
    )
    return vault, chunks, SQLiteVaultEmbeddingIndexRepository(connection)


def _prepare_vault(connection) -> ObsidianVault:
    """Создаёт пользователя, installation и активный vault."""
    ensure_app_user(connection)
    connection.execute(
        "INSERT INTO github_accounts (app_user_id, github_user_id, login) "
        "VALUES (1, 701, 'octocat')"
    )
    SQLiteGitHubInstallationRepository(connection).replace_for_user(
        app_user_id=1,
        installation_ids={101},
    )
    return SQLiteObsidianVaultRepository(connection).replace(
        ObsidianVault(
            app_user_id=1,
            installation_id=101,
            repository_id=501,
            owner="owner",
            repository="notes",
            branch="main",
        )
    )


def _note(vault_id: int | None, *, markdown: str) -> VaultNote:
    """Создаёт тестовую Markdown-заметку."""
    if vault_id is None:
        raise ValueError("vault_id must be saved")
    return VaultNote(
        app_user_id=1,
        vault_id=vault_id,
        path="note.md",
        blob_sha="blob-sha",
        markdown=markdown,
    )


def _chunker() -> DocumentVaultNoteChunker:
    """Собирает chunker с мелкими sections для проверки incremental diff."""
    return DocumentVaultNoteChunker(
        DocumentChunker(
            parsers={DocumentFormat.MARKDOWN: MarkdownDocumentParser()},
            policy=ChunkingPolicy(
                minimum_size_chars=1,
                target_size_chars=40,
                maximum_size_chars=80,
            ),
        )
    )


if __name__ == "__main__":
    unittest.main()
