"""Детерминированное построение compact search query из анализа статьи."""

from __future__ import annotations

import re
from collections.abc import Iterable

from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.search.entities import ArticleSearchQuery


MAX_SEMANTIC_QUERY_CHARS = 1_500
MAX_LEXICAL_QUERY_CHARS = 500
MAX_TITLE_CHARS = 250
MAX_SUMMARY_CHARS = 900
MAX_TOPICS_CHARS = 300
MAX_LEXICAL_FALLBACK_CHARS = 240

_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_MARKER_PATTERN = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_MARKDOWN_DECORATION_PATTERN = re.compile(r"[*`]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_SUMMARY_HEADINGS = frozenset({"кратко", "краткая сводка", "сводка"})
_TOPICS_HEADINGS = frozenset({"темы", "ключевые темы", "теги"})


class ArticleSearchQueryBuilder:
    """Создаёт semantic и lexical query без нового обращения к LLM.

    Builder использует структурные Markdown-разделы сохранённого анализа. Полный
    ответ LLM никогда не передаётся embedding provider или FTS: каждое поле
    имеет отдельный смысл и жёсткую границу размера.
    """

    def build(
        self,
        *,
        article: Article,
        analysis: ArticleAnalysisResult,
    ) -> ArticleSearchQuery:
        """Строит ограниченный запрос для будущего hybrid retrieval.

        Args:
            article: Сохранённая проанализированная статья.
            analysis: Последний сохранённый LLM-анализ этой статьи.

        Returns:
            Раздельные semantic и lexical представления статьи.

        Raises:
            ValueError: Статья не сохранена или analysis принадлежит другой
                статье либо пользователю.
        """
        if article.id is None:
            raise ValueError("article must be saved before search query building")
        if analysis.article_id != article.id:
            raise ValueError("analysis must belong to requested article")
        if analysis.app_user_id != article.app_user_id:
            raise ValueError("analysis must belong to requested app user")

        sections, preamble = _parse_sections(analysis.result_text)
        title = _truncate(_clean_inline(article.title or ""), MAX_TITLE_CHARS)
        summary = _truncate(
            _section_text(sections, _SUMMARY_HEADINGS),
            MAX_SUMMARY_CHARS,
        )
        topics = _truncate(
            _topics_text(sections, _TOPICS_HEADINGS),
            MAX_TOPICS_CHARS,
        )
        fallback = _clean_inline(" ".join(preamble))
        if not summary:
            summary = _truncate(
                fallback or _first_section_text(sections),
                MAX_SUMMARY_CHARS,
            )

        semantic_text = _labeled_text(
            (
                ("Название", title),
                ("Кратко", summary),
                ("Темы", topics),
            ),
            maximum_chars=MAX_SEMANTIC_QUERY_CHARS,
        )
        lexical_fallback = _truncate(summary, MAX_LEXICAL_FALLBACK_CHARS)
        lexical_text = _plain_text(
            (title, topics or lexical_fallback),
            maximum_chars=MAX_LEXICAL_QUERY_CHARS,
        )
        return ArticleSearchQuery(
            app_user_id=article.app_user_id,
            article_id=article.id,
            analysis_id=analysis.id,
            semantic_text=semantic_text,
            lexical_text=lexical_text,
        )


def _parse_sections(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """Разбирает Markdown headings без требования идеального LLM-формата."""
    sections: dict[str, list[str]] = {}
    preamble: list[str] = []
    current: list[str] = preamble
    for raw_line in text.splitlines():
        heading = _HEADING_PATTERN.match(raw_line)
        if heading is not None:
            name = _normalize_heading(heading.group(1))
            current = sections.setdefault(name, [])
            continue
        line = raw_line.strip()
        if line:
            current.append(line)
    return sections, preamble


def _section_text(
    sections: dict[str, list[str]],
    accepted_headings: frozenset[str],
) -> str:
    """Возвращает очищенный текст первого подходящего раздела."""
    for heading, lines in sections.items():
        if heading in accepted_headings:
            return _clean_inline(" ".join(lines))
    return ""


def _topics_text(
    sections: dict[str, list[str]],
    accepted_headings: frozenset[str],
) -> str:
    """Нормализует bullets раздела тем в компактную строку терминов."""
    for heading, lines in sections.items():
        if heading in accepted_headings:
            topics = [
                _clean_inline(_LIST_MARKER_PATTERN.sub("", line))
                for line in lines
            ]
            return ", ".join(topic for topic in topics if topic)
    return ""


def _first_section_text(sections: dict[str, list[str]]) -> str:
    """Возвращает первый содержательный раздел как fallback старого prompt."""
    for lines in sections.values():
        text = _clean_inline(" ".join(lines))
        if text:
            return text
    return ""


def _normalize_heading(value: str) -> str:
    """Нормализует Markdown heading для сопоставления известных разделов."""
    return _clean_inline(value).rstrip(":. ").casefold()


def _clean_inline(value: str) -> str:
    """Удаляет декоративный Markdown и схлопывает пробельные символы."""
    without_decoration = _MARKDOWN_DECORATION_PATTERN.sub("", value)
    return _WHITESPACE_PATTERN.sub(" ", without_decoration).strip()


def _labeled_text(
    values: Iterable[tuple[str, str]],
    *,
    maximum_chars: int,
) -> str:
    """Собирает непустые labeled fields и применяет общий hard limit."""
    text = "\n".join(f"{label}: {value}" for label, value in values if value)
    return _truncate(text, maximum_chars)


def _plain_text(values: Iterable[str], *, maximum_chars: int) -> str:
    """Объединяет непустые lexical fields без служебных labels."""
    return _truncate("\n".join(value for value in values if value), maximum_chars)


def _truncate(value: str, maximum_chars: int) -> str:
    """Обрезает текст на границе слова без добавления поискового термина."""
    cleaned = value.strip()
    if len(cleaned) <= maximum_chars:
        return cleaned
    prefix = cleaned[:maximum_chars].rstrip()
    boundary = prefix.rfind(" ")
    if boundary > maximum_chars // 2:
        prefix = prefix[:boundary]
    return prefix.rstrip(" ,;:-")
