"""Выбирает запуск полного CI по истории ветки и данным GitHub API."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from scripts.ci_repository_compatibility import verify_baseline
from scripts.full_ci_admission import (
    AdmissionError, MAIN_WORKFLOW, RELEASE_BRANCH_PATTERN, RELEASE_WORKFLOW,
    SHA_PATTERN, select_successful_run,
)


VERSION_PATTERN = re.compile(r"v(\d+)\.(\d+)\.(\d+)\Z")


class ControllerError(RuntimeError):
    """Означает, что решение о полном CI нельзя принять надёжно."""


def git(*arguments: str) -> str:
    """Возвращает stdout Git из точного checkout текущего workflow."""
    result = subprocess.run(["git", *arguments], capture_output=True, text=True)
    if result.returncode:
        raise ControllerError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout.strip()


class GitHub:
    """Читает REST API репозитория с token или без него для публичных данных."""

    def __init__(self, repository: str, token: str, *, allow_anonymous: bool = False) -> None:
        """Сохраняет проверенные реквизиты API.

        Args:
            repository: Полное имя репозитория owner/repo.
            token: Выданный GitHub Actions token с правами чтения.
            allow_anonymous: Разрешить запросы без token для публичного репозитория.

        Raises:
            ControllerError: Имя репозитория или token отсутствует.
        """
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or (not token and not allow_anonymous)):
            raise ControllerError("Не заданы GITHUB_REPOSITORY или GITHUB_TOKEN")
        self.repository = repository
        self.root = f"https://api.github.com/repos/{repository}"
        self.token = token

    def get(self, path: str, **parameters: str | int) -> object:
        """Получает JSON или завершает работу при любой ошибке API."""
        url = self.root + path
        if parameters:
            url += "?" + urlencode(parameters)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "compadre-full-ci-controller",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(url, headers=headers)
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
    """Проверяет допуск через общий selector точного полного CI."""
    return select_successful_run(api, api.repository, workflow, sha, branch) is not None


def prior_full_attempt(api: GitHub, workflow: str, sha: str, branch: str) -> bool:
    """Находит прежний полный запуск, чтобы новый dispatch не заменял rerun."""
    runs = api.pages(f"/actions/workflows/{workflow}/runs", key="workflow_runs", head_sha=sha)
    current_run = os.environ.get("GITHUB_RUN_ID")
    for run in runs:
        if (run.get("head_sha") != sha or run.get("head_branch") != branch
                or str(run.get("id")) == current_run):
            continue
        if run.get("status") != "completed":
            return True
        jobs = api.pages(f"/actions/runs/{run['id']}/jobs", key="jobs", filter="latest")
        if any((job.get("name") in {"full-suite", "repository-compatibility"} or
                job.get("name", "").endswith((" / full-suite", " / repository-compatibility")))
               and job.get("conclusion") != "skipped"
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


def previous_release_sha(api: GitHub, branch: str) -> str:
    """Читает прежний выпуск из внешне закреплённой CI policy.

    Args:
        api: Читающий GitHub API клиент.
        branch: Имя main либо release/X.Y.

    Returns:
        SHA выпуска, закреплённого в policy для установленной production базы.

    Raises:
        ControllerError: Pin, policy или immutable Release/tag недействительны.
    """
    if branch != "main" and not RELEASE_BRANCH_PATTERN.fullmatch(branch):
        raise ControllerError(f"Недопустимая ветка: {branch}")
    pin = os.environ.get("COMPATIBILITY_POLICY_SHA", "")
    if not SHA_PATTERN.fullmatch(pin):
        raise ControllerError("Не задан точный внешний SHA CI policy")
    response = api.get("/contents/scripts/repository_compatibility_policy.json", ref=pin)
    if (not isinstance(response, dict) or response.get("encoding") != "base64"
            or not isinstance(response.get("content"), str)):
        raise ControllerError("Некорректный ответ GitHub Contents для CI policy")
    try:
        content = base64.b64decode("".join(response["content"].split()), validate=True)
        policy = json.loads(content)
    except (ValueError, TypeError, binascii.Error) as error:
        raise ControllerError("Недействительные байты CI policy") from error
    if (not isinstance(policy, dict) or policy.get("version") != 1
            or policy.get("repository") != api.repository
            or not SHA_PATTERN.fullmatch(policy.get("previous_sha", ""))
            or not VERSION_PATTERN.fullmatch(policy.get("release_tag", ""))
            or type(policy.get("release_id")) is not int or policy["release_id"] <= 0):
        raise ControllerError("CI policy не задаёт допустимую production базу")
    previous = policy["previous_sha"]
    tag = quote(policy["release_tag"], safe="")
    verify_baseline(policy, api.get(f"/releases/tags/{tag}"),
                    api.get(f"/git/ref/tags/{tag}"), previous)
    return previous


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
        # После PR merge в существующую release-ветку появляется новый SHA:
        # результат main для него невозможен при разрешённом merge commit.
        if full_success(api, RELEASE_WORKFLOW, sha, branch):
            return {"run-full": "false", "previous-release-sha": ""}
        if prior_full_attempt(api, RELEASE_WORKFLOW, sha, branch):
            raise ControllerError("Для прежней неуспешной проверки этого SHA требуется явный rerun")
    else:
        raise ControllerError(f"Неизвестный режим: {mode}")
    previous = previous_release_sha(api, branch)
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
    except (AdmissionError, ControllerError, KeyError, TypeError, ValueError) as error:
        print(f"Full CI controller: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
