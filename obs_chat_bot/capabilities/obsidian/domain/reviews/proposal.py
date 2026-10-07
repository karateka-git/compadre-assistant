"""Сценарий поиска заметок и подготовки изменения Obsidian vault."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import PurePosixPath

from obs_chat_bot.capabilities.obsidian.domain.pending import PendingOrigin
from obs_chat_bot.capabilities.obsidian.domain.articles.ports import (
    ArticleRepository,
    ProcessingErrorRecorder,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.domain.reviews.ports import (
    ObsidianPlanningContext,
    ObsidianProposalRepository,
    ObsidianProposalGenerator,
    ObsidianWritingContext,
    VaultArticleSearch,
)
from obs_chat_bot.capabilities.obsidian.domain.search.article_query import ArticleSearchQueryBuilder
from obs_chat_bot.capabilities.obsidian.domain.search.models import VaultSearchResult
from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import (
    ObsidianVaultRepository,
    VaultInstructionRepository,
    VaultNoteRepository,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.markdown import (
    ensure_frontmatter_tag,
    normalize_updated_markdown,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultNote


LOGGER = logging.getLogger(__name__)
MAX_RELEVANT_NOTES = 5
NEIGHBORS_ON_EACH_SIDE = 2


@dataclass(frozen=True, slots=True)
class PrepareObsidianReviewCommand:
    """Команда подготовки review по сохранённым статье и анализу."""

    article_id: int
    analysis: ArticleAnalysisResult
    app_user_id: int
    origin: PendingOrigin
    incoming_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class PrepareObsidianReviewResult:
    """Возвращает сохранённый proposal, retrieval и понятный Markdown diff."""

    proposal: ObsidianProposal
    search_result: VaultSearchResult
    markdown_diff: str | None = None


class PrepareObsidianReviewError(RuntimeError):
    """Ошибка preflight, retrieval или генерации Obsidian-предложения."""


class PrepareObsidianReviewUseCase:
    """Готовит предложение только после правил, поиска и чтения соседей.

    Двухфазная генерация не позволяет выбрать путь и сразу написать заметку в
    одном непрозрачном шаге. Сначала LLM определяет действие и раздел по правилам,
    каталогу и найденным chunks; затем application-слой загружает полный target и
    соседние заметки и только после этого разрешает генерацию Markdown.
    """

    def __init__(
        self,
        *,
        article_repository: ArticleRepository,
        vault_repository: ObsidianVaultRepository,
        instruction_repository: VaultInstructionRepository,
        note_repository: VaultNoteRepository,
        search: VaultArticleSearch,
        generator: ObsidianProposalGenerator,
        proposal_repository: ObsidianProposalRepository,
        query_builder: ArticleSearchQueryBuilder | None = None,
        error_recorder: ProcessingErrorRecorder | None = None,
        created_note_tag: str | None = "knowledge-catcher",
    ) -> None:
        self._article_repository = article_repository
        self._vault_repository = vault_repository
        self._instruction_repository = instruction_repository
        self._note_repository = note_repository
        self._search = search
        self._generator = generator
        self._proposal_repository = proposal_repository
        self._query_builder = query_builder or ArticleSearchQueryBuilder()
        self._error_recorder = error_recorder
        self._created_note_tag = created_note_tag

    def execute(
        self,
        command: PrepareObsidianReviewCommand,
    ) -> PrepareObsidianReviewResult:
        """Формирует типизированное предложение и переводит статью в review.

        Args:
            command: IDs области пользователя и сохранённый LLM-анализ.

        Returns:
            Предложение вместе с диагностическим результатом retrieval.

        Raises:
            PrepareObsidianReviewError: Если preflight не выполнен, поиск или
                LLM завершились ошибкой либо данные разных пользователей смешаны.
        """
        try:
            return self._execute(command)
        except PrepareObsidianReviewError as error:
            self._record_error(command, error)
            raise
        except Exception as error:
            LOGGER.exception(
                "Obsidian review preparation failed: app_user_id=%s article_id=%s "
                "error_type=%s",
                command.app_user_id,
                command.article_id,
                type(error).__name__,
            )
            wrapped = PrepareObsidianReviewError(
                f"Could not prepare Obsidian review: {type(error).__name__}"
            )
            self._record_error(command, error)
            raise wrapped from error

    def _execute(
        self,
        command: PrepareObsidianReviewCommand,
    ) -> PrepareObsidianReviewResult:
        article = self._article_repository.get_by_id(command.article_id)
        if article is None or article.id is None or article.app_user_id != command.app_user_id:
            raise PrepareObsidianReviewError("Article not found in requested user scope")
        analysis = command.analysis
        if analysis.id is None:
            raise PrepareObsidianReviewError("Article analysis must be saved")
        if analysis.article_id != article.id or analysis.app_user_id != article.app_user_id:
            raise PrepareObsidianReviewError("Analysis does not belong to article scope")

        vault = self._vault_repository.get_for_user(command.app_user_id)
        if vault is None or vault.id is None:
            raise PrepareObsidianReviewError("Active Obsidian vault is not connected")
        if not vault.head_commit_sha or not vault.tree_sha:
            raise PrepareObsidianReviewError("Vault has no synchronized source snapshot")

        instructions = tuple(
            self._instruction_repository.list_for_vault(
                app_user_id=command.app_user_id,
                vault_id=vault.id,
            )
        )
        if not instructions or any(not item.content.strip() for item in instructions):
            raise PrepareObsidianReviewError("Complete vault instructions are required")
        if any(
            item.app_user_id != command.app_user_id or item.vault_id != vault.id
            for item in instructions
        ):
            raise PrepareObsidianReviewError("Vault instructions escaped requested scope")

        query = self._query_builder.build(article=article, analysis=analysis)
        search_result = self._search.search(query=query, vault_id=vault.id)
        notes = tuple(
            self._note_repository.list_for_vault(
                app_user_id=command.app_user_id,
                vault_id=vault.id,
            )
        )
        if any(note.app_user_id != command.app_user_id or note.vault_id != vault.id for note in notes):
            raise PrepareObsidianReviewError("Vault notes escaped requested scope")
        notes_by_path = {note.path: note for note in notes}
        relevant_notes = _relevant_notes(search_result, notes_by_path)
        plan = self._generator.plan(
            ObsidianPlanningContext(
                article=article,
                analysis=analysis,
                instructions=instructions,
                search_result=search_result,
                note_catalog=tuple((note.path, note.title) for note in notes),
            )
        )

        target_note = notes_by_path.get(plan.target_path or "")
        if plan.action is ObsidianProposalAction.ADD and target_note is not None:
            raise PrepareObsidianReviewError("Add target path already exists")
        if plan.action is ObsidianProposalAction.UPDATE and target_note is None:
            raise PrepareObsidianReviewError("Update target path does not exist")

        proposed_markdown: str | None = None
        if plan.action is not ObsidianProposalAction.SKIP:
            proposed_markdown = self._generator.write_markdown(
                ObsidianWritingContext(
                    article=article,
                    analysis=analysis,
                    instructions=instructions,
                    plan=plan,
                    target_note=target_note,
                    neighboring_notes=_neighboring_notes(
                        notes,
                        target_path=plan.target_path or "",
                    ),
                    relevant_notes=relevant_notes,
                )
            ).strip()
            if not proposed_markdown:
                raise PrepareObsidianReviewError("Generator returned empty Markdown")
            if plan.action is ObsidianProposalAction.ADD:
                try:
                    proposed_markdown = ensure_frontmatter_tag(
                        proposed_markdown,
                        self._created_note_tag,
                    )
                except ValueError as error:
                    raise PrepareObsidianReviewError(
                        "Generator returned invalid created-note marker placement"
                    ) from error
            elif plan.action is ObsidianProposalAction.UPDATE and target_note is not None:
                try:
                    proposed_markdown = normalize_updated_markdown(
                        target_note.markdown,
                        proposed_markdown,
                        self._created_note_tag,
                    )
                except ValueError as error:
                    raise PrepareObsidianReviewError(
                        "Generator returned invalid updated-note marker placement"
                    ) from error

        proposal = ObsidianProposal(
            app_user_id=article.app_user_id,
            origin=command.origin,
            article_id=article.id,
            analysis_id=analysis.id,
            vault_id=vault.id,
            action=plan.action,
            reasoning=plan.reasoning,
            target_path=plan.target_path,
            proposed_markdown=proposed_markdown,
            base_commit_sha=vault.head_commit_sha,
            base_tree_sha=vault.tree_sha,
            target_blob_sha=target_note.blob_sha if target_note is not None else None,
        )
        proposal = self._proposal_repository.save_pending(proposal)
        LOGGER.info(
            "Obsidian proposal prepared: proposal_id=%s app_user_id=%s "
            "article_id=%s vault_id=%s action=%s search_mode=%s hits=%s",
            proposal.id,
            article.app_user_id,
            article.id,
            vault.id,
            proposal.action.value,
            search_result.mode.value,
            len(search_result.hits),
        )
        return PrepareObsidianReviewResult(
            proposal=proposal,
            search_result=search_result,
            markdown_diff=(
                _markdown_diff(
                    target_path=proposal.target_path or "",
                    current_markdown=target_note.markdown,
                    proposed_markdown=proposed_markdown or "",
                )
                if proposal.action is ObsidianProposalAction.UPDATE and target_note is not None
                else None
            ),
        )

    def _record_error(
        self,
        command: PrepareObsidianReviewCommand,
        error: Exception,
    ) -> None:
        """Сохраняет безопасную диагностику ошибки review, если port настроен."""
        if self._error_recorder is None:
            return
        self._error_recorder.record(
            article_id=command.article_id,
            app_user_id=command.app_user_id,
            incoming_message_id=command.incoming_message_id,
            stage=ProcessingStage.OBSIDIAN_REVIEW,
            error_type=type(error).__name__,
            error_message=str(error),
        )


def _relevant_notes(
    search_result: VaultSearchResult,
    notes_by_path: dict[str, VaultNote],
) -> tuple[VaultNote, ...]:
    """Возвращает полные заметки лучших chunks без повторов одного path."""
    selected: list[VaultNote] = []
    seen: set[str] = set()
    for hit in search_result.hits:
        path = hit.chunk.note_path
        note = notes_by_path.get(path)
        if note is None or path in seen:
            continue
        selected.append(note)
        seen.add(path)
        if len(selected) >= MAX_RELEVANT_NOTES:
            break
    return tuple(selected)


def _neighboring_notes(
    notes: tuple[VaultNote, ...],
    *,
    target_path: str,
) -> tuple[VaultNote, ...]:
    """Выбирает ближайшие по имени заметки из целевой папки."""
    parent = str(PurePosixPath(target_path).parent)
    if parent == ".":
        parent = ""
    same_folder = sorted(
        (
            note
            for note in notes
            if str(PurePosixPath(note.path).parent) in {parent, "." if not parent else parent}
            and note.path != target_path
        ),
        key=lambda note: note.path.casefold(),
    )
    target_key = target_path.casefold()
    insertion = next(
        (
            index
            for index, note in enumerate(same_folder)
            if note.path.casefold() > target_key
        ),
        len(same_folder),
    )
    start = max(0, insertion - NEIGHBORS_ON_EACH_SIDE)
    end = min(len(same_folder), insertion + NEIGHBORS_ON_EACH_SIDE)
    return tuple(same_folder[start:end])


def _markdown_diff(
    *,
    target_path: str,
    current_markdown: str,
    proposed_markdown: str,
) -> str:
    """Строит стабильный unified diff текущей и предлагаемой заметки."""
    return "".join(
        unified_diff(
            current_markdown.splitlines(keepends=True),
            proposed_markdown.splitlines(keepends=True),
            fromfile=f"a/{target_path}",
            tofile=f"b/{target_path}",
        )
    ).rstrip()
