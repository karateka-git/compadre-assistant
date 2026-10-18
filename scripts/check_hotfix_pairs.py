"""Проверяет выбранные PR исправления и состояние их слияния."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from scripts.create_release_branch import EXPECTED_REPOSITORY, repository_from_origin
from scripts.full_ci_admission import (
    MAIN_WORKFLOW, RELEASE_BRANCH_PATTERN, RELEASE_WORKFLOW, SHA_PATTERN,
    select_successful_run,
)
from scripts.full_ci_controller import GitHub, VERSION_PATTERN, tag_commit


LINK_MARKER = "Hotfix-Links:"
PULL_URL = f"https://github.com/{EXPECTED_REPOSITORY}/pull/"
PULL_URL_PATTERN = re.compile(rf"{re.escape(PULL_URL)}([1-9]\d*)\Z")
RELEASE_LINK_PATTERN = re.compile(r"release/([1-9]\d*)\.([0-9]|[1-9]\d*)\Z")


class HotfixError(RuntimeError):
    """Означает, что связь PR или допуск к публикации не доказаны."""


def git(*arguments: str) -> str:
    """Выполняет локальную Git-команду и возвращает её stdout."""
    result = subprocess.run(
        ["git", *arguments], capture_output=True, text=True,
    )
    if result.returncode:
        raise HotfixError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout.strip()


def require_repository(api: GitHub) -> None:
    """Сверяет локальный origin с репозиторием GitHub API."""
    if api.repository != EXPECTED_REPOSITORY or repository_from_origin() != EXPECTED_REPOSITORY:
        raise HotfixError("Репозиторий API и origin не совпадают с проектом")


def pull(api: GitHub, number: int) -> dict:
    """Читает PR и проверяет обязательные поля ответа GitHub."""
    value = api.get(f"/pulls/{number}")
    if not isinstance(value, dict) or value.get("number") != number:
        raise HotfixError(f"Некорректный PR #{number}")
    if not isinstance(value.get("body"), str) or not isinstance(value.get("base"), dict):
        raise HotfixError(f"Нет описания или целевой ветки PR #{number}")
    return value


def body_links(pr: dict) -> tuple[int, ...]:
    """Читает единственный блок точных GitHub PR URL из текущего описания.

    Args:
        pr: Прочитанный по API объект pull request.

    Returns:
        Номера PR в порядке URL в блоке.

    Raises:
        HotfixError: Блок отсутствует, содержит лишние строки или неверный URL.
    """
    lines = pr["body"].splitlines()
    if lines.count(LINK_MARKER) != 1:
        raise HotfixError(f"PR #{pr['number']}: требуется один блок Hotfix-Links")
    index = lines.index(LINK_MARKER) + 1
    urls = []
    for line in lines[index:]:
        if not line.strip():
            break
        urls.append(line.strip())
    numbers = []
    for url in urls:
        match = PULL_URL_PATTERN.fullmatch(url)
        if not match:
            raise HotfixError(f"Неверный полный URL PR в описании #{pr['number']}")
        numbers.append(int(match[1]))
    if not numbers or len(numbers) != len(set(numbers)) or pr["number"] in numbers:
        raise HotfixError(f"PR #{pr['number']}: неполный или повторённый Hotfix-Links")
    return tuple(numbers)


def linked_group(api: GitHub, number: int) -> tuple[dict, tuple[dict, ...]]:
    """Читает main и выбранные release PR с взаимными URL в описаниях.

    Args:
        api: Клиент чтения GitHub.
        number: Номер PR из группы.

    Returns:
        Main PR и release PR в порядке возрастания номера релизной линии.

    Raises:
        HotfixError: Ссылки или целевые ветки неполны.
    """
    current = pull(api, number)
    if current["base"].get("ref") == "main":
        main = current
    else:
        linked = [pull(api, item) for item in body_links(current)]
        candidates = [pr for pr in linked if pr["base"].get("ref") == "main"]
        if len(candidates) != 1:
            raise HotfixError("В описании release PR нужна одна ссылка на main PR")
        main = candidates[0]
    if main["base"].get("ref") != "main":
        raise HotfixError("Ссылка не указывает на PR в main")
    selected = body_links(main)
    releases = tuple(pull(api, item) for item in selected)
    branches = []
    for release in releases:
        branch = release["base"].get("ref")
        match = RELEASE_LINK_PATTERN.fullmatch(branch) if isinstance(branch, str) else None
        if not match:
            raise HotfixError(f"PR #{release['number']}: неверная релизная ветка")
        branches.append((int(match[1]), int(match[2])))
    if len(branches) != len(set(branches)) or number not in {main["number"], *selected}:
        raise HotfixError("Выбранные PR не образуют однозначную группу")
    members = {main["number"], *selected}
    for pr in (main, *releases):
        links = body_links(pr)
        expected = members - {pr["number"]}
        if len(links) != len(expected) or set(links) != expected:
            raise HotfixError(f"PR #{pr['number']}: нет взаимных URL всех выбранных PR")
    return main, tuple(pr for _, pr in sorted(zip(branches, releases)))


def linked_pulls(api: GitHub, number: int) -> tuple[dict, dict]:
    """Возвращает release PR и выбранный main PR после полной проверки группы.

    Args:
        api: Читающий клиент GitHub API.
        number: Номер любого PR из пары.

    Returns:
        PR релизной ветки и PR в main.

    Raises:
        HotfixError: Связь отсутствует, неоднозначна или направлена не в те ветки.
    """
    main, releases = linked_group(api, number)
    matches = [pr for pr in releases if pr["number"] == number]
    if len(matches) != 1:
        raise HotfixError("Ожидался выбранный release PR")
    return matches[0], main


def check_links(api: GitHub, number: int, expected_branch: str) -> tuple[int, tuple[int, ...]]:
    """Проверяет ссылки и целевые ветки до слияния любого PR группы."""
    main, releases = linked_group(api, number)
    current = next((pr for pr in (main, *releases) if pr["number"] == number), None)
    assert current is not None
    if current["base"]["ref"] != expected_branch:
        raise HotfixError("Целевая ветка события не совпадает с PR")
    return main["number"], tuple(pr["number"] for pr in releases)


def check_pr(api: GitHub, number: int, expected_branch: str) -> tuple[int, tuple[int, ...]]:
    """Проверяет живые ссылки и слияние зависимостей release PR."""
    main, releases = linked_group(api, number)
    current = next((pr for pr in (main, *releases) if pr["number"] == number), None)
    assert current is not None
    if current["base"]["ref"] != expected_branch:
        raise HotfixError("Целевая ветка события не совпадает с PR")
    if number != main["number"]:
        _merged_reachable(api, main, "main")
        for previous in releases:
            if previous["number"] == number:
                break
            _merged_reachable(api, previous, previous["base"]["ref"])
    return main["number"], tuple(pr["number"] for pr in releases)


def merge_pull(api: GitHub, sha: str, branch: str) -> dict:
    """Находит единственный слитый PR для merge commit заданной ветки."""
    matches = [pr for pr in api.pages(f"/commits/{sha}/pulls")
               if isinstance(pr, dict) and pr.get("merged_at")
               and pr.get("merge_commit_sha") == sha
               and isinstance(pr.get("base"), dict) and pr["base"].get("ref") == branch]
    if len(matches) != 1:
        raise HotfixError(f"Для {sha} нет единственного слитого PR в {branch}")
    return pull(api, matches[0]["number"])


def reachable(api: GitHub, sha: str, branch: str) -> bool:
    """Подтверждает принадлежность SHA текущей истории ветки через GitHub."""
    ref = api.get(f"/git/ref/heads/{branch}")
    if (not isinstance(ref, dict) or ref.get("ref") != f"refs/heads/{branch}" or
            not isinstance(ref.get("object"), dict)):
        raise HotfixError(f"Некорректный ref {branch}")
    head = ref["object"].get("sha")
    if not isinstance(head, str) or not SHA_PATTERN.fullmatch(head):
        raise HotfixError(f"Некорректный head {branch}")
    if sha == head:
        return True
    comparison = api.get(f"/compare/{sha}...{head}")
    return (isinstance(comparison, dict) and comparison.get("status") == "ahead"
            and isinstance(comparison.get("base_commit"), dict)
            and comparison["base_commit"].get("sha") == sha
            and isinstance(comparison.get("head_commit"), dict)
            and comparison["head_commit"].get("sha") == head)


def _merged_reachable(api: GitHub, pr: dict, branch: str) -> str:
    """Проверяет merge commit PR и его достижимость в текущей ветке."""
    sha = pr.get("merge_commit_sha")
    if (pr.get("merged_at") is None or not isinstance(sha, str) or
            not SHA_PATTERN.fullmatch(sha) or pr["base"].get("ref") != branch):
        raise HotfixError(f"PR #{pr['number']} ещё не слит в {branch}")
    if merge_pull(api, sha, branch)["number"] != pr["number"] or not reachable(api, sha, branch):
        raise HotfixError(f"Merge PR #{pr['number']} не подтверждён в {branch}")
    return sha


def check_baseline(api: GitHub, branch: str, baseline: str) -> None:
    """Подтверждает прошлый выпуск линии или её проверенную начальную точку."""
    match = RELEASE_BRANCH_PATTERN.fullmatch(branch)
    assert match is not None
    line = tuple(map(int, match.groups()))
    releases = []
    for item in api.pages("/releases"):
        if not isinstance(item, dict):
            raise HotfixError("Некорректный GitHub Release")
        if item.get("draft") or item.get("prerelease"):
            continue
        version = VERSION_PATTERN.fullmatch(item.get("tag_name", ""))
        if version and tuple(map(int, version.groups()[:2])) == line:
            releases.append((int(version.group(3)), item["tag_name"]))
    if releases:
        highest = max(patch for patch, _ in releases)
        tags = [tag for patch, tag in releases if patch == highest]
        if len(tags) != 1 or tag_commit(api, tags[0]) != baseline:
            raise HotfixError("Baseline не является последним опубликованным выпуском линии")
    elif select_successful_run(api, api.repository, MAIN_WORKFLOW, baseline, "main") is None:
        raise HotfixError("Начальный baseline не имеет успешного main-full")


def check_release(api: GitHub, branch: str, candidate: str, baseline: str,
                  *, require_full_ci: bool = True) -> list[tuple[int, int, str]]:
    """Проверяет каждый merge релизной линии после прошлого выпуска.

    Args:
        api: Читающий клиент GitHub API.
        branch: Действующая релизная ветка release/X.Y.
        candidate: Её точный текущий SHA перед публикацией.
        baseline: SHA прошлого выпуска этой линии либо SHA создания ветки.
        require_full_ci: Требовать успешный release-full для обычной публикации.

    Returns:
        Список release PR, main PR и подтверждённого merge SHA main.

    Raises:
        HotfixError: История, ссылки или слияние выбранных PR не подтверждены.
    """
    if not RELEASE_BRANCH_PATTERN.fullmatch(branch) or not all(
        SHA_PATTERN.fullmatch(value) for value in (candidate, baseline)
    ):
        raise HotfixError("Требуются release/X.Y и два полных SHA")
    require_repository(api)
    check_baseline(api, branch, baseline)
    if git("status", "--porcelain", "--untracked-files=all"):
        raise HotfixError("Для публикации нужен чистый checkout")
    if git("rev-parse", "HEAD") != candidate:
        raise HotfixError("Checkout не соответствует candidate SHA")
    if api.get(f"/git/ref/heads/{branch}")["object"]["sha"] != candidate:
        raise HotfixError("Candidate не является текущим SHA релизной ветки")
    first_parent = git("rev-list", "--first-parent", candidate).splitlines()
    if baseline not in first_parent:
        raise HotfixError("Baseline не принадлежит первой родительской линии release")
    merges = list(reversed(first_parent[:first_parent.index(baseline)]))
    if not merges:
        raise HotfixError("После baseline нет исправлений")
    result = []
    for sha in merges:
        if len(git("rev-list", "--parents", "-n", "1", sha).split()) != 3:
            raise HotfixError(f"Изменение release без PR merge: {sha}")
        release = merge_pull(api, sha, branch)
        if release.get("merge_commit_sha") != sha or not release.get("merged_at"):
            raise HotfixError(f"Данные слияния PR #{release['number']} изменились")
        main, selected = linked_group(api, release["number"])
        if release["number"] not in [item["number"] for item in selected]:
            raise HotfixError("PR release не совпал с парой")
        main_sha = _merged_reachable(api, main, "main")
        for previous in selected:
            if previous["number"] == release["number"]:
                break
            _merged_reachable(api, previous, previous["base"]["ref"])
        result.append((release["number"], main["number"], main_sha))
    if api.get(f"/git/ref/heads/{branch}")["object"]["sha"] != candidate:
        raise HotfixError("Релизная ветка сдвинулась во время проверки")
    if require_full_ci and select_successful_run(api, api.repository, RELEASE_WORKFLOW, candidate, branch) is None:
        raise HotfixError("Нет успешного release-full для точного SHA")
    return result


def main() -> None:
    """Запускает проверку ссылок, зависимостей или выпуска."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("links", "deps"):
        pr_parser = subparsers.add_parser(mode)
        pr_parser.add_argument("number", type=int)
        pr_parser.add_argument("base")
    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("branch")
    release_parser.add_argument("candidate")
    release_parser.add_argument("baseline")
    args = parser.parse_args()
    try:
        api = GitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if args.mode in ("links", "deps"):
            require_repository(api)
            check = check_links if args.mode == "links" else check_pr
            main_number, releases = check(api, args.number, args.base)
            print(f"Связанные PR: main #{main_number}, release {', '.join(map(str, releases))}")
        else:
            for release_number, main_number, main_sha in check_release(
                api, args.branch, args.candidate, args.baseline,
            ):
                print(f"release #{release_number}, main #{main_number}: main merge {main_sha}")
    except (HotfixError, KeyError, TypeError, ValueError) as error:
        print(f"Hotfix pairs: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
