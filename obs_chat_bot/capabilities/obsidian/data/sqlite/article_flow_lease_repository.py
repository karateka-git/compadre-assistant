"""Межпроцессная координация полного article flow одной ссылки."""

from datetime import datetime
import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.articles.ports import ArticleFlowLeaseRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_mappers import format_utc_timestamp


class SQLiteArticleFlowLeaseRepository(ArticleFlowLeaseRepository):
    """Ограничивает dedup областью пользователя и нормализованного URL."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def acquire(
        self,
        *,
        app_user_id: int,
        normalized_url: str,
        owner: str,
        now: datetime,
        expires_at: datetime
    ) -> bool:
        """Атомарно резервирует цикл без блокирования других статей пользователя."""
        with self._connection:
            cursor = self._connection.execute(
                """INSERT INTO obsidian_article_flow_leases (app_user_id, normalized_url, owner, expires_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(app_user_id, normalized_url) DO UPDATE
                SET owner = excluded.owner, expires_at = excluded.expires_at
                WHERE obsidian_article_flow_leases.expires_at <= ?""",
                (
                    app_user_id,
                    normalized_url,
                    owner,
                    format_utc_timestamp(expires_at),
                    format_utc_timestamp(now),
                ),
            )
        return cursor.rowcount == 1

    def release(self, *, app_user_id: int, normalized_url: str, owner: str) -> None:
        """Снимает только собственный claim обработки ссылки."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM obsidian_article_flow_leases WHERE app_user_id = ? AND normalized_url = ? AND owner = ?",
                (app_user_id, normalized_url, owner),
            )
