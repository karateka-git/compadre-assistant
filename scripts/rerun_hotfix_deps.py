"""Повторно запускает актуальный hotfix-deps после слияния зависимости."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen

from scripts.check_hotfix_pairs import (
    HotfixError, LINK_MARKER, _merged_reachable, check_pr, linked_group, pull,
    require_repository,
)
from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.full_ci_admission import SHA_PATTERN
from scripts.full_ci_controller import GitHub


class RerunError(RuntimeError):
    """Означает, что адресный повторный запуск нельзя безопасно подтвердить."""


def target_pr(api: GitHub, event: dict) -> dict | None:
    """Выбирает следующий release PR после подтверждённого merge события.

    Args:
        api: Читающий клиент GitHub.
        event: Содержимое доверенного события pull_request_target closed.

    Returns:
        Следующий открытый release PR или None для несвязанного/последнего PR.

    Raises:
        RerunError: Событие, связь или merge commit не подтверждены API.
    """
    if (event.get("action") != "closed" or
            not isinstance(event.get("repository"), dict) or
            event["repository"].get("full_name") != EXPECTED_REPOSITORY or
            not isinstance(event.get("pull_request"), dict)):
        raise RerunError("Неверное событие закрытия PR")
    snapshot = event["pull_request"]
    number = snapshot.get("number")
    if not isinstance(number, int) or number < 1:
        raise RerunError("Нет номера PR в событии")
    current = pull(api, number)
    branch = current["base"].get("ref")
    if branch != "main" and not (isinstance(branch, str) and branch.startswith("release/")):
        return None
    if (snapshot.get("merged") is not True or snapshot.get("merge_commit_sha") !=
            current.get("merge_commit_sha") or snapshot.get("base", {}).get("ref") != branch):
        raise RerunError("Merge PR в событии не совпадает с GitHub API")
    _merged_reachable(api, current, branch)
    if branch == "main" and LINK_MARKER not in current["body"]:
        return None
    main, _, releases = linked_group(api, number)
    if branch == "main":
        if main is None or main["number"] != number:
            raise RerunError("Событие main PR не совпадает с группой")
        following = releases
    else:
        index = next((i for i, item in enumerate(releases) if item["number"] == number), None)
        if index is None:
            raise RerunError("Слитый release PR отсутствует в группе")
        following = releases[index + 1:]
    if not following:
        return None
    target = pull(api, following[0]["number"])
    if target.get("state") != "open" or target.get("merged_at") is not None:
        raise RerunError("Следующий release PR не открыт")
    check_pr(api, target["number"], target["base"]["ref"])
    return target


def current_failed_job(api: GitHub, pr: dict) -> int:
    """Находит ровно один failed hotfix-deps на текущем head и test merge.

    Args:
        api: Читающий клиент GitHub.
        pr: Открытый release PR, повторно прочитанный по API.

    Returns:
        ID job, который можно передать GitHub rerun API.

    Raises:
        RerunError: Нет однозначного актуального run/job или SHA устарел.
    """
    head = pr.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    merge_sha = pr.get("merge_commit_sha")
    if not all(isinstance(sha, str) and SHA_PATTERN.fullmatch(sha)
               for sha in (head_sha, merge_sha)):
        raise RerunError("Неизвестен текущий head или test-merge SHA PR")
    runs = api.pages("/actions/workflows/pr-fast.yml/runs", key="workflow_runs",
                     event="pull_request")
    matches = []
    for run in runs:
        if not isinstance(run, dict) or run.get("event") != "pull_request":
            continue
        attached = run.get("pull_requests")
        if not isinstance(attached, list) or len(attached) != 1:
            continue
        item = attached[0]
        if (not isinstance(item, dict) or item.get("number") != pr["number"] or
                not isinstance(item.get("head"), dict) or
                item["head"].get("sha") != head_sha or
                not isinstance(item.get("base"), dict) or
                item["base"].get("ref") != pr["base"]["ref"] or
                run.get("head_sha") != merge_sha):
            continue
        if not isinstance(run.get("id"), int) or run["id"] < 1:
            raise RerunError("Некорректный ID актуального run")
        matches.append(run)
    if not matches:
        raise RerunError("Нет pr-fast run текущего head/test merge PR")
    run = max(matches, key=lambda item: item["id"])
    if run.get("status") != "completed":
        raise RerunError("Актуальный pr-fast run ещё не завершён")
    jobs = api.pages(f"/actions/runs/{run['id']}/jobs", key="jobs", filter="latest")
    selected = [job for job in jobs if isinstance(job, dict) and job.get("name") == "hotfix-deps"]
    if len(selected) != 1 or selected[0].get("status") != "completed" or (
            selected[0].get("conclusion") != "failure"):
        raise RerunError("Нужен ровно один завершённый failed hotfix-deps")
    job_id = selected[0].get("id")
    if not isinstance(job_id, int) or job_id < 1:
        raise RerunError("Некорректный ID hotfix-deps")
    return job_id


def rerun_job(api: GitHub, job_id: int) -> None:
    """Посылает один POST на повторный запуск конкретного job.

    Raises:
        RerunError: GitHub не подтвердил принятие rerun кодом 201.
    """
    request = Request(f"{api.root}/actions/jobs/{job_id}/rerun", data=b"{}", method="POST",
                      headers={"Accept": "application/vnd.github+json",
                               "Authorization": f"Bearer {api.token}",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "Content-Type": "application/json",
                               "User-Agent": "compadre-hotfix-rerun"})
    try:
        with urlopen(request, timeout=20) as response:
            if response.status != 201:
                raise RerunError(f"GitHub не принял rerun: HTTP {response.status}")
    except Exception as error:
        raise RerunError(f"Не подтверждён rerun hotfix-deps: {error}") from error


def main() -> None:
    """Проверяет событие и выполняет один адресный rerun при наличии цели."""
    try:
        event_path = os.environ.get("GITHUB_EVENT_PATH", "")
        api = GitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        require_repository(api)
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        target = target_pr(api, event)
        if target is None:
            print("Связанного следующего release PR нет")
            return
        job_id = current_failed_job(api, target)
        # Непосредственно перед POST проверяем, что никто не сменил head или test merge.
        fresh = pull(api, target["number"])
        if (fresh.get("head", {}).get("sha") != target["head"]["sha"] or
                fresh.get("merge_commit_sha") != target["merge_commit_sha"] or
                fresh.get("body") != target["body"] or
                fresh.get("state") != "open"):
            raise RerunError("Release PR изменился перед rerun")
        check_pr(api, target["number"], target["base"]["ref"])
        rerun_job(api, job_id)
        print(f"Запрошен rerun hotfix-deps job {job_id} для PR #{target['number']}")
    except (HotfixError, RerunError, KeyError, TypeError, ValueError, OSError) as error:
        print(f"Hotfix rerun: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
