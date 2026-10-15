"""Проверяет и по явному запросу публикует точную версию GitHub Release."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from urllib.parse import quote

from scripts.check_hotfix_pairs import HotfixError, check_release
from scripts.create_release_branch import (
    EXPECTED_REPOSITORY, ReleaseBranchError, checked_main_head, repository_from_origin,
)
from scripts.full_ci_admission import (
    Admission, AdmissionError, MAIN_WORKFLOW, RELEASE_WORKFLOW, SHA_PATTERN,
    select_successful_run,
)
from scripts.full_ci_controller import ControllerError, GitHub, VERSION_PATTERN, tag_commit
from scripts.github_publishing import PublishingError, PublishingGitHub


class PublishError(RuntimeError):
    """Означает, что опубликованная версия не может быть создана безопасно."""


@dataclass(frozen=True)
class PreparedRelease:
    """Хранит проверенные реквизиты будущего выпуска."""

    version: str
    branch: str
    sha: str
    admission: Admission
    pairs: tuple[tuple[int, int, str], ...]
    body: str
    change: str
    notes: str
    make_latest: str


def _sha(value: object, label: str) -> str:
    """Проверяет полный SHA в обязательном поле GitHub API."""
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise PublishError(f"Некорректный SHA: {label}")
    return value


def _version(tag: str) -> tuple[int, int, int]:
    """Разбирает каноническую версию без ведущих нулей."""
    match = VERSION_PATTERN.fullmatch(tag)
    if not match or any(len(part) > 1 and part.startswith("0") for part in match.groups()):
        raise PublishError("Требуется канонический тег vMAJOR.MINOR.PATCH")
    return tuple(map(int, match.groups()))


def _published_versions(api: GitHub) -> dict[tuple[int, int, int], dict]:
    """Получает все опубликованные версии и отвергает неоднозначную историю."""
    versions: dict[tuple[int, int, int], dict] = {}
    for item in api.pages("/releases"):
        if not isinstance(item, dict) or not isinstance(item.get("tag_name"), str):
            raise PublishError("Некорректный список GitHub Releases")
        if item.get("draft") or item.get("prerelease"):
            continue
        version = _version(item["tag_name"])
        if version in versions or not item.get("published_at"):
            raise PublishError("Неоднозначная история опубликованных версий")
        versions[version] = item
    return versions


def _unused_name(api: GitHub, version: str) -> None:
    """Проверяет отсутствие тега и Release, включая draft и prerelease."""
    refs = api.pages("/git/matching-refs/tags/v")
    if any(not isinstance(ref, dict) or not isinstance(ref.get("ref"), str) for ref in refs):
        raise PublishError("Некорректный список Git tags")
    if any(ref["ref"] == f"refs/tags/{version}" for ref in refs):
        raise PublishError("Тег уже существует; версия не переиспользуется")
    releases = api.pages("/releases")
    if any(not isinstance(item, dict) or not isinstance(item.get("tag_name"), str) for item in releases):
        raise PublishError("Некорректный список GitHub Releases")
    if any(item["tag_name"] == version for item in releases):
        raise PublishError("GitHub Release уже существует; версия не переиспользуется")


def _protection(api: GitHub, version: str) -> None:
    """Требует действующей защиты тега и включённой неизменности Release."""
    immutable = api.get("/immutable-releases")
    if not isinstance(immutable, dict) or immutable.get("enabled") is not True:
        raise PublishError("Immutable Releases не включены")
    _tag_protection(api, version)


def _tag_protection(api: GitHub, version: str, *, require_visible_bypass: bool = True) -> None:
    """Требует активный tag ruleset для выбранной версии.

    Args:
        api: Клиент GitHub для чтения правил репозитория.
        version: Проверяемый tag выпуска.
        require_visible_bypass: Требовать явный пустой bypass list. Публичный
            API скрывает это поле; selector компенсирует его проверкой
            immutable Release и точного SHA.
    """
    rulesets = api.pages("/rulesets")
    protected = False
    for summary in rulesets:
        if not isinstance(summary, dict) or type(summary.get("id")) is not int:
            raise PublishError("Некорректный список GitHub rulesets")
        if summary.get("target") != "tag" or summary.get("enforcement") != "active":
            continue
        rule = api.get(f"/rulesets/{summary['id']}")
        if not isinstance(rule, dict) or rule.get("target") != "tag" or rule.get("enforcement") != "active":
            raise PublishError("Некорректное tag ruleset")
        names = rule.get("conditions", {}).get("ref_name", {})
        if (not isinstance(names, dict) or names.get("exclude") != [] or
                not isinstance(names.get("include"), list)):
            raise PublishError("Неоднозначные условия tag ruleset")
        # Принимаем лишь понятные широкие шаблоны; сложный fnmatch не угадываем.
        if not any(pattern in ("refs/tags/*", "refs/tags/v*", f"refs/tags/{version}")
                   for pattern in names["include"]):
            continue
        types = {entry.get("type") for entry in rule.get("rules", []) if isinstance(entry, dict)}
        bypass = rule.get("bypass_actors")
        acceptable_bypass = (bypass == [] if require_visible_bypass else
                             ("bypass_actors" not in rule or bypass == []))
        if {"update", "deletion"} <= types and acceptable_bypass:
            protected = True
    if not protected:
        if require_visible_bypass:
            raise PublishError("Нет активного tag ruleset v* с запретом update/deletion без bypass")
        raise PublishError("Нет активного tag ruleset v* с запретом update/deletion")


def _check_number(parts: tuple[int, int, int], change: str,
                  history: dict[tuple[int, int, int], dict]) -> None:
    """Проверяет последовательность версий внутри поддерживаемой линии."""
    major, minor, patch = parts
    if parts in history:
        raise PublishError("Версия уже опубликована")
    if not history:
        if change != "initial" or parts != (1, 0, 0):
            raise PublishError("Первым выпуском должен быть v1.0.0")
        return
    line = [version for version in history if version[:2] == parts[:2]]
    if line:
        if change != "patch" or patch != max(item[2] for item in line) + 1:
            raise PublishError("Для существующей линии требуется следующий PATCH")
        return
    highest_major, highest_minor, _ = max(history)
    if patch != 0 or not (
        (change == "minor" and major == highest_major and minor == highest_minor + 1) or
        (change == "major" and major == highest_major + 1 and minor == 0)
    ):
        raise PublishError("Новая линия требует следующего MINOR или MAJOR с PATCH=0")


def _candidate(api: GitHub, branch: str, sha: str) -> None:
    """Сверяет локальный чистый checkout и текущий удалённый tip."""
    from scripts.create_release_branch import git
    if repository_from_origin() != EXPECTED_REPOSITORY or api.repository != EXPECTED_REPOSITORY:
        raise PublishError("Репозиторий origin или API не совпадает с проектом")
    if git("status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise PublishError("Для публикации нужен чистый checkout")
    if git("rev-parse", "HEAD").stdout.strip() != sha:
        raise PublishError("HEAD не соответствует candidate SHA")
    ref = api.get(f"/git/ref/heads/{branch}")
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
        raise PublishError("Некорректный ref релизной ветки")
    if _sha(ref["object"].get("sha"), branch) != sha:
        raise PublishError("Candidate не является текущим tip релизной ветки")


def prepare(api: GitHub, version: str, sha: str, change: str, notes: str) -> PreparedRelease:
    """Проверяет номер, защиту, историю, парные PR и полный CI точного SHA.

    Args:
        api: Клиент GitHub API для выбранного репозитория.
        version: Канонический тег vMAJOR.MINOR.PATCH.
        sha: Полный SHA кандидата.
        change: Тип версии initial, major, minor или patch.
        notes: Подготовленное человеком описание изменений.

    Returns:
        Все реквизиты для публикации без скрытых проверок.

    Raises:
        PublishError: Допуск или защита не доказаны.
    """
    major, minor, patch = parts = _version(version)
    if not SHA_PATTERN.fullmatch(sha):
        raise PublishError("Требуется полный 40-символьный SHA")
    if not notes.strip():
        raise PublishError("Release notes не должны быть пустыми")
    reserved = ("Тип выпуска:", "Commit:", "Ветка:", "Полный CI:")
    if any(line.startswith(reserved) for line in notes.splitlines()):
        raise PublishError("Release notes содержат служебное поле манифеста")
    branch = f"release/{major}.{minor}"
    _candidate(api, branch, sha)
    history = _published_versions(api)
    _check_number(parts, change, history)
    _unused_name(api, version)
    _protection(api, version)
    line = [(number, item) for number, item in history.items() if number[:2] == parts[:2]]
    if line:
        _, baseline_release = max(line)
        baseline = tag_commit(api, baseline_release["tag_name"])
        pairs = tuple(check_release(api, branch, sha, baseline))
        admission = select_successful_run(api, api.repository, RELEASE_WORKFLOW, sha, branch)
    else:
        checked_main_head(api, sha)
        pairs = ()
        admission = select_successful_run(api, api.repository, MAIN_WORKFLOW, sha, "main")
    if admission is None:
        raise PublishError("Нет успешного полного CI для точного SHA")
    _candidate(api, branch, sha)
    _unused_name(api, version)
    run_url = f"https://github.com/{api.repository}/actions/runs/{admission.run_id}/attempts/{admission.run_attempt}"
    pair_lines = "\n".join(f"- release #{r}, main #{m}: {method}" for r, m, method in pairs)
    body = (f"{notes.strip()}\n\nТип выпуска: обычный\nCommit: `{sha}`\nВетка: `{branch}`\n"
            f"Полный CI: {run_url}\n")
    if pair_lines:
        body += f"\nПарные PR исправлений:\n{pair_lines}\n"
    make_latest = "true" if not history or parts > max(history) else "false"
    return PreparedRelease(version, branch, sha, admission, pairs, body,
                           change, notes, make_latest)


def publish(api: PublishingGitHub, prepared: PreparedRelease) -> str:
    """Создаёт новый tag ref и затем опубликованный GitHub Release.

    Args:
        api: Клиент GitHub с правом создания тега и Release.
        prepared: Результат свежего допуска.

    Returns:
        Подтверждённая ссылка на опубликованный Release.

    Raises:
        PublishError: Создание или повторная сверка не подтверждены. GitHub
            не объединяет tag ref и Release в одну транзакцию: тег после
            частичного сбоя остаётся занятым и требует ручного разбора.
    """
    if prepare(api, prepared.version, prepared.sha, prepared.change, prepared.notes) != prepared:
        raise PublishError("Допуск изменился после подготовки выпуска")
    tag = api.post("/git/refs", {"ref": f"refs/tags/{prepared.version}", "sha": prepared.sha})
    if tag.get("ref") != f"refs/tags/{prepared.version}" or tag.get("object", {}).get("sha") != prepared.sha:
        raise PublishError("Создание тега не подтверждено; версию разбирать вручную")
    if tag_commit(api, prepared.version) != prepared.sha:
        raise PublishError("Тег не указывает на candidate; Release не создавался")
    _candidate(api, prepared.branch, prepared.sha)
    if prepared.pairs:
        history = _published_versions(api)
        line = [(number, item) for number, item in history.items()
                if number[:2] == _version(prepared.version)[:2]]
        if not line or tuple(check_release(
            api, prepared.branch, prepared.sha, tag_commit(api, max(line)[1]["tag_name"]),
        )) != prepared.pairs:
            raise PublishError("Условия парных PR изменились после создания тега")
    else:
        checked_main_head(api, prepared.sha)
    if select_successful_run(api, api.repository, prepared.admission.workflow,
                             prepared.sha, prepared.admission.branch) != prepared.admission:
        raise PublishError("Допуск CI изменился после создания тега")
    release = api.post("/releases", {
        "tag_name": prepared.version, "target_commitish": prepared.sha,
        "name": prepared.version, "body": prepared.body,
        "draft": False, "prerelease": False, "make_latest": prepared.make_latest,
    })
    if (release.get("tag_name") != prepared.version or release.get("draft") is not False or
            release.get("prerelease") is not False or type(release.get("id")) is not int or
            not isinstance(release.get("html_url"), str)):
        raise PublishError("Создание Release не подтверждено; версию разбирать вручную")
    confirmed = api.get(f"/releases/tags/{quote(prepared.version, safe='')}")
    if (not isinstance(confirmed, dict) or confirmed.get("id") != release["id"] or
            confirmed.get("tag_name") != prepared.version or
            confirmed.get("target_commitish") != prepared.sha or
            confirmed.get("body") != prepared.body or
            confirmed.get("draft") is not False or
            confirmed.get("prerelease") is not False or
            confirmed.get("immutable") is not True or
            not confirmed.get("published_at")):
        raise PublishError("Опубликованный Release не совпадает с подготовленным")
    if tag_commit(api, prepared.version) != prepared.sha:
        raise PublishError("Тег изменился после публикации; выпуск требует ручного разбора")
    return release["html_url"]


def main() -> None:
    """Запускает проверку, dry-run или явную публикацию после допуска."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="новая версия vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный SHA tip релизной ветки")
    parser.add_argument("--change", required=True, choices=("initial", "major", "minor", "patch"))
    parser.add_argument("--notes-file", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="проверить без записи")
    mode.add_argument("--dry-run", action="store_true", help="проверить и показать Release notes")
    mode.add_argument("--publish", action="store_true", help="создать tag и GitHub Release")
    args = parser.parse_args()
    try:
        notes = args.notes_file.read_text(encoding="utf-8")
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        result = prepare(api, args.version, args.sha, args.change, notes)
        print(f"Допуск: {result.version} -> {result.sha}; {result.admission.workflow} "
              f"run={result.admission.run_id}, attempt={result.admission.run_attempt}")
        if args.dry_run:
            print(result.body)
        if args.publish:
            print(f"Опубликовано: {publish(api, result)}")
    except (OSError, AdmissionError, ControllerError, HotfixError, ReleaseBranchError,
            PublishError, PublishingError, KeyError,
            TypeError, ValueError) as error:
        print(f"Release: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
