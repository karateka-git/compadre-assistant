"""OpenAI-compatible adapter двухфазной подготовки Obsidian-предложения."""

from __future__ import annotations

import json
from typing import Any

from obs_chat_bot.capabilities.obsidian.application.reviews.ports import (
    ObsidianPlanningContext,
    ObsidianProposalGenerator,
    ObsidianWritingContext,
)
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposalAction,
    ObsidianReviewPlan,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import VaultInstruction, VaultNote


PLANNING_PROMPT_VERSION = "obsidian-plan-v1"
WRITING_PROMPT_VERSION = "obsidian-write-v1"
MAX_PLANNING_RESPONSE_TOKENS = 600
MAX_WRITING_RESPONSE_TOKENS = 8_000
MAX_ARTICLE_TEXT_CHARS = 24_000
MAX_SEARCH_EXCERPT_CHARS = 1_000
MAX_CATALOG_NOTES = 1_000
MAX_NOTE_CONTEXT_CHARS = 12_000
MAX_TOTAL_NOTE_CONTEXT_CHARS = 48_000


class OpenAIObsidianProposalGenerator(ObsidianProposalGenerator):
    """Выбирает изменение и пишет Markdown через Chat Completions API.

    Args:
        base_url: Базовый URL OpenAI-compatible API.
        api_key: API key LLM-провайдера.
        model: Модель планирования и написания заметки.
        client: Optional готовый SDK-клиент для тестов.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._client = client

    def plan(self, context: ObsidianPlanningContext) -> ObsidianReviewPlan:
        """Возвращает проверенный JSON-план без генерации Markdown."""
        response = self._complete(
            messages=_planning_messages(context),
            max_tokens=MAX_PLANNING_RESPONSE_TOKENS,
        )
        try:
            payload = json.loads(_strip_json_fence(response))
            action = ObsidianProposalAction(str(payload["action"]).strip().lower())
            reasoning = str(payload["reasoning"]).strip()
            raw_path = payload.get("target_path")
            target_path = str(raw_path).strip() if raw_path is not None else None
            if not target_path:
                target_path = None
            return ObsidianReviewPlan(
                action=action,
                reasoning=reasoning,
                target_path=target_path,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("LLM returned an invalid Obsidian review plan") from error

    def write_markdown(self, context: ObsidianWritingContext) -> str:
        """Возвращает полный Markdown после передачи target и соседей."""
        result = self._complete(
            messages=_writing_messages(context),
            max_tokens=MAX_WRITING_RESPONSE_TOKENS,
        ).strip()
        if result.startswith("```") and result.endswith("```"):
            result = _strip_markdown_fence(result)
        if not result:
            raise RuntimeError("LLM returned empty proposed Markdown")
        return result

    def _complete(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> str:
        """Выполняет один Chat Completions запрос и извлекает текст."""
        try:
            response = self._get_client().chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.1,
                max_tokens=max_tokens,
            )
            choice = response.choices[0]
            content = choice.message.content
            if getattr(choice, "finish_reason", None) == "length":
                raise RuntimeError("Obsidian proposal LLM response was truncated")
        except Exception as error:
            if isinstance(error, RuntimeError) and "was truncated" in str(error):
                raise
            raise RuntimeError(
                f"Obsidian proposal LLM request failed: {type(error).__name__}"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Obsidian proposal LLM response is empty")
        return content.strip()

    def _get_client(self) -> Any:
        """Лениво создаёт SDK-клиент, не связывая unit-тесты с `openai`."""
        if self._client is None:
            try:
                from openai import OpenAI
            except ModuleNotFoundError as error:
                raise RuntimeError("openai package is not installed") from error
            self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client


def _planning_messages(context: ObsidianPlanningContext) -> list[dict[str, str]]:
    """Строит prompt выбора действия и целевого пути."""
    rules = _format_instructions(context.instructions)
    hits = "\n\n".join(
        (
            f"PATH: {hit.chunk.note_path}\n"
            f"HEADINGS: {' > '.join(hit.chunk.heading_path) or '-'}\n"
            f"EXCERPT:\n{hit.chunk.text[:MAX_SEARCH_EXCERPT_CHARS]}"
        )
        for hit in context.search_result.hits
    ) or "Совпадений не найдено."
    catalog = "\n".join(
        f"- {path} | {title or 'без заголовка'}"
        for path, title in context.note_catalog[:MAX_CATALOG_NOTES]
    ) or "Vault не содержит заметок."
    article = context.article
    return [
        {
            "role": "system",
            "content": (
                "Ты планируешь изменение Obsidian vault. Строго соблюдай все "
                "правила vault. Верни только JSON-объект без Markdown fence: "
                '{"action":"add|update|skip","target_path":"path.md|null",'
                '"reasoning":"краткое объяснение"}. Для update выбирай только '
                "существующий путь из каталога. Для add выбирай новый .md путь "
                "в существующей подходящей папке; новую папку без прямого основания "
                "в правилах не создавай. Для skip target_path должен быть null."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Версия prompt: {PLANNING_PROMPT_VERSION}\n\n"
                f"ПРАВИЛА VAULT:\n{rules}\n\n"
                f"СТАТЬЯ:\nНазвание: {article.title or 'без заголовка'}\n"
                f"URL: {article.source_url}\n\n"
                f"АНАЛИЗ:\n{context.analysis.result_text}\n\n"
                f"НАЙДЕННЫЕ ФРАГМЕНТЫ:\n{hits}\n\n"
                f"КАТАЛОГ ЗАМЕТОК:\n{catalog}"
            ),
        },
    ]


def _writing_messages(context: ObsidianWritingContext) -> list[dict[str, str]]:
    """Строит prompt полного Markdown после preflight соседних заметок."""
    article = context.article
    note_context = _format_note_context(context)
    return [
        {
            "role": "system",
            "content": (
                "Ты редактируешь Obsidian vault. Строго соблюдай переданные правила "
                "и стиль целевой и соседних заметок. Верни только полный итоговый "
                "Markdown целевой заметки без пояснений и code fence. Не меняй "
                "целевой путь и не выдумывай отсутствующие факты. Не добавляй "
                "служебный тег Knowledge Catcher ни inline, ни во frontmatter: "
                "application добавит его после генерации."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Версия prompt: {WRITING_PROMPT_VERSION}\n"
                f"Действие: {context.plan.action.value}\n"
                f"Целевой путь: {context.plan.target_path}\n"
                f"Причина: {context.plan.reasoning}\n\n"
                f"ПРАВИЛА VAULT:\n{_format_instructions(context.instructions)}\n\n"
                f"СТАТЬЯ:\nНазвание: {article.title or 'без заголовка'}\n"
                f"URL: {article.source_url}\n"
                f"Текст:\n{(article.cleaned_text or '')[:MAX_ARTICLE_TEXT_CHARS]}\n\n"
                f"АНАЛИЗ:\n{context.analysis.result_text}\n\n"
                f"ЦЕЛЕВАЯ И СОСЕДНИЕ ЗАМЕТКИ:\n{note_context}"
            ),
        },
    ]


def _format_instructions(instructions: tuple[VaultInstruction, ...]) -> str:
    """Передаёт полный упорядоченный набор instruction-файлов без усечения."""
    return "\n\n".join(
        f"--- {instruction.path} ---\n{instruction.content}"
        for instruction in instructions
    )


def _format_note_context(context: ObsidianWritingContext) -> str:
    """Формирует bounded контекст, отдавая приоритет target и соседям."""
    ordered = (
        ((context.target_note,) if context.target_note is not None else ())
        + context.neighboring_notes
        + context.relevant_notes
    )
    parts: list[str] = []
    seen: set[str] = set()
    remaining = MAX_TOTAL_NOTE_CONTEXT_CHARS
    for note in ordered:
        if note.path in seen or remaining <= 0:
            continue
        seen.add(note.path)
        is_target = (
            context.target_note is not None
            and note.path == context.target_note.path
        )
        if is_target and len(note.markdown) > remaining:
            raise RuntimeError(
                "Target note is too large for safe complete update context"
            )
        allowed = remaining if is_target else min(MAX_NOTE_CONTEXT_CHARS, remaining)
        markdown = note.markdown[:allowed]
        if len(markdown) < len(note.markdown):
            markdown = (
                markdown.rstrip()
                + "\n\n[Контекст заметки сокращён до безопасного размера.]"
            )
        remaining -= len(markdown)
        parts.append(f"--- {note.path} ---\n{markdown}")
    return "\n\n".join(parts) or "Соседних заметок нет."


def _strip_json_fence(text: str) -> str:
    """Удаляет необязательную JSON-fence некоторых совместимых моделей."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _strip_markdown_fence(text: str) -> str:
    """Удаляет внешнюю fence, сохраняя внутренний Markdown заметки."""
    lines = text.strip().splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return text.strip()
    return "\n".join(lines[1:-1]).strip()
