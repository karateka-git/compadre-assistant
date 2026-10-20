"""Повторно запускает актуальный hotfix-deps после слияния зависимости."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

from scripts.check_hotfix_pairs import (
    HotfixError, LINK_MARKER, _merged_reachable, check_pr, linked_group, pull,
    require_repository,
)
from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.full_ci_admission import SHA_PATTERN
from scripts.full_ci_controller import ControllerError, GitHub


class RerunError(RuntimeError):
    """Означает, что адресный повторный запуск нельзя безопасно подтвердить."""


class PendingCheck(RerunError):
    """Означает, что актуальная проверка ещё не завершилась и требует ожидания."""


# Общий бюджет группы не даёт нескольким pending PR растянуть workflow.
WAIT_SECONDS = 240
POLL_SECONDS = 5


def target_prs(api: GitHub, event: dict) -> tuple[dict, ...]:
    """Выбирает все открытые release PR после подтверждённого merge в main.

    Args:
        api: Читающий клиент GitHub.
        event: Содержимое доверенного события pull_request_target closed.

    Returns:
        Открытые release PR группы либо пустой кортеж для несвязанного события.

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
    if branch != "main":
        return ()
    if (snapshot.get("merged") is not True or snapshot.get("merge_commit_sha") !=
            current.get("merge_commit_sha") or snapshot.get("base", {}).get("ref") != branch):
        raise RerunError("Merge PR в событии не совпадает с GitHub API")
    _merged_reachable(api, current, branch)
    if LINK_MARKER not in current["body"]:
        return ()
    main, _, releases = linked_group(api, number)
    if main is None or main["number"] != number:
        raise RerunError("Событие main PR не совпадает с группой")
    return tuple(pr for pr in releases
                 if pr.get("state") == "open" and pr.get("merged_at") is None)


def current_failed_job(api: GitHub, pr: dict) -> int | None:
    """Находит failed hotfix-deps для текущих head и base PR.

    Args:
        api: Читающий клиент GitHub.
        pr: Открытый release PR, повторно прочитанный по API.

    Returns:
        ID failed job либо None, если проверка уже успешна.

    Raises:
        PendingCheck: Актуальный run/job ещё не виден в API или выполняется.
        RerunError: Нет однозначного актуального run/job или SHA устарел.
    """
    head = pr.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    base = pr.get("base")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    merge_sha = pr.get("merge_commit_sha")
    if not all(isinstance(sha, str) and SHA_PATTERN.fullmatch(sha)
               for sha in (head_sha, base_sha, merge_sha)):
        raise RerunError("Неизвестен текущий head, base или test-merge SHA PR")
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
        # Actions run.head_sha у pull_request равен head PR, а не test merge.
        # Пара head/base из привязанного PR исключает run прежнего основания.
        if (not isinstance(item, dict) or item.get("number") != pr["number"] or
                not isinstance(item.get("head"), dict) or
                item["head"].get("sha") != head_sha or
                not isinstance(item.get("base"), dict) or
                item["base"].get("ref") != base["ref"] or
                item["base"].get("sha") != base_sha or
                run.get("head_sha") != head_sha):
            continue
        if not isinstance(run.get("id"), int) or run["id"] < 1:
            raise RerunError("Некорректный ID актуального run")
        matches.append(run)
    if not matches:
        raise PendingCheck("Нет pr-fast run текущих head/base PR")
    run = max(matches, key=lambda item: item["id"])
    if run.get("status") != "completed":
        raise PendingCheck("Актуальный pr-fast run ещё не завершён")
    jobs = api.pages(f"/actions/runs/{run['id']}/jobs", key="jobs", filter="latest")
    selected = [job for job in jobs if isinstance(job, dict) and job.get("name") == "hotfix-deps"]
    if not selected:
        raise PendingCheck("Актуальный hotfix-deps ещё не появился в API")
    if len(selected) != 1:
        raise RerunError("Нужен ровно один завершённый failed hotfix-deps")
    if selected[0].get("status") != "completed":
        raise PendingCheck("Актуальный hotfix-deps ещё не завершён")
    if selected[0].get("conclusion") == "success":
        return None
    if selected[0].get("conclusion") != "failure":
        raise RerunError("Нужен ровно один завершённый failed hotfix-deps")
    job_id = selected[0].get("id")
    if not isinstance(job_id, int) or job_id < 1:
        raise RerunError("Некорректный ID hotfix-deps")
    return job_id


def _unchanged_pull(api: GitHub, pr: dict) -> dict:
    """Повторно читает PR и отвергает изменение снимка во время ожидания."""
    fresh = pull(api, pr["number"])
    if (fresh.get("head", {}).get("sha") != pr["head"]["sha"] or
            fresh.get("base", {}).get("sha") != pr["base"]["sha"] or
            fresh.get("base", {}).get("ref") != pr["base"]["ref"] or
            fresh.get("merge_commit_sha") != pr["merge_commit_sha"] or
            fresh.get("body") != pr["body"] or fresh.get("state") != "open" or
            fresh.get("merged_at") is not None):
        raise RerunError("Release PR изменился во время ожидания или перед rerun")
    return fresh


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
    """Проверяет событие и независимо перезапускает failed job выбранных линий."""
    try:
        event_path = os.environ.get("GITHUB_EVENT_PATH", "")
        api = GitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        require_repository(api)
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        targets = target_prs(api, event)
        if not targets:
            print("Связанных открытых release PR нет")
            return
        failed = []
        deadline = time.monotonic() + WAIT_SECONDS
        pending = list(targets)
        while pending:
            waiting = []
            for target in pending:
                try:
                    fresh = _unchanged_pull(api, target)
                    job_id = current_failed_job(api, fresh)
                    if job_id is None:
                        print(f"PR #{target['number']}: hotfix-deps успешен; rerun не нужен")
                        continue
                    check_pr(api, target["number"], target["base"]["ref"])
                    # Проверка ссылок обращается к API: после неё снова сверяем
                    # снимок и latest job, чтобы не отправить уже устаревший rerun.
                    fresh = _unchanged_pull(api, target)
                    if current_failed_job(api, fresh) != job_id:
                        raise PendingCheck("Актуальный job изменился перед rerun")
                    rerun_job(api, job_id)
                    print(f"Запрошен rerun hotfix-deps job {job_id} для PR #{target['number']}")
                except PendingCheck:
                    waiting.append(target)
                except (ControllerError, HotfixError, RerunError, KeyError, TypeError, ValueError) as error:
                    failed.append(f"PR #{target['number']}: {error}")
            if not waiting:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failed.extend(f"PR #{target['number']}: истёк срок ожидания актуального pr-fast"
                              for target in waiting)
                break
            # Все готовые линии обработаны до паузы: pending одной линии
            # не задерживает POST для остальных. Бюджет общий для всей группы.
            time.sleep(min(POLL_SECONDS, remaining))
            pending = waiting
        if failed:
            raise RerunError("; ".join(failed))
    except (ControllerError, HotfixError, RerunError, KeyError, TypeError, ValueError, OSError) as error:
        print(f"Hotfix rerun: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
