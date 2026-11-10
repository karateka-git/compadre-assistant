"""Сохраняет краткие измерения попытки GitHub Actions без изменения допуска."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.full_ci_admission import (
    FULL_CALLERS, FULL_SUITE_CHECK, MAIN_WORKFLOW, PR_CHECK, PR_WORKFLOW,
)


# Измеряются только caller workflows; reusable suite отдельно не запускается.
WORKFLOWS = {PR_WORKFLOW, *FULL_CALLERS}
# Полный SHA позволяет различать PR head, тестовый merge и проверяемый выпуск.
SHA = re.compile(r"[0-9a-f]{40}\Z")


class MetricsError(RuntimeError):
    """Означает, что измерение нельзя надёжно восстановить из API."""


def duration(start: object, end: object) -> int | None:
    """Возвращает длительность в секундах или unknown для отсутствующей границы.

    Raises:
        MetricsError: Границы повреждены или идут в обратном порядке.
    """
    if not start or not end:
        return None
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        seconds = (last - first).total_seconds()
    except (TypeError, ValueError) as error:
        raise MetricsError("Некорректное время GitHub API") from error
    if seconds < 0:
        raise MetricsError("Конец этапа раньше начала")
    return round(seconds)


def classify(workflow: str, jobs: list[dict]) -> str:
    """Различает короткий PR, полный main/release и пропущенный набор."""
    if workflow == PR_WORKFLOW:
        if sum(job.get("name") == PR_CHECK for job in jobs) != 1:
            raise MetricsError(f"Отсутствует или повторяется job {PR_CHECK}")
        return PR_CHECK
    prefix = FULL_CALLERS[workflow]
    suite = [job for job in jobs if job.get("name") in {prefix, f"{prefix} / {FULL_SUITE_CHECK}"}]
    if len(suite) != 1:
        raise MetricsError(f"Отсутствует или повторяется job {FULL_SUITE_CHECK}")
    if suite[0].get("conclusion") == "skipped":
        return "skipped"
    if suite[0].get("name") == prefix:
        raise MetricsError(f"Неизвестная структура job {FULL_SUITE_CHECK}")
    return "full-main" if workflow == MAIN_WORKFLOW else "full-release"


def summarize(run: dict, jobs: list[dict], workflow: str, repository: str,
              run_id: int, attempt: int, sha: str) -> dict:
    """Проверяет привязку попытки и строит отчёт по проверочным jobs/steps.

    Args:
        run: Ответ API для workflow run.
        jobs: Jobs именно указанной попытки.
        workflow: Имя caller workflow.
        repository: Проверяемое имя owner/repo.
        run_id: Числовой ID запуска.
        attempt: Числовой номер попытки.
        sha: Полный SHA события.

    Returns:
        Компактная запись без содержимого логов и пользовательских данных.

    Raises:
        MetricsError: API не подтверждает идентичность или структуру попытки.
    """
    if (not isinstance(run, dict) or workflow not in WORKFLOWS or not SHA.fullmatch(sha)
            or run_id <= 0 or attempt <= 0
            or run.get("id") != run_id or run.get("run_attempt") != attempt
            or type(run.get("run_number")) is not int
            or run.get("head_sha") != sha
            or run.get("repository", {}).get("full_name") != repository
            or not isinstance(jobs, list)):
        raise MetricsError("Run не соответствует ожидаемым SHA/repository/attempt")
    expected_path = f".github/workflows/{workflow}"
    if str(run.get("path", "")).split("@")[0] != expected_path:
        raise MetricsError("Run относится к другому workflow")
    measured = []
    seen = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise MetricsError("Некорректный объект job")
        name = job.get("name")
        if name == "ci-metrics":
            continue
        job_sha = job.get("head_sha")
        if (not isinstance(name, str) or name in seen
                or job.get("run_id") != run_id or job.get("run_attempt") != attempt
                or not isinstance(job_sha, str) or not SHA.fullmatch(job_sha)):
            raise MetricsError("Job не соответствует run/attempt или имеет некорректный SHA")
        # У PR run.head_sha и SHA тестового merge имеют разный смысл.
        # Идентичность jobs подтверждает endpoint попытки и поля run_id/run_attempt.
        if run.get("event") != "pull_request" and job_sha != sha:
            raise MetricsError("Job относится к другому SHA")
        seen.add(name)
        steps = job.get("steps") or []
        if not isinstance(steps, list):
            raise MetricsError("Некорректный список steps")
        measured.append({
            "name": name, "sha": job_sha, "conclusion": job.get("conclusion") or "pending",
            # GitHub может вернуть completed_at раньше started_at у skipped job.
            "seconds": (None if job.get("conclusion") == "skipped" else
                        duration(job.get("started_at"), job.get("completed_at"))),
            "carried_over": bool(job.get("completed_at") and run.get("run_started_at")
                                 and job["completed_at"] < run["run_started_at"]),
            "url": f"https://github.com/{repository}/actions/runs/{run_id}/job/{job['id']}",
            "steps": [{"name": step["name"],
                       "conclusion": step.get("conclusion") or "pending",
                       "seconds": (None if step.get("conclusion") == "skipped" else
                                   duration(step.get("started_at"), step.get("completed_at")))}
                      for step in steps],
        })
    if not measured:
        raise MetricsError("Нет проверочных jobs")
    kind = classify(workflow, measured)
    conclusions = [job["conclusion"] for job in measured]
    # Текущий run остаётся in_progress, пока отчётный job выполняется.
    result = ("failure" if any(value in {"failure", "timed_out", "cancelled"}
                               for value in conclusions) else
              "pending" if any(value not in {"success", "skipped"}
                               for value in conclusions) else "success")
    started = run.get("run_started_at")
    finished = max((job.get("completed_at") for job in jobs
                    if job.get("name") != "ci-metrics" and job.get("completed_at")
                    and started and job["completed_at"] >= started),
                   default=None)
    return {"schema": 1, "repository": repository, "workflow": workflow,
            "kind": kind, "sha": sha, "run_id": run_id, "attempt": attempt,
            "branch": run.get("head_branch"), "run_number": run.get("run_number"),
            "event": run.get("event"), "result": result,
            "started_at": started, "checks_finished_at": finished,
            "seconds": duration(started, finished),
            "url": f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}",
            "jobs": measured}


class GitHub:
    """Читает только необходимые REST endpoints с ограниченным ответом ошибок."""

    def __init__(self, repository: str, token: str) -> None:
        """Проверяет адрес репозитория и наличие read-only token."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise MetricsError("Не заданы repository или token")
        self.root = f"https://api.github.com/repos/{repository}"
        self.token = token

    def get(self, path: str, **params: str | int) -> object:
        """Возвращает JSON без включения URL, token и тела ошибки в диагностику."""
        url = self.root + path + ("?" + urlencode(params) if params else "")
        request = Request(url, headers={"Accept": "application/vnd.github+json",
                                        "Authorization": f"Bearer {self.token}",
                                        "User-Agent": "compadre-ci-metrics"})
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            raise MetricsError(f"GitHub API: HTTP {error.code}") from None
        except (URLError, TimeoutError, ValueError) as error:
            raise MetricsError(f"GitHub API: {type(error).__name__}") from None

    def jobs(self, run_id: int, attempt: int) -> list[dict]:
        """Читает все страницы jobs указанной попытки с конечным пределом."""
        result = []
        for page in range(1, 11):
            payload = self.get(f"/actions/runs/{run_id}/attempts/{attempt}/jobs",
                               per_page=100, page=page)
            batch = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise MetricsError("GitHub API: нет списка jobs")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise MetricsError("GitHub API: слишком много jobs")


def render(report: dict, previous: dict | None = None) -> str:
    """Создаёт короткую Markdown сводку текущей попытки и сопоставимого run."""
    lines = ["## CI metrics", "", f"- Тип: `{report['kind']}`; результат: `{report['result']}`",
             f"- SHA: `{report['sha']}`; [run {report['run_id']}/{report['attempt']}]({report['url']})",
             f"- Проверочные jobs: {report['seconds'] if report['seconds'] is not None else 'unknown'} с (от начала run до последнего завершённого job)", ""]
    if report.get("checkout_sha"):
        lines.append(f"- SHA проверяемого checkout: `{report['checkout_sha']}`")
    if (previous is not None and previous["kind"] == report["kind"]
            and previous["workflow"] == report["workflow"]
            and report["seconds"] is not None and previous["seconds"] is not None):
        delta = report["seconds"] - previous["seconds"]
        lines.append(f"Сравнение с [run {previous['run_id']}/{previous['attempt']}]({previous['url']}) того же типа: {delta:+d} с.")
    lines.extend(["", "| Job / step | Результат | Секунды | SHA job |", "| --- | --- | ---: | --- |"])
    for job in report["jobs"]:
        lines.append(f"| {job['name']}{' (из прежней попытки)' if job['carried_over'] else ''} | {job['conclusion']} | {job['seconds'] if job['seconds'] is not None else 'unknown'} | `{job['sha']}` |")
        for step in job["steps"]:
            lines.append(f"| ↳ {step['name']} | {step['conclusion']} | {step['seconds'] if step['seconds'] is not None else 'unknown'} | |")
    lines.extend(["", "Метрики не являются допуском релиза. Итог run в GitHub окончателен после завершения отчётного job."])
    return "\n".join(lines) + "\n"


def comparable_previous(api: GitHub, report: dict) -> dict | None:
    """Находит ближайший завершённый run того же типа для сравнения времени.

    Args:
        api: Клиент GitHub с правом actions:read.
        report: Текущий проверенный отчёт.

    Returns:
        Предыдущий сопоставимый отчёт или None при отсутствии в 30 run.

    Raises:
        MetricsError: История API повреждена или недоступна.
    """
    payload = api.get(f"/actions/workflows/{report['workflow']}/runs", per_page=30)
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise MetricsError("GitHub API: нет истории workflow runs")
    for candidate in runs:
        if (not isinstance(candidate, dict) or candidate.get("id") == report["run_id"]
                or candidate.get("status") != "completed"
                or candidate.get("head_branch") != report["branch"]
                or candidate.get("event") != report["event"]
                or not isinstance(candidate.get("run_number"), int)
                or candidate["run_number"] >= report["run_number"]):
            continue
        candidate_id = candidate.get("id")
        attempt = candidate.get("run_attempt")
        sha = candidate.get("head_sha")
        if type(candidate_id) is not int or type(attempt) is not int or not isinstance(sha, str):
            raise MetricsError("GitHub API: некорректная история run")
        jobs = api.jobs(candidate_id, attempt)
        try:
            kind = classify(report["workflow"], jobs)
        except MetricsError:
            # Путь workflow общий для истории. Сравниваем только новый набор имён.
            continue
        if kind != report["kind"]:
            continue
        previous = summarize(candidate, jobs, report["workflow"], report["repository"],
                             candidate_id, attempt, sha)
        if ({job["name"] for job in previous["jobs"] if job["carried_over"]}
                != {job["name"] for job in report["jobs"] if job["carried_over"]}):
            continue
        if (previous["result"] == report["result"] != "pending"
                and [(job["name"], job["conclusion"]) for job in previous["jobs"]]
                == [(job["name"], job["conclusion"]) for job in report["jobs"]]):
            return previous
    return None


def main() -> None:
    """Читает попытку, сохраняет artifact/summary и явно сигнализирует сбой API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        workflow = os.environ["CI_WORKFLOW"]
        run_id = int(os.environ["GITHUB_RUN_ID"])
        attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
        # В pull_request GITHUB_SHA относится к тестовому merge, а API run — к head PR.
        sha = os.environ.get("CI_HEAD_SHA") or os.environ["GITHUB_SHA"]
        api = GitHub(repository, os.environ["GITHUB_TOKEN"])
        run = api.get(f"/actions/runs/{run_id}/attempts/{attempt}")
        report = summarize(run, api.jobs(run_id, attempt), workflow, repository,
                           run_id, attempt, sha)
        checkout_sha = os.environ["GITHUB_SHA"]
        if not SHA.fullmatch(checkout_sha):
            raise MetricsError("Некорректный SHA проверяемого checkout")
        report["checkout_sha"] = checkout_sha
        # История остаётся в per-attempt artifact; будущий отчёт может сверить тип.
        (args.output / "ci-metrics.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Отказ необязательной истории не должен скрывать уже собранный отчёт.
        try:
            previous = comparable_previous(api, report)
        except (KeyError, TypeError, ValueError, AttributeError, MetricsError):
            summary = render(report) + "\nСравнение недоступно: история API не подтверждена.\n"
        else:
            summary = render(report, previous)
    except (KeyError, TypeError, ValueError, AttributeError, MetricsError) as error:
        reason = str(error) if isinstance(error, MetricsError) else "Некорректное окружение или ответ API"
        summary = f"## CI metrics\n\nСбор метрик не удался: {reason}. Допуск релиза не меняется.\n"
        (args.output / "ci-metrics-error.json").write_text(
            json.dumps({"error": reason}, ensure_ascii=False) + "\n", encoding="utf-8")
        print(summary, file=sys.stderr)
        code = 1
    else:
        code = 0
    (args.output / "ci-metrics.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
