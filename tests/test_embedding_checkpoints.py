"""Проверки checkpoints, возобновления и атомарной готовности semantic index."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from obs_chat_bot.capabilities.obsidian.application.search.errors import EmbeddingProviderError
from obs_chat_bot.capabilities.obsidian.application.search.indexing import VaultEmbeddingIndexer
from obs_chat_bot.capabilities.obsidian.application.search.models import VaultChunkEmbeddingDraft
from obs_chat_bot.data.sqlite.connection import connect_database
from obs_chat_bot.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import SQLiteVaultChunkIndexRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_embedding_index_repository import SQLiteVaultEmbeddingIndexRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import SQLiteVaultSyncLeaseRepository
from obs_chat_bot.capabilities.obsidian.application.vaults.vault_sync import VaultSyncService
from tests.test_vault_embedding_index_repository import _prepare_indexes, _chunker


MARKDOWN = "# A\nПервый раздел.\n## B\nВторой раздел.\n## C\nТретий раздел."


class BatchProvider:
    """Выдаёт по одному vector; сбой имитируется между пакетами."""

    document_model = "doc-model"
    query_model = "query-model"

    def __init__(self, *, fail_at=None, dimension=3, before_batch=None):
        self.calls = []
        self.fail_at = fail_at
        self.dimension = dimension
        self.before_batch = before_batch

    def iter_document_batches(self, texts, *, context=None):
        """Фиксирует только фактически начатые платные пакеты."""
        from obs_chat_bot.capabilities.obsidian.domain.search.entities import EmbeddingVector
        for position, text in enumerate(texts):
            self.calls.append(text)
            if self.before_batch is not None:
                self.before_batch(position)
            if position == self.fail_at:
                raise EmbeddingProviderError("test batch failed")
            yield (EmbeddingVector(model=self.document_model, values=(1.0,) * self.dimension),)


def _update(connection, vault_id, provider, *, guard=None, repository=None):
    """Запускает реальный indexer и SQLite storage с тестовым provider."""
    return VaultEmbeddingIndexer(
        chunk_repository=SQLiteVaultChunkIndexRepository(connection),
        embedding_repository=repository or SQLiteVaultEmbeddingIndexRepository(connection),
        provider=provider,
    ).update(app_user_id=1, vault_id=vault_id,
             chunk_index_signature=_chunker().index_signature, before_write=guard)


class EmbeddingCheckpointsTest(unittest.TestCase):
    """Проверяет долговечность пакетов и запрет публикации неполного поколения."""

    def test_resume_after_reopening_database_only_requests_missing_chunks(self):
        """Успешные пакеты видны другому connection до окончания операции."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as con:
                apply_migrations(con)
                vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                texts = [c.text for c in chunks.list_for_vault(app_user_id=1, vault_id=vault.id)]
                self.assertGreaterEqual(len(texts), 3)

                def observe(position):
                    with connect_database(path) as reader:
                        other = SQLiteVaultEmbeddingIndexRepository(reader)
                        self.assertEqual(len(other.list_for_vault(app_user_id=1, vault_id=vault.id)), position)
                        self.assertIsNone(other.get_state(app_user_id=1, vault_id=vault.id))
                provider = BatchProvider(fail_at=2, before_batch=observe)
                with self.assertRaises(EmbeddingProviderError):
                    _update(con, vault.id, provider)
                self.assertEqual(len(repository.list_for_vault(app_user_id=1, vault_id=vault.id)), 2)
                self.assertIsNotNone(chunks.get_state(app_user_id=1, vault_id=vault.id))
                self.assertFalse(con.in_transaction)
            with connect_database(path) as con:
                provider = BatchProvider()
                result = _update(con, vault.id, provider)
                self.assertEqual(provider.calls, texts[2:])
                self.assertEqual(result.unchanged, 2)
                repository = SQLiteVaultEmbeddingIndexRepository(con)
                self.assertIsNotNone(repository.get_state(app_user_id=1, vault_id=vault.id))
                repeat = BatchProvider()
                _update(con, vault.id, repeat)
                self.assertEqual(repeat.calls, [])

    def test_new_dimension_checkpoints_survive_failure_and_restart(self):
        """После смены dimension новые оплаченные vectors не теряются в смеси старых."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as con:
                apply_migrations(con)
                vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                _update(con, vault.id, BatchProvider(dimension=2))
                source = chunks.list_for_vault(app_user_id=1, vault_id=vault.id)
                with con:
                    con.execute("DELETE FROM obsidian_chunk_embeddings WHERE chunk_id=?", (source[0].id,))
                # Новый chunk получает dimension=3, затем ошибка при пересчёте
                # только прежних vectors. Первый пакет должен пережить retry.
                class FailOld(BatchProvider):
                    def iter_document_batches(self, texts, *, context=None):
                        if source[1].text in texts:
                            raise EmbeddingProviderError("old vectors failed")
                        yield from super().iter_document_batches(texts, context=context)
                provider = FailOld()
                with self.assertRaises(EmbeddingProviderError):
                    _update(con, vault.id, provider)
                self.assertEqual(provider.calls, [source[0].text])
                saved = repository.list_for_vault(app_user_id=1, vault_id=vault.id)
                self.assertEqual([v.chunk_id for v in saved], [source[0].id])
                self.assertEqual(saved[0].dimension, 3)
            with connect_database(path) as con:
                provider = BatchProvider()
                _update(con, vault.id, provider)
                self.assertEqual(provider.calls, [c.text for c in source[1:]])

    def test_finalization_rejects_missing_stale_and_mixed_vectors(self):
        """Полный marker нельзя опубликовать для дырок, новых hashes или профиля."""
        for damage in ("missing", "hash", "dimension", "model", "chunk_marker", "source"):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                with connect_database(Path(directory) / "test.db") as con:
                    apply_migrations(con)
                    vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                    _update(con, vault.id, BatchProvider())
                    ids = {c.id for c in chunks.list_for_vault(app_user_id=1, vault_id=vault.id)}
                    repository.invalidate(app_user_id=1, vault_id=vault.id)
                    with con:
                        if damage == "missing":
                            con.execute("DELETE FROM obsidian_chunk_embeddings WHERE chunk_id=?", (min(ids),))
                        elif damage == "hash":
                            con.execute("UPDATE obsidian_chunk_embeddings SET content_hash='changed'")
                        elif damage == "dimension":
                            con.execute("UPDATE obsidian_chunk_embeddings SET dimension=2, vector=zeroblob(8) WHERE chunk_id=?", (min(ids),))
                        elif damage == "model":
                            con.execute("UPDATE obsidian_chunk_embeddings SET document_model='other'")
                        elif damage == "source":
                            con.execute("UPDATE obsidian_note_chunks SET content_hash='changed' WHERE id=?", (min(ids),))
                        else:
                            con.execute("DELETE FROM obsidian_chunk_index_states")
                    with self.assertRaises(ValueError):
                        repository.complete_generation(
                            app_user_id=1, vault_id=vault.id, current_chunk_ids=ids,
                            chunk_index_signature=_chunker().index_signature,
                            document_model="doc-model", query_model="query-model", dimension=3,
                        )
                    self.assertIsNone(repository.get_state(app_user_id=1, vault_id=vault.id))

    def test_storage_error_stops_requests_without_becoming_provider_error(self):
        """Ошибка второго checkpoint сохраняет первый и не вызывает третий HTTP."""
        with TemporaryDirectory() as directory:
            with connect_database(Path(directory) / "test.db") as con:
                apply_migrations(con)
                vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                class FailingRepository(SQLiteVaultEmbeddingIndexRepository):
                    count = 0
                    def save_batch(self, **values):
                        self.count += 1
                        if self.count == 2:
                            raise sqlite3.OperationalError("test disk failure")
                        return super().save_batch(**values)
                provider = BatchProvider()
                with self.assertRaises(sqlite3.OperationalError):
                    _update(con, vault.id, provider, repository=FailingRepository(con))
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(len(repository.list_for_vault(app_user_id=1, vault_id=vault.id)), 1)
                self.assertIsNone(repository.get_state(app_user_id=1, vault_id=vault.id))

    def test_batch_rejects_changed_source_and_rolls_back_all_items(self):
        """Проверяется весь batch до записи: malformed scope/hash не портит checkpoint."""
        with TemporaryDirectory() as directory:
            with connect_database(Path(directory) / "test.db") as con:
                apply_migrations(con)
                vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                source = chunks.list_for_vault(app_user_id=1, vault_id=vault.id)
                drafts = tuple(VaultChunkEmbeddingDraft(
                    app_user_id=1, vault_id=vault.id, chunk_id=c.id,
                    document_model="doc-model", content_hash=c.content_hash if i == 0 else "stale",
                    values=(1.0, 2.0, 3.0),
                ) for i,c in enumerate(source[:2]))
                with self.assertRaises(ValueError):
                    repository.save_batch(app_user_id=1, vault_id=vault.id, embeddings=drafts,
                                          chunk_index_signature=_chunker().index_signature)
                self.assertEqual(repository.list_for_vault(app_user_id=1, vault_id=vault.id), [])

    def test_incomplete_or_dimension_changing_stream_retains_valid_checkpoint(self):
        """Нарушение пакетного контракта не публикует marker и сохраняет первый пакет."""
        from obs_chat_bot.capabilities.obsidian.domain.search.entities import EmbeddingVector
        for kind in ("short", "empty", "dimension", "model", "extra"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                with connect_database(Path(directory) / "test.db") as con:
                    apply_migrations(con)
                    vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                    class MalformedProvider(BatchProvider):
                        def iter_document_batches(self, texts, *, context=None):
                            """Нарушает контракт после одного корректного checkpoint."""
                            yield (EmbeddingVector(model=self.document_model, values=(1.0, 2.0, 3.0)),)
                            if kind == "short":
                                return
                            if kind == "empty":
                                yield ()
                            elif kind == "extra":
                                yield (EmbeddingVector(model=self.document_model, values=(1.0, 2.0, 3.0)),) * len(texts)
                            else:
                                yield (EmbeddingVector(
                                    model="wrong" if kind == "model" else self.document_model,
                                    values=(1.0, 2.0) if kind == "dimension" else (1.0, 2.0, 3.0),
                                ),)
                    with self.assertRaises(EmbeddingProviderError):
                        _update(con, vault.id, MalformedProvider())
                    self.assertEqual(len(repository.list_for_vault(app_user_id=1, vault_id=vault.id)), 1)
                    self.assertIsNone(repository.get_state(app_user_id=1, vault_id=vault.id))

    def test_lease_loss_after_last_checkpoint_prevents_marker(self):
        """Все vectors могут быть сохранены, но истёкший владелец marker не публикует."""
        with TemporaryDirectory() as directory:
            with connect_database(Path(directory) / "test.db") as con:
                apply_migrations(con)
                vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                valid = [True]
                class ExpiringProvider(BatchProvider):
                    def iter_document_batches(self, texts, *, context=None):
                        """Имитирует потерю lease после последнего HTTP checkpoint."""
                        yield from super().iter_document_batches(texts, context=context)
                        valid[0] = False
                def guard():
                    if not valid[0]:
                        raise RuntimeError("lease expired")
                with self.assertRaisesRegex(RuntimeError, "lease"):
                    _update(con, vault.id, ExpiringProvider(), guard=guard)
                count = len(chunks.list_for_vault(app_user_id=1, vault_id=vault.id))
                self.assertEqual(len(repository.list_for_vault(app_user_id=1, vault_id=vault.id)), count)
                self.assertIsNone(repository.get_state(app_user_id=1, vault_id=vault.id))
                provider = BatchProvider()
                _update(con, vault.id, provider)
                self.assertEqual(provider.calls, [])

    def test_lost_or_expired_lease_blocks_checkpoint_and_marker(self):
        """Guard реального sync service блокирует старого владельца под SQLite lock."""
        for takeover in (False, True):
            with self.subTest(takeover=takeover), TemporaryDirectory() as directory:
                with connect_database(Path(directory) / "test.db") as con:
                    apply_migrations(con)
                    vault, chunks, repository = _prepare_indexes(con, markdown=MARKDOWN)
                    now = [datetime.now(UTC)]
                    leases = SQLiteVaultSyncLeaseRepository(con)
                    service = VaultSyncService(
                        vault_repository=None, note_repository=None, instruction_repository=None,
                        lease_repository=leases, github_gateway=None, chunk_indexer=None,
                        clock=lambda: now[0], lease_duration=timedelta(seconds=1),
                    )
                    def change_lease(position):
                        if position == 1:
                            now[0] += timedelta(seconds=2)
                            if takeover:
                                leases.acquire(app_user_id=1, vault_id=vault.id, owner="new-owner",
                                               now=now[0], expires_at=now[0]+timedelta(minutes=5))
                    provider = BatchProvider(before_batch=change_lease)
                    with self.assertRaisesRegex(RuntimeError, "lease"):
                        service._run_with_lease(vault, now=now[0], operation=lambda: _update(
                            con, vault.id, provider, guard=service._embedding_write_guard))
                    self.assertEqual(len(provider.calls), 2)
                    self.assertEqual(len(repository.list_for_vault(app_user_id=1, vault_id=vault.id)), 1)
                    self.assertIsNone(repository.get_state(app_user_id=1, vault_id=vault.id))
                    if takeover:
                        self.assertEqual(leases.get(app_user_id=1, vault_id=vault.id).owner, "new-owner")
