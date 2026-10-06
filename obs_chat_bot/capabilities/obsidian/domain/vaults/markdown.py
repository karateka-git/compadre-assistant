from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


_WIKILINK_PATTERN = re.compile(r"\[\[([^\[\]]+)\]\]")
_HEADING_PATTERN = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_INLINE_TAG_PATTERN = re.compile(r"(?<![\w/])#([\w/-]+)", re.UNICODE)
_FRONTMATTER_TAGS_PATTERN = re.compile(r"^(tags)(\s*):(.*)$", re.IGNORECASE)
_FRONTMATTER_TAG_VALUE_PATTERN = re.compile(
    r"#?[\w-]+(?:/[\w-]+)*",
    re.UNICODE,
)


@dataclass(frozen=True, slots=True)
class MarkdownMetadata:
    """Содержит извлечённые из Obsidian Markdown метаданные."""

    title: str
    frontmatter: str | None
    tags: tuple[str, ...]
    wikilinks: tuple[str, ...]


def parse_markdown(path: str, markdown: str) -> MarkdownMetadata:
    """Извлекает title, frontmatter, tags и wikilinks из Markdown-заметки.

    Args:
        path: Относительный путь заметки внутри vault.
        markdown: Полный исходный Markdown.

    Returns:
        Нормализованные metadata для локального поиска.
    """
    frontmatter, body = _split_frontmatter(markdown)
    fields = _parse_frontmatter_fields(frontmatter)
    title = fields.get("title") or _first_heading(body)
    if not title:
        title = PurePosixPath(path).stem
    frontmatter_tags = _parse_tags(fields.get("tags"))
    inline_tags = tuple(match.group(1) for match in _INLINE_TAG_PATTERN.finditer(body))
    wikilinks = tuple(
        _normalize_wikilink(match.group(1))
        for match in _WIKILINK_PATTERN.finditer(body)
    )
    return MarkdownMetadata(
        title=title,
        frontmatter=frontmatter,
        tags=_ordered_unique((*frontmatter_tags, *inline_tags)),
        wikilinks=_ordered_unique(value for value in wikilinks if value),
    )


def ensure_frontmatter_tag(markdown: str, tag: str | None) -> str:
    """Добавляет служебный тег в YAML frontmatter без изменения тела заметки.

    Поддерживаются отсутствующий `tags`, scalar, flow sequence и block sequence,
    которые читает текущий Markdown parser. Уже имеющийся тег не дублируется.

    Args:
        markdown: Полный итоговый Markdown, полученный от генератора.
        tag: Имя тега без `#` или `None`, если маркировка отключена.

    Returns:
        Markdown со служебным тегом в frontmatter.

    Raises:
        ValueError: Если генератор поместил служебный тег inline в тело или
            frontmatter содержит неподдерживаемую запись `tags`.
    """
    return _ensure_frontmatter_tag(markdown, tag, reject_inline_conflict=True)


def _ensure_frontmatter_tag(
    markdown: str,
    tag: str | None,
    *,
    reject_inline_conflict: bool,
) -> str:
    """Реализует добавление тега с настраиваемой проверкой inline-конфликта."""
    if tag is None:
        return markdown
    if tag.startswith("#") or _FRONTMATTER_TAG_VALUE_PATTERN.fullmatch(tag) is None:
        raise ValueError("Unsupported frontmatter tag value")

    frontmatter, body = _split_frontmatter(markdown)
    normalized_tag = tag.casefold()
    if reject_inline_conflict and any(
        match.group(1).casefold() == normalized_tag
        for match in _INLINE_TAG_PATTERN.finditer(body)
    ):
        raise ValueError("Created-note tag must not appear inline")

    newline = "\r\n" if "\r\n" in markdown else "\n"
    lines = markdown.splitlines(keepends=True)
    if frontmatter is None:
        if lines and lines[0].strip() == "---":
            raise ValueError("Unsupported frontmatter format")
        return f"---{newline}tags:{newline}  - {tag}{newline}---{newline}{markdown}"

    closing_index = next(
        index
        for index, line in enumerate(lines[1:], start=1)
        if line.strip() == "---"
    )
    tag_line_indices = [
        index
        for index, line in enumerate(lines[1:closing_index], start=1)
        if _FRONTMATTER_TAGS_PATTERN.fullmatch(line.rstrip("\r\n"))
    ]
    if len(tag_line_indices) > 1:
        raise ValueError("Frontmatter contains duplicate tags keys")
    tag_line_index = tag_line_indices[0] if tag_line_indices else None
    if tag_line_index is None:
        lines.insert(closing_index, f"tags:{newline}  - {tag}{newline}")
        return "".join(lines)

    raw_line = lines[tag_line_index].rstrip("\r\n")
    match = _FRONTMATTER_TAGS_PATTERN.fullmatch(raw_line)
    if match is None:
        raise ValueError("Unsupported frontmatter tags format")
    value = match.group(3).strip()
    if value.startswith("[") and value.endswith("]"):
        existing = value[1:-1].strip()
        if existing and any(
            not _is_supported_frontmatter_tag(item)
            for item in existing.split(",")
        ):
            raise ValueError("Unsupported frontmatter tags format")
        if _contains_tag(_parse_tags(value), normalized_tag):
            return markdown
        replacement = f"tags: [{existing + ', ' if existing else ''}{tag}]{newline}"
        lines[tag_line_index] = replacement
        return "".join(lines)
    if value:
        if not _is_supported_frontmatter_tag(value):
            raise ValueError("Unsupported frontmatter tags format")
        if _contains_tag(_parse_tags(value), normalized_tag):
            return markdown
        lines[tag_line_index] = f"tags: [{value}, {tag}]{newline}"
        return "".join(lines)

    insertion_index = tag_line_index + 1
    while insertion_index < closing_index:
        candidate = lines[insertion_index]
        if candidate.strip() and not candidate[:1].isspace():
            break
        if candidate.strip():
            item = candidate.strip()
            if not item.startswith("-") or not _is_supported_frontmatter_tag(
                item[1:]
            ):
                raise ValueError("Unsupported frontmatter tags format")
        insertion_index += 1
    if _contains_tag(
        _parse_tags(_parse_frontmatter_fields(frontmatter).get("tags")),
        normalized_tag,
    ):
        return markdown
    lines.insert(insertion_index, f"  - {tag}{newline}")
    return "".join(lines)


def normalize_updated_markdown(
    source_markdown: str,
    proposed_markdown: str,
    created_note_tag: str | None,
) -> str:
    """Нормализует update, сохраняя исходные frontmatter tags.

    При активной настройке запрещён только служебный inline-marker, а обычные
    inline-теги допустимы. При отключённой настройке запрещены любые новые tags,
    поскольку имя ранее использованного marker неизвестно. Пользовательская
    заметка не может впервые получить известный служебный тег.

    Args:
        source_markdown: Markdown исходной заметки из синхронизированного vault.
        proposed_markdown: Полный Markdown, предложенный LLM.
        created_note_tag: Активное имя служебного тега либо `None`.

    Returns:
        Итоговый Markdown с восстановленными исходными frontmatter tags.

    Raises:
        ValueError: Если LLM нарушила правила marker/tags, исходный или новый
            frontmatter неоднозначен либо исходные tags нельзя безопасно
            восстановить.
    """
    source_frontmatter, source_body = _split_frontmatter(source_markdown)
    proposed_frontmatter, proposed_body = _split_frontmatter(proposed_markdown)
    if source_frontmatter is not None and _frontmatter_tags_key_count(
        source_frontmatter
    ) > 1:
        raise ValueError("Source frontmatter contains duplicate tags keys")
    if proposed_frontmatter is not None and _frontmatter_tags_key_count(
        proposed_frontmatter
    ) > 1:
        raise ValueError("Frontmatter contains duplicate tags keys")
    source_tags = _parse_tags(
        _parse_frontmatter_fields(source_frontmatter).get("tags")
    )
    proposed_tags = _parse_tags(
        _parse_frontmatter_fields(proposed_frontmatter).get("tags")
    )
    source_normalized_tags = {tag.casefold() for tag in source_tags}
    proposed_normalized_tags = {tag.casefold() for tag in proposed_tags}
    source_inline_tags = {
        match.group(1).casefold()
        for match in _INLINE_TAG_PATTERN.finditer(source_body)
    }
    proposed_inline_tags = {
        match.group(1).casefold()
        for match in _INLINE_TAG_PATTERN.finditer(proposed_body)
    }
    if (
        created_note_tag is None
        and proposed_inline_tags - source_inline_tags
    ):
        raise ValueError("Updated Markdown must not introduce inline tags")

    if (
        created_note_tag is None
        and proposed_normalized_tags - source_normalized_tags
    ):
        raise ValueError("Updated Markdown must not introduce frontmatter tags")

    if created_note_tag is not None:
        normalized_marker = created_note_tag.casefold()
        source_has_marker = _contains_tag(source_tags, normalized_marker)
        proposed_has_marker = _contains_tag(proposed_tags, normalized_marker)
        if proposed_has_marker and not source_has_marker:
            raise ValueError("Update must not mark a user-created note")
        if normalized_marker in proposed_inline_tags:
            raise ValueError("Created-note tag must not appear inline")

    normalized = proposed_markdown
    for source_tag in source_tags:
        normalized = _ensure_frontmatter_tag(
            normalized,
            source_tag,
            reject_inline_conflict=False,
        )
    return normalized


def _is_supported_frontmatter_tag(value: str) -> bool:
    """Проверяет один простой YAML tag без попытки пересериализации документа."""
    stripped = value.strip()
    quoted = (
        len(stripped) >= 2
        and stripped[:1] == stripped[-1:]
        and stripped[0] in "\"'"
    )
    if quoted:
        stripped = stripped[1:-1]
    if stripped.startswith("#") and not quoted:
        return False
    return _FRONTMATTER_TAG_VALUE_PATTERN.fullmatch(stripped) is not None


def _frontmatter_tags_key_count(frontmatter: str) -> int:
    """Считает top-level ключи `tags` без учёта регистра."""
    return sum(
        _FRONTMATTER_TAGS_PATTERN.fullmatch(line) is not None
        for line in frontmatter.splitlines()
    )


def _contains_tag(tags: tuple[str, ...], normalized_tag: str) -> bool:
    """Сравнивает Obsidian tags без учёта регистра."""
    return any(existing.casefold() == normalized_tag for existing in tags)


def _split_frontmatter(markdown: str) -> tuple[str | None, str]:
    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, markdown
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[1:index]).rstrip("\r\n"), "".join(lines[index + 1 :])
    return None, markdown


def _parse_frontmatter_fields(frontmatter: str | None) -> dict[str, str]:
    if frontmatter is None:
        return {}
    fields: dict[str, str] = {}
    lines = frontmatter.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if ":" not in line or line[:1].isspace():
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip().casefold()
        value = value.strip().strip("\"'")
        if key == "tags" and not value:
            items: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index].strip()
                if not candidate.startswith("-"):
                    break
                items.append(candidate[1:].strip().strip("\"'"))
                index += 1
            fields[key] = ",".join(items)
            continue
        fields[key] = value
        index += 1
    return fields


def _parse_tags(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    cleaned = value.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return tuple(
        tag
        for item in cleaned.split(",")
        if (tag := item.strip().strip("\"'").removeprefix("#"))
    )


def _first_heading(markdown: str) -> str | None:
    match = _HEADING_PATTERN.search(markdown)
    return match.group(1).strip() if match else None


def _normalize_wikilink(value: str) -> str:
    target = value.split("|", 1)[0].split("#", 1)[0]
    return target.strip()


def _ordered_unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
