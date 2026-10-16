"""Проверяет связь двух PR исправления и допуск изменений релизной линии."""

from __future__ import annotations

import argparse
import hashlib
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


PAIR_PATTERN = re.compile(r"^Hotfix-Pair: #([1-9]\d*)$", re.MULTILINE)
ATTESTATION_PATTERN = re.compile(
    r"^Hotfix-Adaptation-Approved: #(\d+) release=([0-9a-f]{40}) main=([0-9a-f]{40})$",
    re.MULTILINE,
)


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


def pair_number(pr: dict) -> int:
    """Извлекает единственную явную ссылку на парный PR."""
    matches = PAIR_PATTERN.findall(pr["body"])
    if len(matches) != 1 or int(matches[0]) == pr["number"]:
        raise HotfixError(f"PR #{pr['number']}: требуется одна строка Hotfix-Pair: #N")
    return int(matches[0])


def linked_pulls(api: GitHub, number: int) -> tuple[dict, dict]:
    """Проверяет взаимные ссылки и разные целевые ветки PR.

    Args:
        api: Читающий клиент GitHub API.
        number: Номер любого PR из пары.

    Returns:
        PR релизной ветки и PR в main.

    Raises:
        HotfixError: Связь отсутствует, неоднозначна или направлена не в те ветки.
    """
    first = pull(api, number)
    second = pull(api, pair_number(first))
    if pair_number(second) != number:
        raise HotfixError("PR не содержат взаимные ссылки Hotfix-Pair")
    release, main = (first, second) if first["base"].get("ref") != "main" else (second, first)
    if not RELEASE_BRANCH_PATTERN.fullmatch(release["base"].get("ref", "")):
        raise HotfixError("Один PR должен быть направлен в release/X.Y")
    if main["base"].get("ref") != "main":
        raise HotfixError("Второй PR должен быть направлен в main")
    return release, main


def check_pr(api: GitHub, number: int, expected_branch: str) -> tuple[int, int]:
    """Проверяет взаимные ссылки до слияния каждого PR."""
    release, main = linked_pulls(api, number)
    current = release if release["number"] == number else main
    if current["base"]["ref"] != expected_branch:
        raise HotfixError("Целевая ветка события не совпадает с PR")
    return release["number"], main["number"]


def merge_pull(api: GitHub, sha: str, branch: str) -> dict:
    """Находит единственный слитый PR для merge commit заданной ветки."""
    matches = [pr for pr in api.pages(f"/commits/{sha}/pulls")
               if isinstance(pr, dict) and pr.get("merged_at")
               and pr.get("merge_commit_sha") == sha
               and isinstance(pr.get("base"), dict) and pr["base"].get("ref") == branch]
    if len(matches) != 1:
        raise HotfixError(f"Для {sha} нет единственного слитого PR в {branch}")
    return pull(api, matches[0]["number"])


def patch_digest(merge_sha: str) -> str | None:
    """Хеширует точные байты Git diff первого родителя merge commit."""
    result = subprocess.run(
        ["git", "diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames",
         f"{merge_sha}^1", merge_sha], capture_output=True,
    )
    if result.returncode:
        raise HotfixError(f"git diff {merge_sha}: {result.stderr.decode(errors='replace').strip()}")
    if not result.stdout:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def owner_attested(api: GitHub, release: dict, main: dict) -> bool:
    """Ищет явное подтверждение владельца для разных адаптаций."""
    expected = (str(main["number"]), release["merge_commit_sha"], main["merge_commit_sha"])
    owner = EXPECTED_REPOSITORY.split("/", 1)[0]
    for comment in api.pages(f"/issues/{release['number']}/comments"):
        if not isinstance(comment, dict) or not isinstance(comment.get("user"), dict):
            raise HotfixError("Некорректный комментарий GitHub API")
        if comment["user"].get("login") != owner:
            continue
        body = comment.get("body")
        if isinstance(body, str) and expected in ATTESTATION_PATTERN.findall(body):
            return True
    return False


def reachable(api: GitHub, sha: str, branch: str) -> bool:
    """Подтверждает принадлежность SHA текущей истории ветки через GitHub."""
    ref = api.get(f"/git/ref/heads/{branch}")
    head = ref["object"]["sha"]
    comparison = api.get(f"/compare/{sha}...{head}")
    return (comparison.get("status") in ("ahead", "identical")
            and comparison.get("base_commit", {}).get("sha") == sha)


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
        Список пар PR и способа подтверждения патча.

    Raises:
        HotfixError: История, связь, слияние в main или адаптация не подтверждены.
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
        linked_release, main = linked_pulls(api, release["number"])
        if linked_release["number"] != release["number"]:
            raise HotfixError("PR release не совпал с парой")
        main_sha = main.get("merge_commit_sha")
        if not main.get("merged_at") or not isinstance(main_sha, str) or not SHA_PATTERN.fullmatch(main_sha):
            raise HotfixError(f"Парный PR #{main['number']} ещё не слит в main")
        if merge_pull(api, main_sha, "main")["number"] != main["number"] or not reachable(api, main_sha, "main"):
            raise HotfixError(f"Слияние PR #{main['number']} не подтверждено в main")
        release_patch, main_patch = patch_digest(sha), patch_digest(main_sha)
        if release_patch and release_patch == main_patch:
            method = f"точный Git diff SHA-256 {release_patch}"
        elif owner_attested(api, release, main):
            method = "ручное подтверждение владельца"
        else:
            raise HotfixError(f"PR #{release['number']} и #{main['number']}: патчи различаются; "
                              "нужно подтверждение владельца с точными merge SHA")
        result.append((release["number"], main["number"], method))
    if api.get(f"/git/ref/heads/{branch}")["object"]["sha"] != candidate:
        raise HotfixError("Релизная ветка сдвинулась во время проверки")
    if require_full_ci and select_successful_run(api, api.repository, RELEASE_WORKFLOW, candidate, branch) is None:
        raise HotfixError("Нет успешного release-full для точного SHA")
    return result


def main() -> None:
    """Запускает раннюю проверку PR или проверку пары перед публикацией."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    pr_parser = subparsers.add_parser("pr")
    pr_parser.add_argument("number", type=int)
    pr_parser.add_argument("base")
    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("branch")
    release_parser.add_argument("candidate")
    release_parser.add_argument("baseline")
    args = parser.parse_args()
    try:
        api = GitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if args.mode == "pr":
            require_repository(api)
            release_number, main_number = check_pr(api, args.number, args.base)
            print(f"Связанные PR: release #{release_number}, main #{main_number}")
        else:
            for release_number, main_number, method in check_release(
                api, args.branch, args.candidate, args.baseline,
            ):
                print(f"release #{release_number}, main #{main_number}: {method}")
    except (HotfixError, KeyError, TypeError, ValueError) as error:
        print(f"Hotfix pairs: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
