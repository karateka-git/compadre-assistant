"""Выбирает запуск полного CI по истории ветки и данным GitHub API."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BOOTSTRAP_PRODUCTION_SHA = "ac2ac981ba628a881b483238a4eaf191a644de6a"
MAIN_WORKFLOW = "main-full.yml"
RELEASE_WORKFLOW = "release-full.yml"
REQUIRED_STEPS = {
    "Checkout exact candidate",
    "Verify candidate commit",
    "Setup Python",
    "Install runtime dependencies",
    "Run full unittest suite",
    "Run three autonomous smoke checks",
    "Check fresh and previous SQLite schemas",
    "Build and healthcheck candidate image",
    "Publish check manifest",
}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
VERSION_PATTERN = re.compile(r"v(\d+)\.(\d+)\.(\d+)\Z")
RELEASE_BRANCH_PATTERN = re.compile(r"release/(\d+)\.(\d+)\Z")


class ControllerError(RuntimeError):
    """Означает, что решение о полном CI нельзя принять надёжно."""


def git(*arguments: str) -> str:
    """Возвращает stdout Git из точного checkout текущего workflow."""
    result = subprocess.run(["git", *arguments], capture_output=True, text=True)
    if result.returncode:
        raise ControllerError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout.strip()


def is_ancestor(older: str, newer: str) -> bool:
    """Проверяет достижимость commit без обращения к изменяемому имени ref."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer], capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise ControllerError("Не удалось проверить родство commit в Git")
    return result.returncode == 0


class GitHub:
    """Читает только REST API репозитория с кратким сроком жизни token."""

    def __init__(self, repository: str, token: str) -> None:
        """Сохраняет проверенные реквизиты API.

        Args:
            repository: Полное имя репозитория owner/repo.
            token: Выданный GitHub Actions token с правами чтения.

        Raises:
            ControllerError: Имя репозитория или token отсутствует.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise ControllerError("Не заданы GITHUB_REPOSITORY или GITHUB_TOKEN")
        self.root = f"https://api.github.com/repos/{repository}"
        self.token = token

    def get(self, path: str, **parameters: str | int) -> object:
        """Получает JSON или завершает работу при любой ошибке API."""
        url = self.root + path
        if parameters:
            url += "?" + urlencode(parameters)
        request = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "compadre-full-ci-controller",
        })
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except Exception as error:
            raise ControllerError(f"GitHub API недоступен: {path}: {error}") from error

    def pages(self, path: str, key: str | None = None, **parameters: str) -> list[object]:
        """Читает все страницы без молчаливого усечения выборки."""
        items: list[object] = []
        for page in range(1, 101):
            payload = self.get(path, per_page=100, page=page, **parameters)
            batch = payload[key] if key else payload
            if not isinstance(batch, list):
                raise ControllerError(f"Некорректный ответ GitHub API: {path}")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ControllerError(f"Слишком много страниц GitHub API: {path}")


def full_success(api: GitHub, workflow: str, sha: str, branch: str) -> bool:
    """Ищет полный успешный job на точном SHA, игнорируя успех контроллера."""
    runs = api.pages(
        f"/actions/workflows/{workflow}/runs", key="workflow_runs",
        head_sha=sha, status="success",
    )
    for run in runs:
        if (run.get("head_sha") != sha or run.get("conclusion") != "success"
                or run.get("head_branch") != branch):
            continue
        jobs = api.pages(f"/actions/runs/{run['id']}/jobs", key="jobs")
        for job in jobs:
            if not (job.get("name") == "full-suite" or job.get("name", "").endswith(" / full-suite")):
                continue
            if job.get("conclusion") != "success" or job.get("head_sha") != sha:
                continue
            steps = {step.get("name"): step.get("conclusion") for step in job.get("steps", [])}
            if all(steps.get(name) == "success" for name in REQUIRED_STEPS):
                return True
    return False


def prior_full_attempt(api: GitHub, workflow: str, sha: str, branch: str) -> bool:
    """Находит прежний полный запуск, чтобы новый dispatch не заменял rerun."""
    runs = api.pages(
        f"/actions/workflows/{workflow}/runs", key="workflow_runs", head_sha=sha,
    )
    current_run = os.environ.get("GITHUB_RUN_ID")
    for run in runs:
        if (run.get("head_sha") != sha or run.get("head_branch") != branch
                or str(run.get("id")) == current_run):
            continue
        jobs = api.pages(f"/actions/runs/{run['id']}/jobs", key="jobs")
        if any((job.get("name") == "full-suite" or
                job.get("name", "").endswith(" / full-suite"))
               and job.get("conclusion") != "skipped" and job.get("steps")
               for job in jobs):
            return True
    return False


def merged_pr(api: GitHub, sha: str) -> bool:
    """Подтверждает, что merge commit принадлежит слитому PR в main."""
    prs = api.pages(f"/commits/{sha}/pulls")
    matches = [pr for pr in prs if pr.get("merged_at") and pr.get("merge_commit_sha") == sha
               and pr.get("base", {}).get("ref") == "main"]
    if len(matches) > 1:
        raise ControllerError(f"Несколько PR для merge commit {sha}")
    return bool(matches)


def activation_sha(head: str) -> str:
    """Находит неизменяемую bootstrap-точку: первое появление caller в main."""
    commits = git("rev-list", "--first-parent", "--reverse", head).splitlines()
    for sha in commits:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}:.github/workflows/main-full.yml"],
            capture_output=True,
        ).returncode == 0
        if exists:
            return sha
    raise ControllerError("Bootstrap commit main-full не найден в первой родительской линии")


def merged_since_last_success(api: GitHub, head: str) -> tuple[int, str]:
    """Считает только PR merge commits до успеха full-suite или bootstrap."""
    anchor = activation_sha(head)
    count = 0
    for sha in git("rev-list", "--first-parent", head).splitlines():
        if full_success(api, MAIN_WORKFLOW, sha, "main"):
            return count, anchor
        if sha == anchor:
            return count, anchor
        parents = git("rev-list", "--parents", "-n", "1", sha).split()
        if len(parents) == 3 and merged_pr(api, sha):
            count += 1
            if count >= 3:
                return count, anchor
    raise ControllerError("Bootstrap commit недостижим из текущего main")


def tag_commit(api: GitHub, tag: str) -> str:
    """Разыменовывает опубликованный Git tag до точного commit."""
    reference = api.get(f"/git/ref/tags/{quote(tag, safe='')}")
    obj = reference["object"]
    for _ in range(5):
        if obj["type"] == "commit" and SHA_PATTERN.fullmatch(obj["sha"]):
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api.get(f"/git/tags/{obj['sha']}")["object"]
    raise ControllerError(f"Тег {tag} не указывает на commit")


def previous_release_sha(api: GitHub, candidate: str, branch: str) -> str:
    """Выбирает последний опубликованный выпуск линии или bootstrap.

    Args:
        api: Читающий GitHub API клиент.
        candidate: Проверяемый полный SHA.
        branch: Имя main либо release/X.Y.

    Returns:
        SHA предыдущего опубликованного выпуска.

    Raises:
        ControllerError: Выпуск или тег неоднозначны, либо bootstrap недостижим.
    """
    match = RELEASE_BRANCH_PATTERN.fullmatch(branch)
    if branch != "main" and not match:
        raise ControllerError(f"Недопустимая ветка: {branch}")
    versions: list[tuple[tuple[int, int, int], datetime, str]] = []
    for release in api.pages("/releases"):
        if release.get("draft") or release.get("prerelease"):
            continue
        version = VERSION_PATTERN.fullmatch(release.get("tag_name", ""))
        if not version or not release.get("published_at"):
            raise ControllerError("Опубликован выпуск без однозначной SemVer-версии")
        parts = tuple(map(int, version.groups()))
        published = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
        versions.append((parts, published, release["tag_name"]))
    if len({version for version, _, _ in versions}) != len(versions):
        raise ControllerError("Повторяющиеся номера опубликованных выпусков")
    if not versions:
        if is_ancestor(BOOTSTRAP_PRODUCTION_SHA, candidate):
            return BOOTSTRAP_PRODUCTION_SHA
        raise ControllerError("Bootstrap production SHA не является предком candidate")
    relevant = [entry for entry in versions if entry[0][:2] == tuple(map(int, match.groups()))] if match else versions
    # Для новой релизной линии схема берётся от последнего глобального выпуска.
    choices = relevant or versions
    latest = max(entry[1] for entry in choices)
    matches = [entry for entry in choices if entry[1] == latest]
    if len(matches) != 1:
        raise ControllerError("Неоднозначное время последнего опубликованного выпуска")
    _, _, tag = matches[0]
    # PATCH поддерживаемой линии может жить в соседней Git-ветке. Совместимость
    # её схемы с candidate доказывает строгий prefix/content тест full-suite.
    return tag_commit(api, tag)


def decide(api: GitHub, mode: str, sha: str, branch: str, created: bool) -> dict[str, str]:
    """Возвращает решение caller и SHA схемы прежнего выпуска."""
    if not SHA_PATTERN.fullmatch(sha) or git("rev-parse", "HEAD") != sha:
        raise ControllerError("Checkout не соответствует полному GITHUB_SHA")
    ref = api.get(f"/git/ref/heads/{quote(branch, safe='/')}")
    if ref["object"]["sha"] != sha:
        raise ControllerError("Ветка ушла с SHA события; устаревший run нельзя продолжать")
    if mode == "main":
        if branch != "main":
            raise ControllerError("Ручной main-full разрешён только для main")
        count, anchor = merged_since_last_success(api, sha)
        print(f"main bootstrap={anchor}, PR since success={count}")
        if count < 3 and os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            return {"run-full": "false", "previous-release-sha": ""}
        if full_success(api, MAIN_WORKFLOW, sha, "main"):
            return {"run-full": "false", "previous-release-sha": ""}
        if prior_full_attempt(api, MAIN_WORKFLOW, sha, "main"):
            raise ControllerError("Для прежней неуспешной проверки этого SHA требуется явный rerun")
    elif mode == "release":
        if not RELEASE_BRANCH_PATTERN.fullmatch(branch):
            raise ControllerError("release-full разрешён только для release/X.Y")
        if created:
            if not full_success(api, MAIN_WORKFLOW, sha, "main"):
                raise ControllerError("Созданная release ветка не имеет успешного main-full этого SHA")
            return {"run-full": "false", "previous-release-sha": ""}
        if (full_success(api, RELEASE_WORKFLOW, sha, branch)
                or full_success(api, MAIN_WORKFLOW, sha, "main")):
            return {"run-full": "false", "previous-release-sha": ""}
        if prior_full_attempt(api, RELEASE_WORKFLOW, sha, branch):
            raise ControllerError("Для прежней неуспешной проверки этого SHA требуется явный rerun")
    else:
        raise ControllerError(f"Неизвестный режим: {mode}")
    previous = previous_release_sha(api, sha, branch)
    return {"run-full": "true", "previous-release-sha": previous}


def main() -> None:
    """Пишет outputs для GitHub Actions или завершает controller ошибкой."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("main", "release"))
    arguments = parser.parse_args()
    try:
        api = GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""))
        branch = os.environ.get("GITHUB_REF_NAME", "")
        result = decide(
            api, arguments.mode, os.environ.get("GITHUB_SHA", ""), branch,
            os.environ.get("BRANCH_CREATED") == "true",
        )
        print(result)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    except (ControllerError, KeyError, TypeError, ValueError) as error:
        print(f"Full CI controller: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
