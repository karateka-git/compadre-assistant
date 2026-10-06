"""Тесты OpenAI-compatible adapter предложения Obsidian."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from obs_chat_bot.capabilities.obsidian.application.reviews.ports import (
    ObsidianPlanningContext,
    ObsidianWritingContext,
)
from obs_chat_bot.capabilities.obsidian.application.search.models import VaultSearchResult
from obs_chat_bot.capabilities.obsidian.data.llm.openai_obsidian_proposal_generator import (
    OpenAIObsidianProposalGenerator,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposalAction,
    ObsidianReviewPlan,
)
from obs_chat_bot.capabilities.obsidian.domain.search.entities import ArticleSearchQuery
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultInstruction, VaultNote


class OpenAIObsidianProposalGeneratorTests(unittest.TestCase):
    """Проверяет строгий plan JSON и отдельный Markdown-вызов."""

    def test_plan_parses_json_fence_and_passes_rules_and_catalog(self) -> None:
        """Совместимая модель может обернуть корректный JSON в fence."""
        client = FakeClient(
            ['```json\n{"action":"update","target_path":"Tech/A.md",'
             '"reasoning":"Дополняет заметку"}\n```']
        )
        generator = _generator(client)

        plan = generator.plan(_planning_context())

        self.assertEqual(plan.action, ObsidianProposalAction.UPDATE)
        self.assertEqual(plan.target_path, "Tech/A.md")
        prompt = client.calls[0]["messages"][1]["content"]
        self.assertIn("memory-bank/AGENTS.md.txt", prompt)
        self.assertIn("Tech/A.md", prompt)

    def test_write_returns_full_markdown_without_outer_fence(self) -> None:
        """Adapter удаляет только ошибочно добавленную внешнюю Markdown-fence."""
        client = FakeClient(["```markdown\n# Заголовок\nТекст\n```"])
        generator = _generator(client)

        markdown = generator.write_markdown(_writing_context())

        self.assertEqual(markdown, "# Заголовок\nТекст")
        prompt = client.calls[0]["messages"][1]["content"]
        self.assertIn("--- Tech/A.md ---", prompt)
        self.assertIn("Соседний стиль", prompt)
        system_prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("Не добавляй служебный тег", system_prompt)

    def test_invalid_plan_is_rejected(self) -> None:
        """Свободный текст LLM не превращается в невалидное изменение vault."""
        generator = _generator(FakeClient(["создай заметку где-нибудь"]))

        with self.assertRaisesRegex(RuntimeError, "invalid Obsidian review plan"):
            generator.plan(_planning_context())


class FakeClient:
    """Имитирует последовательные ответы Chat Completions SDK."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        """Возвращает очередной SDK-подобный response."""
        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def _generator(client: FakeClient) -> OpenAIObsidianProposalGenerator:
    return OpenAIObsidianProposalGenerator(
        base_url="https://llm.example/v1",
        api_key="secret",
        model="model",
        client=client,
    )


def _article() -> Article:
    return Article(
        id=7,
        app_user_id=3,
        source_url="https://example.com/article",
        normalized_url="https://example.com/article",
        title="Заголовок статьи",
        cleaned_text="Полный текст статьи",
    )


def _analysis() -> ArticleAnalysisResult:
    return ArticleAnalysisResult(
        id=9,
        article_id=7,
        app_user_id=3,
        llm_model="model",
        prompt_version="v1",
        result_text="## Кратко\nСводка\n## Темы\nDocker",
    )


def _instruction() -> VaultInstruction:
    return VaultInstruction(
        id=1,
        app_user_id=3,
        vault_id=4,
        position=0,
        path="memory-bank/AGENTS.md.txt",
        blob_sha="sha",
        content="Следуй стилю соседних заметок.",
    )


def _planning_context() -> ObsidianPlanningContext:
    query = ArticleSearchQuery(
        app_user_id=3,
        article_id=7,
        analysis_id=9,
        semantic_text="Заголовок Сводка Docker",
        lexical_text="Заголовок Docker",
    )
    return ObsidianPlanningContext(
        article=_article(),
        analysis=_analysis(),
        instructions=(_instruction(),),
        search_result=VaultSearchResult(
            query=query,
            vault_id=4,
            hits=(),
            lexical_candidates=0,
            vector_candidates=0,
        ),
        note_catalog=(("Tech/A.md", "A"),),
    )


def _writing_context() -> ObsidianWritingContext:
    neighbor = VaultNote(
        id=1,
        app_user_id=3,
        vault_id=4,
        path="Tech/A.md",
        blob_sha="sha",
        markdown="# A\nСоседний стиль",
        title="A",
    )
    return ObsidianWritingContext(
        article=_article(),
        analysis=_analysis(),
        instructions=(_instruction(),),
        plan=ObsidianReviewPlan(
            action=ObsidianProposalAction.ADD,
            target_path="Tech/B.md",
            reasoning="Новая тема.",
        ),
        target_note=None,
        neighboring_notes=(neighbor,),
        relevant_notes=(),
    )


if __name__ == "__main__":
    unittest.main()
