"""Тесты полного application preflight и предложения Этапа 10.9."""

from __future__ import annotations

from dataclasses import replace
import unittest

from obs_chat_bot.capabilities.obsidian.application.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.application.reviews.proposal import (
    PrepareObsidianReviewCommand,
    PrepareObsidianReviewError,
    PrepareObsidianReviewUseCase,
)
from obs_chat_bot.capabilities.obsidian.application.search.models import VaultSearchResult
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
    ObsidianReviewPlan,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import (
    ObsidianVault,
    VaultInstruction,
    VaultNote,
)


class PrepareObsidianReviewTests(unittest.TestCase):
    """Проверяет порядок plan -> neighbors -> Markdown и безопасные ошибки."""

    def setUp(self) -> None:
        self.article = Article(
            id=7,
            app_user_id=3,
            source_url="https://example.com/article",
            normalized_url="https://example.com/article",
            title="Docker volumes",
            cleaned_text="Полный текст статьи",
            status=ArticleStatus.ANALYZED,
        )
        self.analysis = ArticleAnalysisResult(
            id=9,
            article_id=7,
            app_user_id=3,
            llm_model="model",
            prompt_version="v1",
            result_text="## Кратко\nПро volumes\n## Темы\nDocker, storage",
        )
        self.vault = ObsidianVault(
            id=4,
            app_user_id=3,
            installation_id=11,
            repository_id=12,
            owner="owner",
            repository="vault",
            branch="main",
            head_commit_sha="commit-sha",
            tree_sha="tree-sha",
        )
        self.instructions = [
            VaultInstruction(
                id=1,
                app_user_id=3,
                vault_id=4,
                position=0,
                path="memory-bank/AGENTS.md.txt",
                blob_sha="rules-sha",
                content="Пиши в существующей папке.",
            )
        ]

    def test_add_reads_neighbors_before_writing_and_updates_status(self) -> None:
        """Для add writer получает соседей выбранной существующей папки."""
        notes = [
            _note(1, "Tech/A.md"),
            _note(2, "Tech/C.md"),
            _note(3, "Other/X.md"),
        ]
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.ADD,
                target_path="Tech/B.md",
                reasoning="Отдельный конспект в существующем разделе.",
            ),
            markdown="# Docker volumes\nКонспект",
        )
        repository = ArticleRepositoryFake(self.article)
        use_case = self._use_case(
            article_repository=repository,
            notes=notes,
            generator=generator,
        )

        result = use_case.execute(self._command())

        self.assertEqual(result.proposal.action, ObsidianProposalAction.ADD)
        self.assertEqual(result.proposal.base_commit_sha, "commit-sha")
        self.assertIsNone(result.proposal.target_blob_sha)
        self.assertEqual(
            result.proposal.proposed_markdown,
            "---\ntags:\n  - knowledge-catcher\n---\n# Docker volumes\nКонспект",
        )
        self.assertEqual(
            [note.path for note in generator.writing_context.neighboring_notes],
            ["Tech/A.md", "Tech/C.md"],
        )
        self.assertEqual(repository.article.status, ArticleStatus.NEEDS_OBSIDIAN_REVIEW)

    def test_update_captures_expected_target_blob(self) -> None:
        """Update жёстко связывается с blob прочитанной целевой заметки."""
        target = _note(2, "Tech/Docker.md", blob_sha="target-sha")
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.UPDATE,
                target_path=target.path,
                reasoning="Материал дополняет существующий конспект.",
            ),
            markdown="# Docker\nОбновлённый конспект",
        )
        use_case = self._use_case(notes=[target], generator=generator)

        result = use_case.execute(self._command())

        self.assertEqual(result.proposal.target_blob_sha, "target-sha")
        self.assertEqual(generator.writing_context.target_note, target)
        self.assertIn("--- a/Tech/Docker.md", result.markdown_diff)
        self.assertIn("+++ b/Tech/Docker.md", result.markdown_diff)
        self.assertIn("+Обновлённый конспект", result.markdown_diff)
        self.assertNotIn("knowledge-catcher", result.proposal.proposed_markdown)

    def test_update_preserves_bot_marker_in_pending_and_preview(self) -> None:
        """Pending и diff используют Markdown с восстановленным marker бота."""
        target = _note(
            2,
            "Tech/Bot.md",
            blob_sha="target-sha",
            markdown="---\ntags: [knowledge-catcher, docker]\n---\n# Было",
        )
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.UPDATE,
                target_path=target.path,
                reasoning="Дополнение ботской заметки.",
            ),
            markdown="# Стало\nОбновлённый текст",
        )

        result = self._use_case(notes=[target], generator=generator).execute(
            self._command()
        )

        self.assertIn("knowledge-catcher", result.proposal.proposed_markdown)
        self.assertIn("docker", result.proposal.proposed_markdown)
        self.assertIn("+tags:", result.markdown_diff)
        self.assertIn("+# Стало", result.markdown_diff)

    def test_update_rejects_marker_added_to_user_note(self) -> None:
        """Пользовательская заметка не получает marker из LLM update."""
        target = _note(2, "Tech/User.md", blob_sha="target-sha")
        repository = ArticleRepositoryFake(self.article)
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.UPDATE,
                target_path=target.path,
                reasoning="Обновление пользовательской заметки.",
            ),
            markdown=(
                "---\ntags: [knowledge-catcher]\n---\n"
                "# Пользовательская заметка\nОбновление"
            ),
        )

        with self.assertRaisesRegex(
            PrepareObsidianReviewError,
            "invalid updated-note marker placement",
        ):
            self._use_case(
                article_repository=repository,
                notes=[target],
                generator=generator,
            ).execute(self._command())

        self.assertEqual(repository.article.status, ArticleStatus.ANALYZED)

    def test_update_preserves_existing_tags_when_marker_is_disabled(self) -> None:
        """Режим `off` сохраняет marker через общую защиту исходных tags."""
        target = _note(
            2,
            "Tech/Bot.md",
            blob_sha="target-sha",
            markdown="---\ntags: [knowledge-catcher, docker]\n---\n# Было",
        )
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.UPDATE,
                target_path=target.path,
                reasoning="Дополнение ботской заметки.",
            ),
            markdown="# Стало",
        )

        result = self._use_case(
            notes=[target],
            generator=generator,
            created_note_tag=None,
        ).execute(self._command())

        self.assertIn("knowledge-catcher", result.proposal.proposed_markdown)
        self.assertIn("docker", result.proposal.proposed_markdown)

    def test_add_keeps_markdown_unchanged_when_marker_is_disabled(self) -> None:
        """Отключённая настройка не создаёт frontmatter в add proposal."""
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.ADD,
                target_path="Tech/New.md",
                reasoning="Новая заметка.",
            ),
            markdown="# Новая заметка\nТекст",
        )

        result = self._use_case(
            generator=generator,
            created_note_tag=None,
        ).execute(self._command())

        self.assertEqual(result.proposal.proposed_markdown, "# Новая заметка\nТекст")

    def test_add_rejects_inline_created_note_marker_before_save(self) -> None:
        """Inline-маркер LLM останавливает add до сохранения pending proposal."""
        repository = ArticleRepositoryFake(self.article)
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.ADD,
                target_path="Tech/New.md",
                reasoning="Новая заметка.",
            ),
            markdown="# Новая заметка\n#knowledge-catcher",
        )

        with self.assertRaisesRegex(
            PrepareObsidianReviewError,
            "invalid created-note marker placement",
        ):
            self._use_case(
                article_repository=repository,
                generator=generator,
            ).execute(self._command())

        self.assertEqual(repository.article.status, ArticleStatus.ANALYZED)

    def test_skip_does_not_call_markdown_writer(self) -> None:
        """Skip формирует предложение без фиктивного Markdown или target path."""
        generator = RecordingGenerator(
            plan=ObsidianReviewPlan(
                action=ObsidianProposalAction.SKIP,
                reasoning="Статья не добавляет новых знаний.",
            ),
        )

        result = self._use_case(generator=generator).execute(self._command())

        self.assertIsNone(result.proposal.proposed_markdown)
        self.assertIsNone(generator.writing_context)

    def test_missing_instructions_stops_before_search_and_records_error(self) -> None:
        """Без полного набора правил proposal не создаётся."""
        search = RecordingSearch()
        recorder = ErrorRecorderFake()
        use_case = self._use_case(
            instructions=[],
            search=search,
            error_recorder=recorder,
        )

        with self.assertRaisesRegex(PrepareObsidianReviewError, "instructions"):
            use_case.execute(self._command())

        self.assertEqual(search.calls, 0)
        self.assertEqual(recorder.records[0]["stage"], ProcessingStage.OBSIDIAN_REVIEW)

    def test_unexpected_search_error_is_not_fts_fallback_but_isolated(self) -> None:
        """Unexpected RuntimeError становится ошибкой review, а не ложной выдачей."""
        recorder = ErrorRecorderFake()
        repository = ArticleRepositoryFake(self.article)
        use_case = self._use_case(
            article_repository=repository,
            search=RecordingSearch(error=RuntimeError("broken invariant")),
            error_recorder=recorder,
        )

        with self.assertRaisesRegex(PrepareObsidianReviewError, "RuntimeError"):
            use_case.execute(self._command())

        self.assertEqual(repository.article.status, ArticleStatus.ANALYZED)
        self.assertEqual(recorder.records[0]["error_type"], "RuntimeError")

    def _command(self) -> PrepareObsidianReviewCommand:
        return PrepareObsidianReviewCommand(
            article_id=7,
            analysis=self.analysis,
            app_user_id=3,
            incoming_message_id=20,
        )

    def _use_case(
        self,
        *,
        article_repository=None,
        instructions=None,
        notes=None,
        search=None,
        generator=None,
        error_recorder=None,
        created_note_tag="knowledge-catcher",
    ) -> PrepareObsidianReviewUseCase:
        article_repository = article_repository or ArticleRepositoryFake(self.article)
        return PrepareObsidianReviewUseCase(
            article_repository=article_repository,
            vault_repository=VaultRepositoryFake(self.vault),
            instruction_repository=InstructionRepositoryFake(
                self.instructions if instructions is None else instructions
            ),
            note_repository=NoteRepositoryFake(notes or []),
            search=search or RecordingSearch(),
            generator=generator
            or RecordingGenerator(
                plan=ObsidianReviewPlan(
                    action=ObsidianProposalAction.SKIP,
                    reasoning="Не требуется.",
                )
            ),
            proposal_repository=ProposalRepositoryFake(article_repository),
            error_recorder=error_recorder,
            created_note_tag=created_note_tag,
        )


class ArticleRepositoryFake:
    """Минимальный mutable fake статей."""

    def __init__(self, article: Article) -> None:
        self.article = article

    def get_by_id(self, article_id: int) -> Article | None:
        return self.article if self.article.id == article_id else None

    def update_status(self, article_id: int, status: ArticleStatus) -> Article | None:
        if self.article.id != article_id:
            return None
        self.article = replace(self.article, status=status)
        return self.article


class ProposalRepositoryFake:
    """Сохраняет pending proposal и воспроизводит атомарный перевод статьи."""

    def __init__(self, article_repository: ArticleRepositoryFake) -> None:
        self.article_repository = article_repository

    def save_pending(self, proposal: ObsidianProposal) -> ObsidianProposal:
        self.article_repository.update_status(
            proposal.article_id,
            ArticleStatus.NEEDS_OBSIDIAN_REVIEW,
        )
        return replace(proposal, id=1)


class VaultRepositoryFake:
    """Возвращает активный vault одного пользователя."""

    def __init__(self, vault: ObsidianVault) -> None:
        self.vault = vault

    def get_for_user(self, app_user_id: int) -> ObsidianVault | None:
        return self.vault if self.vault.app_user_id == app_user_id else None


class InstructionRepositoryFake:
    """Возвращает заранее заданные правила vault."""

    def __init__(self, instructions: list[VaultInstruction]) -> None:
        self.instructions = instructions

    def list_for_vault(self, *, app_user_id: int, vault_id: int):
        return list(self.instructions)


class NoteRepositoryFake:
    """Возвращает полный локальный каталог Markdown."""

    def __init__(self, notes: list[VaultNote]) -> None:
        self.notes = notes

    def list_for_vault(self, *, app_user_id: int, vault_id: int):
        return list(self.notes)


class RecordingSearch:
    """Запоминает hybrid-вызов или воспроизводит неожиданную ошибку."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def search(self, *, query, vault_id, candidate_limit=20, result_limit=10):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return VaultSearchResult(
            query=query,
            vault_id=vault_id,
            hits=(),
            lexical_candidates=0,
            vector_candidates=0,
        )


class RecordingGenerator:
    """Фиксирует обе фазы LLM-контекста без внешнего API."""

    def __init__(self, *, plan: ObsidianReviewPlan, markdown: str = "") -> None:
        self.result_plan = plan
        self.markdown = markdown
        self.planning_context = None
        self.writing_context = None

    def plan(self, context):
        self.planning_context = context
        return self.result_plan

    def write_markdown(self, context):
        self.writing_context = context
        return self.markdown


class ErrorRecorderFake:
    """Собирает диагностические записи use case."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)


def _note(
    note_id: int,
    path: str,
    *,
    blob_sha: str | None = None,
    markdown: str | None = None,
) -> VaultNote:
    """Создаёт сохранённую заметку для preflight-тестов."""
    return VaultNote(
        id=note_id,
        app_user_id=3,
        vault_id=4,
        path=path,
        blob_sha=blob_sha or f"sha-{note_id}",
        markdown=markdown or f"# {path}\nСодержимое",
        title=path.rsplit("/", 1)[-1].removesuffix(".md"),
    )


if __name__ == "__main__":
    unittest.main()
