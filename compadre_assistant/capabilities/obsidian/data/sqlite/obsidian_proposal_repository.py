"""SQLite-хранилище жизненного цикла Obsidian-предложений."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin

from compadre_assistant.capabilities.obsidian.domain.reviews.ports import ObsidianProposalRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.proposal_mappers import (
    proposal_dto_from_domain,
    proposal_dto_from_row,
    proposal_from_dto,
)
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
    ObsidianProposalStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultNote
from compadre_assistant.capabilities.obsidian.domain.vaults.lease_scope import current_vault_lease
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_lease_fence import fence_vault_write


PROPOSAL_COLUMNS = """
    id, origin_channel, origin_chat_id, version, execution_expires_at, app_user_id, article_id, analysis_id, vault_id, action, reasoning,
    target_path, proposed_markdown, base_commit_sha, base_tree_sha,
    target_blob_sha, status, applied_commit_sha, created_at, updated_at,
    completed_at
"""


class PendingObsidianProposalError(RuntimeError):
    """Для этой статьи уже существует активный цикл review."""


class SQLiteObsidianProposalRepository(ObsidianProposalRepository):
    """Сохраняет proposals и завершает review одной SQLite-транзакцией."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save_pending(self, proposal: ObsidianProposal) -> ObsidianProposal:
        """Сохраняет новое pending-предложение с защитой от конкуренции."""
        if proposal.id is not None:
            raise ValueError("new proposal must not contain id")
        if proposal.status is not ObsidianProposalStatus.PENDING:
            raise ValueError("new proposal must be pending")
        dto = proposal_dto_from_domain(proposal)
        try:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT INTO obsidian_proposals (
                        origin_channel, origin_chat_id, app_user_id, article_id, analysis_id, vault_id, action,
                        reasoning, target_path, proposed_markdown,
                        base_commit_sha, base_tree_sha, target_blob_sha, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dto.origin_channel,
                        dto.origin_chat_id,
                        dto.app_user_id,
                        dto.article_id,
                        dto.analysis_id,
                        dto.vault_id,
                        dto.action,
                        dto.reasoning,
                        dto.target_path,
                        dto.proposed_markdown,
                        dto.base_commit_sha,
                        dto.base_tree_sha,
                        dto.target_blob_sha,
                        dto.status,
                    ),
                )
                article_cursor = self._connection.execute(
                    """
                    UPDATE articles
                    SET status = 'needs_obsidian_review',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND app_user_id = ?
                    """,
                    (proposal.article_id, proposal.app_user_id),
                )
                if article_cursor.rowcount != 1:
                    raise ValueError("proposal article escaped requested scope")
        except sqlite3.IntegrityError as error:
            if (
                self.get_active_for_article(
                    app_user_id=proposal.app_user_id, article_id=proposal.article_id
                )
                is not None
            ):
                raise PendingObsidianProposalError(
                    "User already has a pending Obsidian proposal"
                ) from error
            raise
        saved = self._get_by_id(cursor.lastrowid)
        if saved is None:
            raise RuntimeError("Saved Obsidian proposal could not be read")
        return saved

    def list_pending(self, app_user_id: int, origin: PendingOrigin) -> tuple[ObsidianProposal, ...]:
        """Возвращает только активные предложения указанного исходного чата."""
        rows = self._connection.execute(
            f"""SELECT {PROPOSAL_COLUMNS} FROM obsidian_proposals
            WHERE app_user_id = ? AND origin_channel = ? AND origin_chat_id = ?
              AND status IN ('pending', 'executing') ORDER BY id""",
            (app_user_id, origin.channel, origin.chat_id),
        ).fetchall()
        return tuple(_proposal_from_row(row) for row in rows)

    def get_by_id(self, proposal_id: int, app_user_id: int) -> ObsidianProposal | None:
        """Читает конкретную операцию в области её внутреннего владельца."""
        row = self._connection.execute(
            f"SELECT {PROPOSAL_COLUMNS} FROM obsidian_proposals WHERE id = ? AND app_user_id = ?",
            (proposal_id, app_user_id),
        ).fetchone()
        return _proposal_from_row(row) if row is not None else None

    def get_active_for_article(
        self, *, app_user_id: int, article_id: int
    ) -> ObsidianProposal | None:
        """Находит активный цикл статьи для dedup, без смены исходного чата."""
        row = self._connection.execute(
            f"""SELECT {PROPOSAL_COLUMNS} FROM obsidian_proposals
            WHERE app_user_id = ? AND article_id = ? AND status IN ('pending', 'executing')""",
            (app_user_id, article_id),
        ).fetchone()
        return _proposal_from_row(row) if row is not None else None

    def claim(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        origin: PendingOrigin,
        version: int,
        now: datetime,
        expires_at: datetime,
    ) -> ObsidianProposal | None:
        """Атомарно проверяет адрес и версию и захватывает выполнение.

        Истёкший execution claim можно восстановить только с актуальной версией.
        Для записи caller предварительно захватывает общий lease vault.

        Args:
            proposal_id: Устойчивый ID операции.
            app_user_id: Внутренний владелец операции.
            origin: Исходный адрес, проверенный по текущему событию.
            version: Версия, показанная пользователю при выборе решения.
            now: Текущее UTC-время проверки execution claim.
            expires_at: Срок нового claim, после которого допустимо восстановление.

        Returns:
            Захваченная операция с новой версией либо `None` при несовпадении CAS.
        """
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE obsidian_proposals
                SET status = 'executing', version = version + 1, execution_expires_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND app_user_id = ? AND origin_channel = ? AND origin_chat_id = ?
                    AND version = ? AND (status = 'pending' OR
                      (status = 'executing' AND execution_expires_at <= ?))""",
                (
                    _format_timestamp(expires_at),
                    proposal_id,
                    app_user_id,
                    origin.channel,
                    origin.chat_id,
                    version,
                    _format_timestamp(now),
                ),
            )
        return self.get_by_id(proposal_id, app_user_id) if cursor.rowcount else None

    def release_claim(self, proposal: ObsidianProposal) -> ObsidianProposal | None:
        """Возвращает незавершённую операцию в pending после ошибки внешней записи."""
        with self._connection:
            fence_vault_write(self._connection, proposal.app_user_id, proposal.vault_id)
            cursor = self._connection.execute(
                """UPDATE obsidian_proposals SET status = 'pending', version = version + 1,
                    execution_expires_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND app_user_id = ? AND status = 'executing' AND version = ?""",
                (proposal.id, proposal.app_user_id, proposal.version),
            )
        return self.get_by_id(proposal.id or 0, proposal.app_user_id) if cursor.rowcount else None

    def get_latest_applied_for_article(
        self,
        *,
        app_user_id: int,
        article_id: int,
    ) -> ObsidianProposal | None:
        """Возвращает последний применённый результат статьи в user scope."""
        row = self._connection.execute(
            f"""
            SELECT {PROPOSAL_COLUMNS} FROM obsidian_proposals
            WHERE app_user_id = ? AND article_id = ? AND status = 'applied'
            ORDER BY id DESC LIMIT 1
            """,
            (app_user_id, article_id),
        ).fetchone()
        return _proposal_from_row(row) if row is not None else None

    def mark_cancelled(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        origin: PendingOrigin,
        version: int,
    ) -> ObsidianProposal | None:
        """Отменяет предложение только пока оно ожидает ответа."""
        return self._finish_without_apply(
            proposal_id=proposal_id,
            app_user_id=app_user_id,
            status=ObsidianProposalStatus.CANCELLED,
            origin=origin,
            version=version,
        )

    def mark_conflict(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        version: int,
    ) -> ObsidianProposal | None:
        """Закрывает устаревшее предложение без изменения статьи и vault."""
        return self._finish_without_apply(
            proposal_id=proposal_id,
            app_user_id=app_user_id,
            status=ObsidianProposalStatus.CONFLICT,
            version=version,
        )

    def complete_applied(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        version: int,
        applied_commit_sha: str | None,
        head_commit_sha: str | None,
        tree_sha: str | None,
        note: VaultNote | None,
    ) -> ObsidianProposal | None:
        """Атомарно фиксирует local snapshot и завершает article review."""
        now = _format_timestamp(datetime.now(UTC))
        with self._connection:
            if note is not None:
                fence_vault_write(self._connection, app_user_id, note.vault_id)
            pending = self._connection.execute(
                """
                SELECT action, article_id, vault_id FROM obsidian_proposals
                WHERE id = ? AND app_user_id = ? AND status = 'executing' AND version = ?
                """,
                (proposal_id, app_user_id, version),
            ).fetchone()
            if pending is None:
                return None
            action = ObsidianProposalAction(pending["action"])
            if action is ObsidianProposalAction.SKIP:
                if any(
                    value is not None
                    for value in (
                        applied_commit_sha,
                        head_commit_sha,
                        tree_sha,
                        note,
                    )
                ):
                    raise ValueError("skip completion must not contain GitHub result")
            else:
                if (
                    applied_commit_sha is None
                    or head_commit_sha is None
                    or tree_sha is None
                    or note is None
                ):
                    raise ValueError("add/update completion requires GitHub result")
                if note.app_user_id != app_user_id or note.vault_id != pending["vault_id"]:
                    raise ValueError("committed note escaped proposal scope")
                self._upsert_note(note)
                vault_cursor = self._connection.execute(
                    """
                    UPDATE obsidian_vaults
                    SET head_commit_sha = ?, tree_sha = ?, head_etag = NULL,
                        last_checked_at = ?, last_synced_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE app_user_id = ? AND id = ?
                    """,
                    (
                        head_commit_sha,
                        tree_sha,
                        now,
                        now,
                        app_user_id,
                        note.vault_id,
                    ),
                )
                if vault_cursor.rowcount != 1:
                    raise ValueError("proposal vault escaped requested scope")
                self._connection.execute(
                    """
                    DELETE FROM obsidian_chunk_index_states
                    WHERE app_user_id = ? AND vault_id = ?
                    """,
                    (app_user_id, note.vault_id),
                )
                self._connection.execute(
                    """
                    DELETE FROM obsidian_embedding_index_states
                    WHERE app_user_id = ? AND vault_id = ?
                    """,
                    (app_user_id, note.vault_id),
                )
            article_cursor = self._connection.execute(
                """
                UPDATE articles SET status = 'reviewed', cleaned_text = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND app_user_id = ?
                """,
                (pending["article_id"], app_user_id),
            )
            if article_cursor.rowcount != 1:
                raise ValueError("proposal article escaped requested scope")
            cursor = self._connection.execute(
                """
                UPDATE obsidian_proposals
                SET status = 'applied', version = version + 1, execution_expires_at = NULL, applied_commit_sha = ?,
                    completed_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND app_user_id = ? AND status = 'executing' AND version = ?
                """,
                (applied_commit_sha, now, proposal_id, app_user_id, version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Pending proposal changed during completion")
        return self._get_by_id(proposal_id)

    def _finish_without_apply(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        status: ObsidianProposalStatus,
        version: int,
        origin: PendingOrigin | None = None,
    ) -> ObsidianProposal | None:
        now = _format_timestamp(datetime.now(UTC))
        expected_status = "pending" if origin is not None else "executing"
        address_clause = " AND origin_channel = ? AND origin_chat_id = ?" if origin else ""
        params = (status.value, now, proposal_id, app_user_id, expected_status, version)
        if origin is not None:
            params += (origin.channel, origin.chat_id)
        with self._connection:
            scope = current_vault_lease()
            if origin is None and scope is not None:
                fence_vault_write(self._connection, app_user_id, scope.vault_id)
            cursor = self._connection.execute(
                """UPDATE obsidian_proposals SET status = ?, version = version + 1,
                    execution_expires_at = NULL, completed_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND app_user_id = ? AND status = ? AND version = ?""" + address_clause,
                params,
            )
        return self._get_by_id(proposal_id) if cursor.rowcount else None

    def _get_by_id(self, proposal_id: int | None) -> ObsidianProposal | None:
        if proposal_id is None:
            return None
        row = self._connection.execute(
            f"SELECT {PROPOSAL_COLUMNS} FROM obsidian_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        return _proposal_from_row(row) if row is not None else None

    def _upsert_note(self, note: VaultNote) -> None:
        """Обновляет Markdown и metadata внутри внешней review-транзакции."""
        self._connection.execute(
            """
            INSERT INTO obsidian_notes (
                app_user_id, vault_id, path, blob_sha, title, markdown, frontmatter
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vault_id, path) DO UPDATE SET
                blob_sha = excluded.blob_sha, title = excluded.title,
                markdown = excluded.markdown, frontmatter = excluded.frontmatter,
                updated_at = CURRENT_TIMESTAMP
            WHERE obsidian_notes.app_user_id = excluded.app_user_id
            """,
            (
                note.app_user_id,
                note.vault_id,
                note.path,
                note.blob_sha,
                note.title,
                note.markdown,
                note.frontmatter,
            ),
        )
        row = self._connection.execute(
            """
            SELECT id FROM obsidian_notes
            WHERE app_user_id = ? AND vault_id = ? AND path = ?
            """,
            (note.app_user_id, note.vault_id, note.path),
        ).fetchone()
        if row is None:
            raise RuntimeError("Committed note could not be read")
        note_id = row["id"]
        self._connection.execute(
            "DELETE FROM obsidian_note_tags WHERE note_id = ?",
            (note_id,),
        )
        self._connection.execute(
            "DELETE FROM obsidian_note_wikilinks WHERE note_id = ?",
            (note_id,),
        )
        self._connection.executemany(
            """
            INSERT INTO obsidian_note_tags (app_user_id, note_id, tag, position)
            VALUES (?, ?, ?, ?)
            """,
            ((note.app_user_id, note_id, tag, position) for position, tag in enumerate(note.tags)),
        )
        self._connection.executemany(
            """
            INSERT INTO obsidian_note_wikilinks (
                app_user_id, note_id, target, position
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (note.app_user_id, note_id, target, position)
                for position, target in enumerate(note.wikilinks)
            ),
        )


def _proposal_from_row(row: sqlite3.Row) -> ObsidianProposal:
    """Преобразует SQLite row в доменное предложение."""
    return proposal_from_dto(proposal_dto_from_row(row))


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
