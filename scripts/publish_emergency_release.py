"""Публикует явно разрешённый аварийный выпуск с проверяемым журналом CI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import sys
from urllib.parse import quote

from scripts.check_hotfix_pairs import HotfixError, check_release
from scripts.create_release_branch import EXPECTED_REPOSITORY, ReleaseBranchError, checked_main_head
from scripts.full_ci_admission import (
    AdmissionError, MAIN_WORKFLOW, RELEASE_WORKFLOW, SHA_PATTERN, select_successful_run,
)
from scripts.full_ci_controller import ControllerError, GitHub, tag_commit
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.publish_release import (
    PublishError, _candidate, _check_number, _protection, _published_versions,
    _unused_name, _version,
)


class EmergencyPublishError(RuntimeError):
    """Означает, что аварийный манифест нельзя подтвердить или опубликовать."""


@dataclass(frozen=True)
class CheckSnapshot:
    """Фиксирует наблюдаемое состояние проверки и ссылку на источник."""

    state: str
    source: str


@dataclass(frozen=True)
class PullSnapshot:
    """Хранит подтверждённый PR и состояние быстрого CI при его слиянии."""

    number: int
    branch: str
    merge_sha: str
    merged_at: str
    fast: CheckSnapshot
    bypass: str
    rule_suite_id: int | None


@dataclass(frozen=True)
class PreparedEmergencyRelease:
    """Хранит неизменный снимок допуска и тело будущего GitHub Release."""

    version: str
    branch: str
    sha: str
    change: str
    reason: str
    notes: str
    pr_numbers: tuple[int, ...]
    pulls: tuple[PullSnapshot, ...]
    full: CheckSnapshot
    pairs: tuple[tuple[int, int, str], ...]
    body: str
    make_latest: str


def _time(value: object) -> datetime | None:
    """Разбирает точное UTC-время API; неизвестное значение оставляет пустым."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _state(conclusion: object) -> str:
    """Переводит завершённый вывод GitHub в ограниченный словарь состояний."""
    if conclusion == "success":
        return "success"
    if conclusion in ("failure", "timed_out", "cancelled", "action_required"):
        return "failure"
    return "unverified"


def _pull(api: GitHub, number: int, expected_branch: str, candidate: str,
          *, main_current: bool = False) -> dict:
    """Подтверждает слияние PR и достижимость merge SHA в кандидате."""
    value = api.get(f"/pulls/{number}")
    if (not isinstance(value, dict) or value.get("number") != number or
            value.get("merged_at") is None or _time(value.get("merged_at")) is None or
            not isinstance(value.get("base"), dict) or value["base"].get("ref") != expected_branch or
            not isinstance(value.get("head"), dict) or
            not isinstance(value["head"].get("sha"), str) or
            not SHA_PATTERN.fullmatch(value["head"]["sha"]) or
            not isinstance(value.get("merge_commit_sha"), str) or
            not SHA_PATTERN.fullmatch(value["merge_commit_sha"])):
        raise EmergencyPublishError(f"PR #{number} не подтверждён как слитый в {expected_branch}")
    merge_sha = value["merge_commit_sha"]
    commit = api.get(f"/git/commits/{merge_sha}")
    parents = commit.get("parents") if isinstance(commit, dict) else None
    if (not isinstance(parents, list) or len(parents) != 2 or
            not all(isinstance(parent, dict) for parent in parents) or
            parents[1].get("sha") != value["head"]["sha"]):
        raise EmergencyPublishError(f"PR #{number}: второй родитель merge не совпадает с head")
    target = candidate
    if main_current:
        main_ref = api.get("/git/ref/heads/main")
        if (not isinstance(main_ref, dict) or not isinstance(main_ref.get("object"), dict) or
                not isinstance(main_ref["object"].get("sha"), str) or
                not SHA_PATTERN.fullmatch(main_ref["object"]["sha"])):
            raise EmergencyPublishError("Текущий SHA main не подтверждён")
        target = main_ref["object"]["sha"]
    comparison = api.get(f"/compare/{merge_sha}...{target}")
    if (not isinstance(comparison, dict) or comparison.get("status") not in ("ahead", "identical") or
            not isinstance(comparison.get("base_commit"), dict) or
            comparison["base_commit"].get("sha") != merge_sha):
        raise EmergencyPublishError(f"PR #{number} не доказан предком candidate SHA")
    return value


def _fast_at_merge(api: GitHub, pr: dict) -> CheckSnapshot:
    """Восстанавливает состояние job pr-fast на момент merge по попыткам workflow."""
    merged = _time(pr["merged_at"])
    assert merged is not None
    metadata = api.get("/actions/workflows/pr-fast.yml")
    if (not isinstance(metadata, dict) or type(metadata.get("id")) is not int or
            metadata.get("path") != ".github/workflows/pr-fast.yml"):
        raise EmergencyPublishError("Workflow pr-fast нельзя идентифицировать")
    head = pr["head"]["sha"]
    # pull_request workflow проверяет синтетический merge commit: run.head_sha
    # не обязан совпадать с PR head. Связываем run по сохранённым данным PR.
    runs = api.pages("/actions/workflows/pr-fast.yml/runs", key="workflow_runs", event="pull_request")
    observed: list[tuple[datetime, int, int, CheckSnapshot]] = []
    relevant_run = False
    ambiguous_run = False
    for run in runs:
        if not isinstance(run, dict):
            raise EmergencyPublishError("Некорректный run pr-fast")
        if run.get("workflow_id") != metadata["id"] or run.get("event") != "pull_request":
            continue
        pull_refs = run.get("pull_requests")
        if (not isinstance(run.get("repository"), dict) or
                run["repository"].get("full_name") != api.repository or
                not isinstance(pull_refs, list)):
            ambiguous_run = True
            continue
        related = [item for item in pull_refs if isinstance(item, dict) and item.get("number") == pr["number"]]
        if not related:
            continue
        if (len(related) != 1 or not isinstance(related[0].get("head"), dict) or
                related[0]["head"].get("sha") != head or
                not isinstance(related[0].get("base"), dict) or
                related[0]["base"].get("ref") != pr["base"]["ref"]):
            ambiguous_run = True
            continue
        created = _time(run.get("created_at"))
        attempt_count = run.get("run_attempt")
        run_id = run.get("id")
        if created is None or type(attempt_count) is not int or attempt_count < 1 or type(run_id) is not int:
            raise EmergencyPublishError("Некорректные метаданные run pr-fast")
        if created > merged:
            continue
        relevant_run = True
        for attempt in range(1, attempt_count + 1):
            jobs = api.pages(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", key="jobs")
            matching = [job for job in jobs if isinstance(job, dict) and job.get("name") == "pr-fast"]
            if len(matching) > 1:
                raise EmergencyPublishError("Неоднозначный job pr-fast")
            if not matching:
                continue
            job = matching[0]
            started = _time(job.get("started_at")) or created
            if started > merged:
                continue
            completed = _time(job.get("completed_at"))
            state = _state(job.get("conclusion")) if completed and completed <= merged else "pending"
            source = f"https://github.com/{api.repository}/actions/runs/{run_id}/attempts/{attempt}"
            observed.append((started, run_id, attempt, CheckSnapshot(state, source)))
    if not observed:
        if relevant_run or ambiguous_run:
            return CheckSnapshot("unverified", "GitHub Actions: run найден, состояние job не подтверждено")
        if datetime.now(timezone.utc) - merged > timedelta(days=90):
            return CheckSnapshot("unverified", "GitHub Actions: доступная история run может быть неполной")
        return CheckSnapshot("not_run", "GitHub Actions: подходящий run для PR не найден")
    observed.sort(key=lambda item: (item[0], item[1], item[2]))
    return observed[-1][3]


def _bypass_at_merge(api: GitHub, pr: dict) -> tuple[str, int | None]:
    """Сверяет rule suite merge commit; отсутствие журнала оставляет неподтверждённым."""
    merged = _time(pr["merged_at"])
    assert merged is not None
    if datetime.now(timezone.utc) - merged > timedelta(days=30):
        return "unverified", None
    branch = pr["base"]["ref"]
    suites = api.pages("/rulesets/rule-suites", ref=f"refs/heads/{branch}", time_period="month")
    matches = [item for item in suites if isinstance(item, dict) and
               item.get("ref") == f"refs/heads/{branch}" and
               item.get("after_sha") == pr["merge_commit_sha"] and
               _time(item.get("pushed_at")) is not None and
               abs((_time(item["pushed_at"]) - merged).total_seconds()) <= 300]
    if len(matches) != 1 or type(matches[0].get("id")) is not int:
        return "unverified", None
    suite = api.get(f"/rulesets/rule-suites/{matches[0]['id']}")
    if not isinstance(suite, dict) or any(suite.get(key) != matches[0].get(key)
                                               for key in ("id", "ref", "after_sha", "result")):
        raise EmergencyPublishError("Детали rule suite не совпали со списком")
    evaluations = suite.get("rule_evaluations")
    if not isinstance(evaluations, list) or not any(
        isinstance(item, dict) and isinstance(item.get("rule_source"), dict) and
        item["rule_source"].get("id") == 25067104 for item in evaluations
    ):
        return "unverified", suite["id"]
    if suite.get("result") == "bypass" and suite.get("actor_id") == 42288695:
        return "yes", suite["id"]
    if suite.get("result") == "pass":
        return "no", suite["id"]
    return "unverified", suite["id"]


def _full_snapshot(api: GitHub, workflow: str, sha: str, branch: str) -> CheckSnapshot:
    """Описывает полный CI без превращения незавершённого run в успешный."""
    metadata = api.get(f"/actions/workflows/{workflow}")
    if (not isinstance(metadata, dict) or type(metadata.get("id")) is not int or
            metadata.get("path") != f".github/workflows/{workflow}"):
        raise EmergencyPublishError("Полный workflow нельзя идентифицировать")
    runs = api.pages(f"/actions/workflows/{workflow}/runs", key="workflow_runs", head_sha=sha)
    if any(not isinstance(item, dict) for item in runs):
        raise EmergencyPublishError("Некорректный список full runs")
    matching = [item for item in runs if item.get("head_sha") == sha and
                item.get("head_branch") == branch and item.get("workflow_id") == metadata["id"] and
                item.get("event") in ("push", "workflow_dispatch")]
    candidates = [item for item in matching if
                  isinstance(item.get("repository"), dict) and
                  item["repository"].get("full_name") == api.repository and
                  item.get("path") in (f".github/workflows/{workflow}",
                                       f".github/workflows/{workflow}@{branch}",
                                       f".github/workflows/{workflow}@refs/heads/{branch}")]
    if not candidates:
        if matching:
            return CheckSnapshot("unverified", "GitHub Actions: полный run не идентифицирован")
        return CheckSnapshot("not_run", "GitHub Actions: подходящий full run не найден")
    if any(_time(item.get("created_at")) is None or type(item.get("id")) is not int or
           item["id"] <= 0 or type(item.get("run_attempt")) is not int or
           item["run_attempt"] <= 0 for item in candidates):
        return CheckSnapshot("unverified", "GitHub Actions: метаданные full run неполны")
    latest = max(candidates, key=lambda item: (_time(item["created_at"]), item["id"], item["run_attempt"]))
    source = (f"https://github.com/{api.repository}/actions/runs/"
              f"{latest['id']}/attempts/{latest['run_attempt']}")
    if latest.get("status") in ("queued", "in_progress", "waiting", "requested", "pending"):
        return CheckSnapshot("pending", source)
    if latest.get("status") != "completed":
        return CheckSnapshot("unverified", source)
    state = _state(latest.get("conclusion"))
    if state == "success":
        admitted = select_successful_run(api, api.repository, workflow, sha, branch,
                                         latest["id"], latest["run_attempt"])
        if admitted is None:
            state = "unverified"
    return CheckSnapshot(state, source)


def prepare(api: PublishingGitHub, version: str, sha: str, change: str, reason: str,
            notes: str, pr_numbers: tuple[int, ...], *,
            _created_tag: bool = False) -> PreparedEmergencyRelease:
    """Проверяет аварийный кандидат и собирает подтверждённый манифест.

    Args:
        api: GitHub API выбранного репозитория.
        version: Каноническая новая версия.
        sha: Полный SHA tip релизной ветки.
        change: Тип SemVer изменения.
        reason: Обязательная причина срочного выпуска.
        notes: Описание содержимого выпуска.
        pr_numbers: Явно выбранные номера вошедших PR.

    Returns:
        Неизменный снимок перед публикацией.

    Raises:
        EmergencyPublishError: Выпуск нельзя подтвердить по GitHub и локальному Git.
    """
    parts = _version(version)
    user = api.authenticated_user()
    if not isinstance(user, dict) or user.get("id") != 42288695 or user.get("login") != "karateka-git":
        raise EmergencyPublishError("Аварийную публикацию выполняет только владелец User 42288695")
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise EmergencyPublishError("Требуется полный SHA candidate")
    if not reason.strip() or "\n" in reason or "\r" in reason:
        raise EmergencyPublishError("Требуется однострочная причина аварийного выпуска")
    if not notes.strip() or any(line.startswith(("Тип выпуска:", "Commit:", "Ветка:",
                                                "Причина:", "Полный CI:", "PR #", "- PR #",
                                                "PR и состояние на момент merge:",
                                                "Парные PR исправлений:",
                                                "Неподтверждённые или неуспешные проверки:",
                                                "Все наблюдаемые проверки успешны; аварийный допуск всё равно явный."))
                                for line in notes.splitlines()):
        raise EmergencyPublishError("Notes пусты или содержат служебные поля")
    if not pr_numbers or len(set(pr_numbers)) != len(pr_numbers) or any(type(n) is not int or n <= 0 for n in pr_numbers):
        raise EmergencyPublishError("Требуется непустой список уникальных номеров PR")
    branch = f"release/{parts[0]}.{parts[1]}"
    _candidate(api, branch, sha)
    history = _published_versions(api)
    _check_number(parts, change, history)
    if _created_tag:
        if tag_commit(api, version) != sha:
            raise EmergencyPublishError("Созданный тег больше не указывает на candidate")
        releases = api.pages("/releases")
        if any(not isinstance(item, dict) or item.get("tag_name") == version for item in releases):
            raise EmergencyPublishError("Имя Release занято после создания тега")
    else:
        _unused_name(api, version)
    _protection(api, version)
    line = [(number, item) for number, item in history.items() if number[:2] == parts[:2]]
    if line:
        baseline = tag_commit(api, max(line)[1]["tag_name"])
        pairs = tuple(check_release(api, branch, sha, baseline, require_full_ci=False))
        expected = {number for release, main, _ in pairs for number in (release, main)}
        if set(pr_numbers) != expected:
            raise EmergencyPublishError("Список PR должен точно совпадать с проверенными парами")
        workflow, ci_branch = RELEASE_WORKFLOW, branch
    else:
        checked_main_head(api, sha)
        pairs = ()
        workflow, ci_branch = MAIN_WORKFLOW, "main"
    pulls: list[PullSnapshot] = []
    for number in pr_numbers:
        expected_branch = (next((branch for release, main, _ in pairs if release == number), "main")
                           if pairs else "main")
        pr = _pull(api, number, expected_branch, sha,
                   main_current=bool(line and expected_branch == "main"))
        # Для PATCH парный merge в main может не быть предком release candidate.
        fast = _fast_at_merge(api, pr)
        bypass, suite_id = _bypass_at_merge(api, pr)
        pulls.append(PullSnapshot(number, expected_branch, pr["merge_commit_sha"],
                                  pr["merged_at"], fast, bypass, suite_id))
    full = _full_snapshot(api, workflow, sha, ci_branch)
    _candidate(api, branch, sha)
    if not _created_tag:
        _unused_name(api, version)
    elif tag_commit(api, version) != sha:
        raise EmergencyPublishError("Тег изменился во время проверки")
    if not line:
        checked_main_head(api, sha)
    lines = [notes.strip(), "", "Тип выпуска: аварийный", f"Commit: `{sha}`",
             f"Ветка: `{branch}`", f"Причина: {reason.strip()}",
             f"Полный CI: {workflow} {full.state} — {full.source}", "", "PR и состояние на момент merge:"]
    for item in pulls:
        suffix = f"; rule suite {item.rule_suite_id}" if item.rule_suite_id else ""
        lines.append(f"- PR #{item.number} -> {item.branch}; merge `{item.merge_sha}` "
                     f"({item.merged_at}); pr-fast={item.fast.state} [{item.fast.source}]; "
                     f"bypass={item.bypass}{suffix}")
    if pairs:
        lines.extend(("", "Парные PR исправлений:"))
        lines.extend(
            f"- release https://github.com/{api.repository}/pull/{release}, "
            f"main https://github.com/{api.repository}/pull/{main}; "
            f"main merge `{main_sha}` подтверждён в main"
            for release, main, main_sha in pairs
        )
    missing = [f"pr-fast PR #{item.number}: {item.fast.state}" for item in pulls
               if item.fast.state != "success"]
    if full.state != "success":
        missing.append(f"{workflow}: {full.state}")
    lines.extend(("", "Неподтверждённые или неуспешные проверки:",
                  *(f"- {item}" for item in missing)) if missing else
                 ("", "Все наблюдаемые проверки успешны; аварийный допуск всё равно явный."))
    make_latest = "true" if not history or parts > max(history) else "false"
    return PreparedEmergencyRelease(version, branch, sha, change, reason.strip(), notes,
                                    pr_numbers, tuple(pulls), full, pairs,
                                    "\n".join(lines) + "\n", make_latest)


def publish(api: PublishingGitHub, prepared: PreparedEmergencyRelease) -> str:
    """Повторяет допуск, создаёт тег и неизменяемый Release точного SHA."""
    if prepare(api, prepared.version, prepared.sha, prepared.change, prepared.reason,
               prepared.notes, prepared.pr_numbers) != prepared:
        raise EmergencyPublishError("Аварийный допуск изменился до записи")
    created = api.post("/git/refs", {"ref": f"refs/tags/{prepared.version}", "sha": prepared.sha})
    if (not isinstance(created, dict) or created.get("ref") != f"refs/tags/{prepared.version}" or
            not isinstance(created.get("object"), dict) or created["object"].get("sha") != prepared.sha or
            tag_commit(api, prepared.version) != prepared.sha):
        raise EmergencyPublishError("Создание точного тега не подтверждено; требуется ручной разбор")
    _candidate(api, prepared.branch, prepared.sha)
    # Повторная сверка после тега закрывает окно движения PR, CI и правил.
    if prepare(api, prepared.version, prepared.sha, prepared.change, prepared.reason,
               prepared.notes, prepared.pr_numbers, _created_tag=True) != prepared:
        raise EmergencyPublishError("Условия изменились после тега; Release не создан")
    release = api.post("/releases", {
        "tag_name": prepared.version, "target_commitish": prepared.sha,
        "name": prepared.version, "body": prepared.body,
        "draft": False, "prerelease": False, "make_latest": prepared.make_latest,
    })
    if (not isinstance(release, dict) or type(release.get("id")) is not int or
            release.get("tag_name") != prepared.version or release.get("draft") is not False or
            release.get("prerelease") is not False or not isinstance(release.get("html_url"), str)):
        raise EmergencyPublishError("Создание Release не подтверждено; требуется ручной разбор")
    confirmed = api.get(f"/releases/tags/{quote(prepared.version, safe='')}")
    if (not isinstance(confirmed, dict) or confirmed.get("id") != release["id"] or
            confirmed.get("tag_name") != prepared.version or
            confirmed.get("target_commitish") != prepared.sha or
            confirmed.get("body") != prepared.body or confirmed.get("immutable") is not True or
            confirmed.get("draft") is not False or confirmed.get("prerelease") is not False or
            not confirmed.get("published_at") or tag_commit(api, prepared.version) != prepared.sha):
        raise EmergencyPublishError("Опубликованный Release не подтверждён; требуется ручной разбор")
    return release["html_url"]


def main() -> None:
    """Запускает отдельный аварийный CLI с обязательной причиной и списком PR."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("sha")
    parser.add_argument("--change", required=True, choices=("initial", "major", "minor", "patch"))
    parser.add_argument("--reason", required=True)
    parser.add_argument("--notes-file", required=True, type=Path)
    parser.add_argument("--pr", required=True, type=int, action="append")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        notes = args.notes_file.read_text(encoding="utf-8")
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        prepared = prepare(api, args.version, args.sha, args.change, args.reason,
                           notes, tuple(args.pr))
        print(f"Аварийный допуск: {prepared.version} -> {prepared.sha}; полный CI: {prepared.full.state}")
        if args.dry_run:
            print(prepared.body)
        if args.publish:
            print(f"Опубликовано: {publish(api, prepared)}")
    except (OSError, AdmissionError, ControllerError, HotfixError, ReleaseBranchError,
            PublishError, PublishingError, EmergencyPublishError, KeyError, TypeError,
            ValueError) as error:
        print(f"Emergency Release: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
