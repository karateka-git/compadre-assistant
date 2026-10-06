"""Application-сервис полнотекстового поиска по chunks vault."""

from __future__ import annotations

from obs_chat_bot.capabilities.obsidian.application.search.ports import (
    VaultFullTextSearchRepository,
    VaultNoteChunker,
)
from obs_chat_bot.capabilities.obsidian.domain.search.entities import VaultChunkSearchHit


class VaultFullTextSearchService:
    """Ищет chunks с обязательной проверкой текущей chunking signature."""

    def __init__(
        self,
        *,
        repository: VaultFullTextSearchRepository,
        chunker: VaultNoteChunker,
    ) -> None:
        self._repository = repository
        self._chunker = chunker

    def search(
        self,
        *,
        app_user_id: int,
        vault_id: int,
        query: str,
        limit: int = 10,
        article_id: int | None = None,
    ) -> tuple[VaultChunkSearchHit, ...]:
        """Выполняет полнотекстовый поиск в одном пользовательском vault.

        Args:
            app_user_id: Внутренний ID пользователя приложения.
            vault_id: ID активного Obsidian vault.
            query: Обычный текстовый запрос без операторов FTS5.
            limit: Максимальное число результатов от 1 до 100.
            article_id: Неиспользуемый lexical-ветвью ID общего контракта.

        Returns:
            Ранжированные chunks либо пустой tuple для пустого запроса или
            индекса другого поколения.

        Raises:
            ValueError: Если IDs или limit выходят за допустимые границы.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if vault_id <= 0:
            raise ValueError("vault_id must be positive")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not query.strip():
            return ()
        return self._repository.search(
            app_user_id=app_user_id,
            vault_id=vault_id,
            query=query,
            expected_index_signature=self._chunker.index_signature,
            limit=limit,
        )
