"""Проверяет локальные ссылки tracked Markdown из корня репозитория.

Учитывает однострочные ссылки и изображения, определения ссылок, заголовки
ATX и Setext. Поддерживает основной синтаксис документации, включая
GFM-таблицы. HTML-ссылки и якоря, заголовки внутри цитат и списков, а также
фрагменты ссылок на файлы других форматов остаются вне проверки.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import unicodedata
from urllib.parse import unquote


_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_COMMENT_OR_CODE = re.compile(
    r"<!--.*?(?:-->|\Z)|(?<![\\`])(?P<ticks>`+)(?!`)(?P<code>.+?)(?<!`)(?P=ticks)(?!`)",
    re.DOTALL,
)
_ATX = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+)$")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_DESTINATION = r"(?:<(?P<angle>[^<>\n]*)>|(?P<bare>(?:\\[^\n]|[^\s()\\]|\([^\s()]*\))+))"
_TITLE = r'''(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\))'''
_LABEL = r"(?:\\.|[^\[\]\n\\]|\[[^\[\]\n]*\])*"
_INLINE_LINK = re.compile(
    rf"(?<!\\)\[(?P<label>{_LABEL})\]\([ \t]*{_DESTINATION}(?:[ \t]+{_TITLE})?[ \t]*\)"
)
_LINK_DEFINITION = re.compile(
    rf"^ {{0,3}}\[[^\]\n]+\]:[ \t]*{_DESTINATION}(?:[ \t]+{_TITLE})?[ \t]*$"
)
_BACKSLASH_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])")
_EMPHASIS = re.compile(
    r"(\*{1,3}|~~)(?=\S)(.+?)(?<=\S)\1|(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\3(?!\w)"
)
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")


@dataclass(frozen=True)
class _Document:
    anchors: set[str]
    links: list[tuple[int, str]]


def _git_paths(root: Path) -> set[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, stdout=subprocess.PIPE
    ).stdout
    return {path.decode("utf-8", errors="surrogateescape") for path in output.split(b"\0") if path}


def _without_fenced_code(source: str) -> list[str]:
    lines = []
    fence = ""
    for line in source.splitlines():
        match = _FENCE.match(line)
        if not fence and match and (not match[1].startswith("`") or "`" not in match[2]):
            fence = match[1]
            lines.append("")
        elif fence:
            if match and match[1].startswith(fence) and not match[2].strip():
                fence = ""
            lines.append("")
        else:
            lines.append(line)
    return lines


def _mask(text: str) -> str:
    # Сохраняем строки и позиции токенов для диагностик и чтения заголовков.
    return re.sub(r"[^\n]", " ", text)


def _heading_slug(heading: str) -> str:
    heading = _INLINE_LINK.sub(r"\g<label>", heading)
    heading = _EMPHASIS.sub(lambda match: match[2] or match[4], heading)
    heading = _COMMENT_OR_CODE.sub(lambda match: match["code"] or "", heading)
    heading = re.sub(r"</?[a-zA-Z][^>]*>", "", heading)
    heading = html.unescape(_BACKSLASH_ESCAPE.sub(r"\1", heading)).strip().lower()
    return "".join(
        "-" if char == " " else char
        for char in heading
        if char in " -_" or unicodedata.category(char)[0] in "LMN"
    )


def _add_anchor(anchors: set[str], heading: str) -> None:
    slug = _heading_slug(heading)
    anchor = slug
    suffix = 0
    while anchor in anchors:
        suffix += 1
        anchor = f"{slug}-{suffix}"
    anchors.add(anchor)


def _destination(match: re.Match[str]) -> str:
    raw = match["angle"] if match["angle"] is not None else match["bare"]
    return html.unescape(_BACKSLASH_ESCAPE.sub(r"\1", raw))


def _parse_document(source: str) -> _Document:
    lines = _without_fenced_code(source)
    masked = _COMMENT_OR_CODE.sub(lambda match: _mask(match[0]), "\n".join(lines)).split("\n")
    anchors: set[str] = set()
    links = []
    previous = ""
    for number, (line, syntax) in enumerate(zip(lines, masked, strict=False), 1):
        # Маскируем inline code при поиске ссылок, но сохраняем его текст в slug.
        visible = syntax.rstrip()
        heading = _ATX.match(line) if visible.lstrip(" ").startswith("#") else None
        setext = previous and _SETEXT.fullmatch(visible)
        if heading:
            _add_anchor(anchors, re.sub(r"[ \t]+#+[ \t]*$", "", heading[1]))
        elif setext:
            _add_anchor(anchors, previous)

        reference = _LINK_DEFINITION.fullmatch(syntax)
        if reference:
            links.append((number, _destination(reference)))
        else:
            links.extend((number, _destination(match)) for match in _INLINE_LINK.finditer(syntax))

        starts_block = visible.lstrip().startswith(("#", "|", ">", "- ", "* ", "+ "))
        if not visible or starts_block or setext or reference:
            previous = ""
        else:
            previous = line
    return _Document(anchors, links)


def _check(root: Path) -> tuple[int, list[str]]:
    paths = _git_paths(root)
    files = {path for path in paths if (root / path).is_file()}
    targets = files | {parent.as_posix() for path in files for parent in PurePosixPath(path).parents}
    documents = {
        path: _parse_document((root / path).read_text(encoding="utf-8-sig"))
        for path in sorted(files)
        if PurePosixPath(path).suffix.lower() in _MARKDOWN_SUFFIXES
    }
    errors = []
    for path, document in documents.items():
        for number, destination in document.links:
            if _EXTERNAL.match(destination):
                continue
            path_and_query, _, fragment = destination.partition("#")
            target_path = unquote(path_and_query.split("?", 1)[0])
            if target_path.startswith("/"):
                resolved = posixpath.normpath(target_path.lstrip("/"))
            elif target_path:
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target_path))
            else:
                resolved = path

            anchor = unquote(fragment)
            if resolved not in targets:
                errors.append(f"{path}:{number}: путь отсутствует в репозитории: {destination}")
            elif anchor and resolved in documents and anchor not in documents[resolved].anchors:
                errors.append(f"{path}:{number}: якорь отсутствует в {resolved}: {destination}")
    return len(documents), errors


def main() -> int:
    """Печатает ошибки ссылок и возвращает 1 при нарушении или 0 при успехе."""
    count, errors = _check(Path.cwd())
    for error in errors:
        print(error, file=sys.stderr)
    print(f"Markdown-файлов проверено: {count}, ошибок: {len(errors)}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
