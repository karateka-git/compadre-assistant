"""Проверяет read-only допуск точного commit по результатам полного GitHub CI."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol


MAIN_WORKFLOW = "main-full.yml"
RELEASE_WORKFLOW = "release-full.yml"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_BRANCH_PATTERN = re.compile(r"release/(\d+)\.(\d+)\Z")
FULL_STEPS = frozenset({
    "Checkout exact candidate", "Verify candidate commit", "Setup Python",
    "Install runtime dependencies", "Run full unittest suite",
    "Run three autonomous smoke checks", "Check fresh and previous SQLite schemas",
    "Build and healthcheck candidate image", "Publish check manifest",
})


class AdmissionError(RuntimeError):
    """Означает, что ответ API не позволяет надёжно проверить допуск."""


class ReadOnlyGitHub(Protocol):
    """Описывает операции чтения GitHub API, нужные selector."""

    repository: str

    def get(self, path: str, **parameters: str | int) -> object:
        """Получает один JSON-ответ API."""
        ...

    def pages(self, path: str, key: str | None = None, **parameters: str | int) -> list[object]:
        """Получает все страницы списка API."""
        ...


@dataclass(frozen=True)
class Admission:
    """Фиксирует проверенную попытку полного CI для точного commit."""

    repository: str
    workflow: str
    branch: str
    sha: str
    run_id: int
    run_attempt: int


def _object(value: object, description: str) -> dict:
    """Проверяет структуру обязательного объекта ответа API."""
    if not isinstance(value, dict):
        raise AdmissionError(f"Некорректный объект GitHub API: {description}")
    return value


def _positive_int(value: object, description: str) -> int:
    """Проверяет положительный числовой идентификатор API."""
    if type(value) is not int or value <= 0:
        raise AdmissionError(f"Некорректный идентификатор GitHub API: {description}")
    return value


def _names_and_steps(jobs: list[object], workflow: str, sha: str) -> bool:
    """Проверяет каждый обязательный job и шаг одной попытки."""
    caller = "main-full" if workflow == MAIN_WORKFLOW else "release-full"
    required = {
        f"{caller}-controller": {
            "Checkout exact main commit" if workflow == MAIN_WORKFLOW
            else "Checkout exact release commit", "Decide full run",
        },
        f"{caller} / full-suite": FULL_STEPS,
    }
    seen: set[str] = set()
    for raw_job in jobs:
        job = _object(raw_job, "job")
        name = job.get("name")
        if not isinstance(name, str) or name in seen:
            raise AdmissionError("Отсутствует или повторяется имя job")
        seen.add(name)
        if job.get("head_sha") != sha or job.get("status") != "completed" or job.get("conclusion") != "success":
            return False
        raw_steps = job.get("steps")
        if not isinstance(raw_steps, list):
            raise AdmissionError(f"Нет списка шагов job {name}")
        steps: dict[str, dict] = {}
        for raw_step in raw_steps:
            step = _object(raw_step, "step")
            step_name = step.get("name")
            if not isinstance(step_name, str) or step_name in steps:
                raise AdmissionError(f"Отсутствует или повторяется имя шага job {name}")
            steps[step_name] = step
        if any(step.get("status") != "completed" or step.get("conclusion") != "success"
               for step in steps.values()):
            return False
        if name in required and not required[name].issubset(steps):
            return False
    return required.keys() <= seen


def select_successful_run(
    api: ReadOnlyGitHub, repository: str, workflow: str, sha: str, branch: str,
) -> Admission | None:
    """Находит успешную полную попытку CI для точного SHA и ветки.

    Args:
        api: Клиент GitHub API с доступом только на чтение.
        repository: Ожидаемое полное имя репозитория owner/repo.
        workflow: Имя разрешённого caller workflow.
        sha: Полный commit SHA, который требуется допустить.
        branch: Ветка исходного запуска, main либо release/X.Y.

    Returns:
        Данные проверенной попытки или None, если допуска нет.

    Raises:
        AdmissionError: Запрос или ответ API неоднозначен либо повреждён.
    """
    if workflow not in (MAIN_WORKFLOW, RELEASE_WORKFLOW):
        raise AdmissionError("Неизвестный caller workflow")
    if not SHA_PATTERN.fullmatch(sha):
        raise AdmissionError("Требуется полный commit SHA")
    if ((workflow == MAIN_WORKFLOW and branch != "main") or
            (workflow == RELEASE_WORKFLOW and not RELEASE_BRANCH_PATTERN.fullmatch(branch))):
        raise AdmissionError("Недопустимая ветка caller workflow")
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or api.repository != repository):
        raise AdmissionError("Репозиторий API не совпадает с ожидаемым")

    metadata = _object(api.get(f"/actions/workflows/{workflow}"), "workflow")
    expected_path = f".github/workflows/{workflow}"
    workflow_id = _positive_int(metadata.get("id"), "workflow")
    if metadata.get("path") != expected_path:
        raise AdmissionError("Workflow API указывает на другой файл")
    runs = api.pages(
        f"/actions/workflows/{workflow}/runs", key="workflow_runs", head_sha=sha,
    )
    for raw_run in runs:
        run = _object(raw_run, "run")
        # Фильтр head_sha на сервере не заменяет проверку полей ответа.
        if run.get("head_sha") != sha or run.get("head_branch") != branch:
            continue
        if (_object(run.get("repository"), "run.repository").get("full_name") != api.repository or
                _object(run.get("head_repository"), "run.head_repository").get("full_name") != api.repository or
                run.get("workflow_id") != workflow_id or
                run.get("path") not in (expected_path, f"{expected_path}@{branch}",
                                        f"{expected_path}@refs/heads/{branch}")):
            continue
        if run.get("event") not in ("push", "workflow_dispatch"):
            continue
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        run_id = _positive_int(run.get("id"), "run")
        attempt = _positive_int(run.get("run_attempt"), "run_attempt")
        jobs = api.pages(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", key="jobs")
        if _names_and_steps(jobs, workflow, sha):
            return Admission(api.repository, workflow, branch, sha, run_id, attempt)
    return None
