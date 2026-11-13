"""Выбирает запуск полного CI по истории ветки и данным GitHub API."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from scripts.full_ci_admission import (
    AdmissionError, COMPATIBILITY_CHECK, FULL_CALLERS, FULL_SUITE_CHECK,
    MAIN_WORKFLOW, RELEASE_BRANCH_PATTERN, RELEASE_WORKFLOW,
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
    caller = FULL_CALLERS[workflow]
    checks = {f"{caller} / {name}" for name in (FULL_SUITE_CHECK, COMPATIBILITY_CHECK)}
    for run in runs:
        if (run.get("head_sha") != sha or run.get("head_branch") != branch
                or str(run.get("id")) == current_run):
            continue
        if run.get("status") != "completed":
            return True
        jobs = api.pages(f"/actions/runs/{run['id']}/jobs", key="jobs", filter="latest")
        if any(job.get("name") in checks
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
    raise ControllerError("Bootstrap commit pre-release-check-full не найден в первой родительской линии")


def merged_since_last_success(api: GitHub, head: str) -> tuple[int, str]:
    """Считает только PR merge commits до успеха check-full-suite или bootstrap."""
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


def _baseline_outputs(api: GitHub) -> dict[str, str]:
    """Закрепляет одну принятую базу для всех jobs полного CI."""
    from scripts.deployment_acceptance import DeploymentAcceptanceError, resolve_accepted_baseline

    policy_sha = os.environ.get("COMPATIBILITY_POLICY_SHA", "")
    if not SHA_PATTERN.fullmatch(policy_sha):
        raise ControllerError("Не задан точный внешний SHA CI policy")
    try:
        baseline = resolve_accepted_baseline(api)
    except DeploymentAcceptanceError as error:
        raise ControllerError(str(error)) from error
    return {"run-full": "true", "previous-release-sha": baseline.sha, "policy-sha": policy_sha,
            "accepted-baseline": json.dumps(asdict(baseline), separators=(",", ":"), sort_keys=True)}


def decide(api: GitHub, mode: str, sha: str, branch: str, created: bool, *,
           expected_sha: str = "", expected_policy_sha: str = "") -> dict[str, str]:
    """Возвращает решение caller и точную приёмку предыдущего выпуска.

    Args:
        api: GitHub-клиент для проверки точной ветки и закреплённой базы.
        mode: Имя caller main или release.
        sha: Полный SHA события CI.
        branch: Ветка события CI.
        created: Признак создания release-ветки.
        expected_sha: Ожидаемый SHA ветки для ручного запуска.
        expected_policy_sha: Необязательный ожидаемый pin для ручного запуска.

    Returns:
        Outputs caller для запуска полного набора и выбора его базы.

    Raises:
        ControllerError: Checkout, ref, предыдущая попытка или база не подтверждены.
    """
    if not SHA_PATTERN.fullmatch(sha) or git("rev-parse", "HEAD") != sha:
        raise ControllerError("Checkout не соответствует полному GITHUB_SHA")
    if mode == "main" and branch == "main":
        workflow = MAIN_WORKFLOW
    elif mode == "release" and RELEASE_BRANCH_PATTERN.fullmatch(branch):
        workflow = RELEASE_WORKFLOW
    else:
        raise ControllerError("Caller разрешён только для своей ветки main или release/X.Y")
    ref = api.get(f"/git/ref/heads/{quote(branch, safe='/')}")
    if ref["object"]["sha"] != sha:
        raise ControllerError("Ветка ушла с SHA события; устаревший run нельзя продолжать")
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        if expected_sha != sha:
            raise ControllerError("Ручной запуск требует точный SHA текущей ветки")
        if expected_policy_sha and expected_policy_sha != os.environ.get("COMPATIBILITY_POLICY_SHA"):
            raise ControllerError("Внешний CI policy pin изменился перед ручным запуском")
        # Прежний успешный CI того же SHA мог проверять другую production базу.
        return _baseline_outputs(api)
    if mode == "main":
        count, anchor = merged_since_last_success(api, sha)
        print(f"main bootstrap={anchor}, PR since success={count}")
        if count < 3:
            return {"run-full": "false"}
    elif created:
        if not full_success(api, MAIN_WORKFLOW, sha, "main"):
            raise ControllerError("Созданная release ветка не имеет успешного pre-release-check-full этого SHA")
        return {"run-full": "false"}
    if full_success(api, workflow, sha, branch):
        return {"run-full": "false"}
    if prior_full_attempt(api, workflow, sha, branch):
        raise ControllerError("Для прежней неуспешной проверки этого SHA требуется явный ручной запуск")
    return _baseline_outputs(api)


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
            expected_sha=os.environ.get("EXPECTED_SHA", ""),
            expected_policy_sha=os.environ.get("EXPECTED_POLICY_SHA", ""),
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
