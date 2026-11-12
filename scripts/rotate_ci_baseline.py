"""Готовит reviewed policy и подтверждает полный CI для новой production-базы."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from scripts.check_repository_compatibility import export_commit
from scripts.create_release_branch import (
    EXPECTED_REPOSITORY, ReleaseBranchError, checked_main_head, git, repository_from_origin,
)
from scripts.deploy_prepare import PreparationError
from scripts.deployment_acceptance import (
    DeploymentAcceptanceError, LEGACY_INSTALLATION_VERSION, require_accepted_deployment,
)
from scripts.full_ci_admission import Admission, AdmissionError, MAIN_WORKFLOW, PR_CHECK, PR_WORKFLOW, SHA_PATTERN
from scripts.full_ci_controller import ControllerError, GitHub
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.predeploy_compatibility import EvidenceAPI, _blob, _trusted_policy, verify_evidence
from scripts.publish_emergency_release import EmergencyPublishError, _fast_at_merge, _pull, _time
from scripts.publish_installation_tag import (
    InstallationReceipt, InstallationTagError, installation_tag_exists, read_receipt, require_published_release,
)
from scripts.publish_release import PublishError, _policy_pin
from scripts.repository_compatibility_contract import load_policy, verify_inventory
from scripts.select_deploy_release import DeploySelectionError


POLICY_FILE = "scripts/repository_compatibility_policy.json"
PIN_NAME = "COMPATIBILITY_POLICY_SHA"
DISPATCH_PATH = f"/actions/workflows/{MAIN_WORKFLOW}/dispatches"
RUN_TITLE_PREFIX = "CI baseline "
# GitHub отдаёт текст журнала задачи через временный URL хранилища.
MAX_PR_JOB_LOG_BYTES = 4 * 1024 * 1024
# ID приложения GitHub Actions на github.com.
GITHUB_ACTIONS_APP_ID = 15368


class RotationError(RuntimeError):
    """Означает, что ротацию нельзя продолжить по подтверждённым данным."""


@dataclass(frozen=True)
class RotationPlan:
    """Связывает свежую приёмку, reviewed merge и точный текущий SHA main."""

    receipt: InstallationReceipt
    policy_sha: str
    main_sha: str


def _sha(value: object, description: str) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise RotationError(f"Не подтверждён полный SHA: {description}")
    return value


def _positive(value: object, description: str) -> int:
    if type(value) is not int or value < 1:
        raise RotationError(f"Некорректный номер: {description}")
    return value


class _JobLogRedirect(HTTPRedirectHandler):
    """Принимает только HTTPS-ссылку на журнал GitHub Actions."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Отклоняет переадресацию, если GitHub не подтвердил защищённый URL."""
        if not newurl.startswith("https://"):
            raise RotationError("GitHub вернул незащищённую ссылку на журнал pr-check")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RotationGitHub(PublishingGitHub):
    """Читает журналы проверок и выполняет явно разрешённые записи ротации."""

    def job_log(self, job_id: int) -> str:
        """Читает ограниченный по размеру журнал точной задачи, сохраняя token в API.

        Args:
            job_id: ID подтверждённой задачи GitHub Actions.

        Returns:
            Текст журнала, который не выводится оператору.

        Raises:
            RotationError: Журнал не получен или превышает лимит.
        """
        job_id = _positive(job_id, "задача pr-check")
        request = Request(f"{self.root}/actions/jobs/{job_id}/logs", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with build_opener(_JobLogRedirect()).open(request, timeout=20) as response:
                data = response.read(MAX_PR_JOB_LOG_BYTES + 1)
        except OSError as error:
            raise RotationError("GitHub не подтвердил журнал задачи pr-check") from error
        if len(data) > MAX_PR_JOB_LOG_BYTES:
            raise RotationError("Журнал задачи pr-check превышает лимит")
        return data.decode("utf-8", errors="replace")

    def _write(self, method: str, path: str, payload: dict, expected_status: int) -> object:
        request = Request(self.root + path, data=json.dumps(payload).encode("utf-8"), method=method, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "Content-Type": "application/json",
            "User-Agent": "compadre-ci-baseline",
        })
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with urlopen(request, timeout=20) as response:
                if response.status != expected_status:
                    raise RotationError("GitHub не подтвердил действие ротации")
                return None if expected_status == 204 else json.load(response)
        except (OSError, ValueError) as error:
            raise RotationError("GitHub не подтвердил действие ротации") from error

    def update_policy_pin(self, sha: str) -> None:
        """Обновляет существующую переменную reviewed policy точным SHA.

        Args:
            sha: Полный SHA reviewed merge с новой базой CI.

        Raises:
            RotationError: Запись не подтверждена ответом GitHub.
        """
        self._write("PATCH", f"/actions/variables/{PIN_NAME}", {"value": _sha(sha, PIN_NAME)}, 204)

    def dispatch_rotation(self, plan: RotationPlan) -> int:
        """Запускает pre-release-check-full с заданными pin и SHA.

        Args:
            plan: Свежая принятая установка, reviewed pin и точный SHA main.

        Returns:
            ID запуска из ответа GitHub.

        Raises:
            RotationError: Ответ GitHub или ID запуска не подтверждён.
        """
        result = self._write("POST", DISPATCH_PATH, {"ref": "main", "inputs": {
            "rotation_policy_sha": plan.policy_sha, "rotation_main_sha": plan.main_sha,
        }}, 200)
        if not isinstance(result, dict):
            raise RotationError("GitHub не подтвердил ID запуска ротации")
        return _positive(result.get("workflow_run_id"), "запуск ротации")


def require_accepted_installation(api: GitHub, receipt: InstallationReceipt) -> None:
    """Подтверждает запись приёмки текущей установки по её эпохе.

    Args:
        api: Клиент чтения Release, тега или Deployments ожидаемого репозитория.
        receipt: Свежая техническая приёмка текущей установки.

    Raises:
        RotationError: Исторический тег установки не опубликован.
        InstallationTagError: Исторический тег или Release не совпадает с VPS.
        DeploymentAcceptanceError: Новая запись не подтверждена статусом success.
    """
    if receipt.version == LEGACY_INSTALLATION_VERSION:
        require_published_release(api, receipt)
        if not installation_tag_exists(api, receipt.version, receipt.sha, receipt=receipt):
            raise RotationError("Для исторической приёмки отсутствует тег установки")
    else:
        require_accepted_deployment(api, receipt)


def _fresh(read_fresh: Callable[[], InstallationReceipt], receipt: InstallationReceipt) -> None:
    if read_fresh() != receipt:
        raise RotationError("Техническая приёмка VPS изменилась в ходе ротации")


def _policy_for_receipt(policy: dict, receipt: InstallationReceipt) -> dict:
    return {**policy, "previous_sha": receipt.sha, "release_tag": receipt.version, "release_id": receipt.release_id}


def prepare_policy(api: PublishingGitHub, read_fresh: Callable[[], InstallationReceipt]) -> None:
    """Готовит в чистом worktree изменение трёх полей policy для отдельного PR.

    Args:
        api: Клиент ожидаемого GitHub-репозитория для чтения Release и тега установки.
        read_fresh: Получает актуальную техническую приёмку с доверенного VPS.

    Raises:
        RotationError: Приёмка, worktree или точная матрица старого API не подтверждены.
    """
    receipt = read_fresh()
    require_accepted_installation(api, receipt)
    path = Path(POLICY_FILE)
    policy = load_policy(path)
    desired = _policy_for_receipt(policy, receipt)
    with TemporaryDirectory(prefix="ci-baseline-") as temporary:
        previous = Path(temporary) / "previous"
        export_commit(Path.cwd(), receipt.sha, previous)
        verify_inventory(previous, desired)
    if desired == policy:
        return
    _fresh(read_fresh, receipt)
    if git("status", "--porcelain").stdout:
        raise RotationError("Для подготовки CI policy нужен чистый worktree")
    with TemporaryDirectory(prefix=".ci-policy-", dir=path.parent) as temporary:
        pending = Path(temporary) / path.name
        pending.write_text(json.dumps(desired, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        pending.replace(path)


def _main_sha(api: GitHub) -> str:
    reference = api.get("/git/ref/heads/main")
    if (not isinstance(reference, dict) or reference.get("ref") != "refs/heads/main"
            or not isinstance(reference.get("object"), dict) or reference["object"].get("type") != "commit"):
        raise RotationError("Текущий ref main не подтверждён")
    return _sha(reference["object"].get("sha"), "main")


def _rotation_pr_run(run: object, workflow_id: int, head: dict, repository: str) -> bool:
    return (isinstance(run, dict) and run.get("workflow_id") == workflow_id
            and run.get("path") == f".github/workflows/{PR_WORKFLOW}" and run.get("event") == "pull_request"
            and run.get("head_sha") == head["sha"] and run.get("head_branch") == head["ref"]
            and isinstance(run.get("repository"), dict) and run["repository"].get("full_name") == repository
            and isinstance(run.get("head_repository"), dict)
            and run["head_repository"].get("full_name") == head["repo"]["full_name"])


def _test_merge_checkout(log: str, pr: int, job: dict) -> str:
    all_steps = job.get("steps")
    if not isinstance(all_steps, list) or any(not isinstance(step, dict) for step in all_steps):
        raise RotationError("GitHub не подтвердил шаги задачи pr-check")
    steps = [step for step in all_steps if step.get("name") == "Checkout test merge"]
    if len(steps) != 1 or steps[0].get("conclusion") != "success":
        raise RotationError("Не подтверждён шаг checkout точного test merge")
    started, completed = _time(steps[0].get("started_at")), _time(steps[0].get("completed_at"))
    job_started, job_completed = _time(job.get("started_at")), _time(job.get("completed_at"))
    if not (started and completed and job_started and job_completed
            and job_started <= started <= completed <= job_completed):
        raise RotationError("Не подтверждено время шага checkout")
    ref = f"refs/remotes/pull/{pr}/merge"
    fetch = re.compile(r"\[command\]/usr/bin/git (?:-c protocol\.version=2 )?fetch .+ \+([0-9a-f]{40}):"
                       + re.escape(ref))
    # GitHub округляет время шага до секунды, а строки журнала точнее.
    messages = []
    for line in log.splitlines():
        stamp, _, message = line.partition(" ")
        when = _time(stamp)
        if when and started <= when and when - completed < timedelta(seconds=1):
            messages.append(message)
    fetched = [(i, match[1]) for i, message in enumerate(messages) if (match := fetch.fullmatch(message))]
    checkout = f"[command]/usr/bin/git checkout --progress --force {ref}"
    if len(fetched) == 1 and messages.count(checkout) == 1:
        index, sha = fetched[0]
        checkout_index = messages.index(checkout)
        if index < checkout_index and any(
            command == "[command]/usr/bin/git log -1 --format=%H" and value == sha
            for command, value in zip(messages[checkout_index + 1:], messages[checkout_index + 2:])
        ):
            return sha
    raise RotationError("Журнал pr-check не подтверждает checkout test merge этого PR")


def _verify_pr_check_from_checkout(api: RotationGitHub, pr: dict, merge_commit: dict) -> None:
    """Проверяет PR по точной попытке и test merge, если GitHub очистил связь run с PR."""
    merged_at = _time(pr.get("merged_at"))
    head, base = pr["head"], pr["base"]
    workflow = api.get(f"/actions/workflows/{PR_WORKFLOW}")
    if not merged_at or not isinstance(workflow, dict) or workflow.get("path") != f".github/workflows/{PR_WORKFLOW}":
        raise RotationError("Не подтверждены время merge или workflow pr-check")
    if not head.get("ref") or not isinstance(head.get("repo"), dict) or not head["repo"].get("full_name"):
        raise RotationError("Не подтверждён источник head для PR смены базы CI")
    workflow_id = _positive(workflow.get("id"), "workflow pr-check")
    attempts = []
    for run in api.pages(f"/actions/workflows/{PR_WORKFLOW}/runs", key="workflow_runs",
                         event="pull_request", head_sha=head["sha"]):
        if not isinstance(run, dict) or not isinstance(run.get("head_branch"), str):
            raise RotationError("Не подтверждены метаданные запуска pr-check")
        if run.get("head_branch") != head["ref"]:
            continue
        created = _time(run.get("created_at"))
        if not created or not _rotation_pr_run(run, workflow_id, head, api.repository):
            raise RotationError("Не подтверждены метаданные запуска pr-check")
        if created > merged_at:
            continue
        run_id = _positive(run.get("id"), "запуск pr-check")
        for number in range(1, _positive(run.get("run_attempt"), "попытка pr-check") + 1):
            path = f"/actions/runs/{run_id}/attempts/{number}"
            attempt = api.get(path)
            started = _time(attempt.get("run_started_at")) if isinstance(attempt, dict) else None
            if (not started or not _rotation_pr_run(attempt, workflow_id, head, api.repository)
                    or attempt.get("id") != run_id or attempt.get("run_attempt") != number):
                raise RotationError("Не подтверждены метаданные точной попытки pr-check")
            if started <= merged_at:
                attempts.append((started, path, attempt))
    attempts.sort(key=lambda item: item[0], reverse=True)
    if not attempts or (len(attempts) > 1 and attempts[0][0] == attempts[1][0]):
        raise RotationError("Не подтверждена последняя попытка pr-check до merge")
    started, path, attempt = attempts[0]
    if attempt.get("pull_requests") != []:
        raise RotationError("GitHub вернул другую связь попытки pr-check с PR")
    jobs = api.pages(f"{path}/jobs", key="jobs")
    if any(not isinstance(job, dict) for job in jobs):
        raise RotationError("GitHub не подтвердил задачи точной попытки pr-check")
    jobs = [job for job in jobs if job.get("name") == PR_CHECK]
    if len(jobs) != 1:
        raise RotationError("Не подтверждена единственная задача pr-check точной попытки")
    job = jobs[0]
    job_started, completed = _time(job.get("started_at")), _time(job.get("completed_at"))
    if (job.get("run_id") != attempt["id"] or job.get("run_attempt") != attempt["run_attempt"]
            or job.get("head_sha") != head["sha"] or job.get("status") != "completed"
            or job.get("conclusion") != "success" or not job_started or not completed
            or not started <= job_started <= completed < merged_at):
        raise RotationError("Последняя попытка не подтверждает успешный pr-check до merge")
    job_id = _positive(job.get("id"), "задача pr-check")
    check = api.get(f"/check-runs/{job_id}")
    if (not isinstance(check, dict) or check.get("id") != job_id or check.get("name") != PR_CHECK
            or check.get("head_sha") != head["sha"]
            or check.get("status") != "completed" or check.get("conclusion") != "success"
            or check.get("completed_at") != job["completed_at"]
            or not isinstance(check.get("app"), dict) or check["app"].get("slug") != "github-actions"
            or check["app"].get("id") != GITHUB_ACTIONS_APP_ID or not isinstance(check.get("check_suite"), dict)
            or check["check_suite"].get("id") != _positive(attempt.get("check_suite_id"), "check suite pr-check")):
        raise RotationError("GitHub Actions не подтвердил задачу pr-check точной попытки")
    sha = _test_merge_checkout(api.job_log(job_id), pr["number"], job)
    test_merge = api.get(f"/git/commits/{sha}")
    parents = [_sha(base.get("sha"), "base PR"), head["sha"]]
    merge_tree = merge_commit.get("tree")
    if (not isinstance(test_merge, dict) or test_merge.get("sha") != sha
            or not isinstance(test_merge.get("parents"), list)
            or any(not isinstance(item, dict) for item in test_merge["parents"])
            or [item.get("sha") for item in test_merge["parents"]] != parents
            or [item.get("sha") for item in merge_commit["parents"]] != parents
            or not isinstance(merge_tree, dict) or not isinstance(test_merge.get("tree"), dict)
            or test_merge["tree"].get("sha") != _sha(merge_tree.get("sha"), "дерево reviewed merge")):
        raise RotationError("Test merge pr-check не совпадает с родителями и деревом reviewed merge")


def review_rotation(api: RotationGitHub, receipt: InstallationReceipt, *, pr: int, main_sha: str,
                    resume: bool = False) -> RotationPlan:
    """Подтверждает принятую установку и reviewed merge с единственным изменением policy.

    Args:
        api: Клиент GitHub для проверки Release, PR и истории main.
        receipt: Свежая техническая приёмка установленной версии.
        pr: Номер отдельного PR с обновлением baseline.
        main_sha: Явно выбранный полный SHA main для полного CI.
        resume: Разрешает завершить проверку существующего run, если main продвинулся.

    Returns:
        План ротации с точным reviewed pin.

    Raises:
        RotationError: Приёмка, PR, текущий main или diff policy не доказаны.
    """
    require_accepted_installation(api, receipt)
    main_sha = _sha(main_sha, "ожидаемый main")
    current_main = checked_main_head(api, main_sha) if resume else _main_sha(api)
    if not resume and current_main != main_sha:
        raise RotationError("Main изменился с момента выбора SHA для ротации")
    pull = _pull(api, _positive(pr, "PR"), "main", main_sha)
    policy_sha = pull["merge_commit_sha"]
    commit = api.get(f"/git/commits/{policy_sha}")
    parents = commit.get("parents") if isinstance(commit, dict) else None
    if (not isinstance(commit, dict) or commit.get("sha") != policy_sha
            or not isinstance(parents, list) or len(parents) != 2 or not isinstance(parents[0], dict)):
        raise RotationError("GitHub не подтвердил точный merge commit policy")
    parent = _sha(parents[0].get("sha"), "первый родитель policy")
    for resource in (f"/pulls/{pr}/files", f"/commits/{policy_sha}"):
        files = api.pages(resource, key="files" if resource.startswith("/commits/") else None)
        if (len(files) != 1 or not isinstance(files[0], dict) or files[0].get("filename") != POLICY_FILE
                or files[0].get("status") != "modified"):
            raise RotationError("PR и merge должны менять только файл CI policy")
    old_policy = _trusted_policy(_blob(api, parent, POLICY_FILE))
    new_policy = _trusted_policy(_blob(api, policy_sha, POLICY_FILE))
    desired = _policy_for_receipt(old_policy, receipt)
    if old_policy == desired or new_policy != desired:
        raise RotationError("Reviewed policy должна менять только поля базы на данные принятого выпуска")
    if _trusted_policy(_blob(api, main_sha, POLICY_FILE)) != desired:
        raise RotationError("На выбранном main CI policy отличается от reviewed merge")
    if current_main != main_sha and _trusted_policy(_blob(api, current_main, POLICY_FILE)) != desired:
        raise RotationError("Текущий main больше не содержит проверенную CI policy")
    fast = _fast_at_merge(api, pull)
    if fast.state == "not_run":
        _verify_pr_check_from_checkout(api, pull, commit)
    elif fast.state != "success":
        raise RotationError("Для merge CI policy не подтверждён успешный pr-check")
    return RotationPlan(receipt, policy_sha, main_sha)


def _owner(api: PublishingGitHub) -> None:
    if api.authenticated_user().get("login") != api.repository.split("/", 1)[0]:
        raise RotationError("Изменить CI pin или запустить ротацию может владелец репозитория")


def pin(api: RotationGitHub, plan: RotationPlan, *, expected_old: str,
        read_fresh: Callable[[], InstallationReceipt]) -> None:
    """Обновляет pin от ожидаемого значения к reviewed merge и читает его обратно.

    Args:
        api: GitHub-клиент оператора с Variables write.
        plan: Проверенный reviewed merge и принятая установка.
        expected_old: Явно подтверждённый прежний GitHub pin.
        read_fresh: Повторно получает техническую приёмку перед записью.

    Raises:
        RotationError: Pin, оператор или VPS изменились, запись не подтверждена.
    """
    _sha(expected_old, "прежний CI pin")
    _owner(api)
    _fresh(read_fresh, plan.receipt)
    require_accepted_installation(api, plan.receipt)
    if _main_sha(api) != plan.main_sha:
        raise RotationError("Main изменился перед обновлением CI pin")
    current = _policy_pin(api)
    if current == plan.policy_sha:
        return
    if current != expected_old:
        raise RotationError("CI pin отличается от ожидаемого прежнего значения")
    try:
        api.update_policy_pin(plan.policy_sha)
    except RotationError:
        if _policy_pin(api) != plan.policy_sha:
            raise
    if _policy_pin(api) != plan.policy_sha:
        raise RotationError("GitHub не подтвердил новый CI pin")


def dispatch(api: RotationGitHub, plan: RotationPlan, *, read_fresh: Callable[[], InstallationReceipt]) -> int:
    """Явно запускает полный CI с новым pin и не повторяет сомнительную запись.

    Args:
        api: GitHub-клиент оператора с Actions write.
        plan: Проверенный reviewed merge и принятая установка.
        read_fresh: Повторно получает техническую приёмку перед запуском.

    Returns:
        ID подтверждённого GitHub запуска.

    Raises:
        RotationError: VPS, main, pin или ответ о запуске не подтверждены.
    """
    _owner(api)
    _fresh(read_fresh, plan.receipt)
    require_accepted_installation(api, plan.receipt)
    if _main_sha(api) != plan.main_sha or _policy_pin(api) != plan.policy_sha:
        raise RotationError("Main или новый CI pin изменились перед запуском")
    runs = api.pages(f"/actions/workflows/{MAIN_WORKFLOW}/runs", key="workflow_runs", head_sha=plan.main_sha)
    for run in runs:
        if not isinstance(run, dict) or run.get("head_sha") != plan.main_sha:
            raise RotationError("GitHub не подтвердил список точных запусков CI")
        if run.get("head_branch") != "main":
            continue
        if (run.get("event") == "workflow_dispatch"
                and run.get("display_title") == RUN_TITLE_PREFIX + plan.policy_sha):
            run_id = _positive(run.get("id"), "существующий запуск ротации")
            raise RotationError(f"Ротация уже имеет run={run_id}. Проверьте или повторите его попытку")
        if run.get("status") != "completed":
            raise RotationError("Уже есть незавершённый CI этого SHA. Сначала проверьте его попытку")
    try:
        return api.dispatch_rotation(plan)
    except RotationError as error:
        raise RotationError("Запуск CI не подтверждён. Найдите точный run в GitHub перед новым запуском") from error


def verify_rotation_run(api: EvidenceAPI, *, receipt: InstallationReceipt, policy_sha: str, main_sha: str,
                        run_id: int, attempt: int) -> Admission:
    """Подтверждает точный полный pre-release-check-full под новым reviewed pin и базой.

    Args:
        api: Клиент чтения GitHub Actions, Contents и Variables.
        receipt: SHA и Release принятой установки на VPS.
        policy_sha: SHA reviewed merge новой policy.
        main_sha: Точный SHA main выбранной ротации.
        run_id: ID явного запуска полного CI.
        attempt: Номер проверяемой попытки CI.

    Returns:
        Проверенная полная попытка для независимой активации на VPS.

    Raises:
        RotationError: Pin либо точная успешная попытка не подтверждены.
        ValueError: Полное evidence не соответствует новой базе и pin.
    """
    previous_sha = _sha(receipt.sha, "установленная база")
    policy_sha = _sha(policy_sha, "reviewed CI pin")
    main_sha = _sha(main_sha, "main прогона")
    run_id, attempt = _positive(run_id, "CI run"), _positive(attempt, "попытка CI")
    if api.repository != EXPECTED_REPOSITORY or _policy_pin(api) != policy_sha:
        raise RotationError("GitHub pin не соответствует reviewed ротации")
    run = api.get(f"/actions/runs/{run_id}/attempts/{attempt}")
    if (not isinstance(run, dict) or type(run.get("id")) is not int or type(run.get("run_attempt")) is not int
            or run["id"] != run_id or run["run_attempt"] != attempt
            or run.get("event") != "workflow_dispatch" or run.get("head_sha") != main_sha
            or run.get("head_branch") != "main" or run.get("status") != "completed"
            or run.get("display_title") != RUN_TITLE_PREFIX + policy_sha
            or run.get("conclusion") != "success"):
        raise RotationError("Не подтверждена указанная успешная попытка ручного pre-release-check-full")
    policy = _trusted_policy(_blob(api, policy_sha, POLICY_FILE))
    if policy != _policy_for_receipt(policy, receipt):
        raise RotationError("Новый CI pin не указывает на принятый Release VPS")
    admission = Admission(api.repository, MAIN_WORKFLOW, "main", main_sha, run_id, attempt)
    verify_evidence(api, admission, policy_sha, previous_sha)
    if _policy_pin(api) != policy_sha:
        raise RotationError("GitHub pin изменился во время проверки полного CI")
    return admission


def main() -> None:
    """Выполняет один явный шаг ротации из операторского worktree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("sha", help="полный SHA принятой версии")
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--known-hosts", required=True, type=Path)
    parser.add_argument("--identity", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="подготовить файл policy для отдельного PR")
    update = commands.add_parser("pin", help="обновить GitHub pin после слияния PR")
    start = commands.add_parser("dispatch", help="явно запустить pre-release-check-full с новым pin")
    finish = commands.add_parser("finish", help="подтвердить точный CI run для активации на VPS")
    for command in (update, start, finish):
        command.add_argument("--pr", required=True, type=int)
        command.add_argument("--main-sha", required=True)
    update.add_argument("--expected-old-pin", required=True)
    finish.add_argument("--run-id", required=True, type=int)
    finish.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args()

    def read_fresh() -> InstallationReceipt:
        return read_receipt(args.version, args.sha, ssh_host=args.ssh_host,
                            known_hosts=args.known_hosts, identity=args.identity)

    try:
        if repository_from_origin() != EXPECTED_REPOSITORY:
            raise RotationError("Origin worktree не совпадает с репозиторием проекта")
        api = RotationGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if args.command == "prepare":
            prepare_policy(api, read_fresh)
            print("Policy подготовлена для отдельного PR")
            return
        plan = review_rotation(api, read_fresh(), pr=args.pr, main_sha=args.main_sha,
                               resume=args.command == "finish")
        if args.command == "pin":
            pin(api, plan, expected_old=args.expected_old_pin, read_fresh=read_fresh)
            print(f"Подтверждён GitHub CI pin {plan.policy_sha}")
        elif args.command == "dispatch":
            print(f"Запущен pre-release-check-full run={dispatch(api, plan, read_fresh=read_fresh)}")
        else:
            verify_rotation_run(EvidenceAPI(api.repository, api.token), receipt=plan.receipt,
                                policy_sha=plan.policy_sha, main_sha=plan.main_sha,
                                run_id=args.run_id, attempt=args.attempt)
            _fresh(read_fresh, plan.receipt)
            print(json.dumps({"policy_sha": plan.policy_sha, "main_sha": plan.main_sha,
                              "run_id": args.run_id, "attempt": args.attempt,
                              "runtime_id": plan.receipt.runtime_id}, sort_keys=True))
    except (AdmissionError, ControllerError, DeploymentAcceptanceError, DeploySelectionError,
            EmergencyPublishError, InstallationTagError,
            PreparationError, PublishError, PublishingError, ReleaseBranchError, RotationError,
            OSError, subprocess.CalledProcessError, KeyError, TypeError, ValueError) as error:
        print(f"Ротация CI policy: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
