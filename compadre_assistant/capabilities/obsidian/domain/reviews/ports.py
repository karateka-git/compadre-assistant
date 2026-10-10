"""Ports подготовки типизированного Obsidian-предложения."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from typing import Protocol

from compadre_assistant.capabilities.obsidian.domain.search.models import VaultSearchResult
from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import ObsidianProposal, ObsidianReviewPlan
from compadre_assistant.capabilities.obsidian.domain.search.entities import ArticleSearchQuery
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultInstruction, VaultNote


@dataclass(frozen=True, slots=True)
class ObsidianPlanningContext:
    """Контекст выбора действия и целевого раздела vault."""

    article: Article
    analysis: ArticleAnalysisResult
    instructions: tuple[VaultInstruction, ...]
    search_result: VaultSearchResult
    note_catalog: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class ObsidianWritingContext:
    """Контекст генерации Markdown после чтения целевой и соседних заметок."""

    article: Article
    analysis: ArticleAnalysisResult
    instructions: tuple[VaultInstruction, ...]
    plan: ObsidianReviewPlan
    target_note: VaultNote | None
    neighboring_notes: tuple[VaultNote, ...]
    relevant_notes: tuple[VaultNote, ...]


class VaultArticleSearch(Protocol):
    """Описывает hybrid retrieval для одной проанализированной статьи."""

    def search(
        self,
        *,
        query: ArticleSearchQuery,
        vault_id: int,
        candidate_limit: int = 20,
        result_limit: int = 10,
    ) -> VaultSearchResult:
        """Возвращает релевантные chunks активного vault."""


class ObsidianProposalGenerator(Protocol):
    """Описывает двухфазную LLM-подготовку изменения vault."""

    def plan(self, context: ObsidianPlanningContext) -> ObsidianReviewPlan:
        """Выбирает `add`, `update` или `skip` и безопасный целевой путь."""

    def write_markdown(self, context: ObsidianWritingContext) -> str:
        """Генерирует полный Markdown после загрузки соседних заметок."""


class ObsidianProposalRepository(Protocol):
    """Хранит pending-предложения и атомарно завершает review workflow."""

    def save_pending(self, proposal: ObsidianProposal) -> ObsidianProposal:
        """Сохраняет предложение, если у статьи нет другого активного цикла."""

    def list_pending(self, app_user_id: int, origin: PendingOrigin) -> tuple[ObsidianProposal, ...]:
        """Возвращает активные предложения только исходного чата."""

    def get_by_id(self, proposal_id: int, app_user_id: int) -> ObsidianProposal | None:
        """Читает конкретную операцию данного владельца."""

    def get_active_for_article(
        self, *, app_user_id: int, article_id: int
    ) -> ObsidianProposal | None:
        """Находит активный цикл одной статьи для dedup."""

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
        """Атомарно проверяет адрес, статус и версию перед выполнением."""

    def release_claim(self, proposal: ObsidianProposal) -> ObsidianProposal | None:
        """Возвращает захваченную операцию в ожидание после внешней ошибки."""

    def get_latest_applied_for_article(
        self,
        *,
        app_user_id: int,
        article_id: int,
    ) -> ObsidianProposal | None:
        """Возвращает последний применённый результат review статьи."""

    def mark_cancelled(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        origin: PendingOrigin,
        version: int,
    ) -> ObsidianProposal | None:
        """Отменяет только текущее pending-предложение пользователя."""

    def mark_conflict(
        self,
        *,
        proposal_id: int,
        app_user_id: int,
        version: int,
    ) -> ObsidianProposal | None:
        """Помечает предложение устаревшим после расхождения GitHub SHA."""

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
        """Атомарно фиксирует GitHub/local result, статью и proposal."""
