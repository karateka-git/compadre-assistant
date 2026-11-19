"""Выбирает запуск полного CI по истории ветки и данным GitHub API."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import StrEnum
import json
import os
import re
import subprocess
import sys
from time import perf_counter_ns
from urllib.parse import quote, urlencode
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

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


class HttpLocation(StrEnum):
    """Обозначает процесс, отправивший запрос."""

    RUNNER = "runner"
    VPS = "vps"


class HttpPhase(StrEnum):
    """Ограничивает набор меток этапов выпуска."""

    OTHER = "other"  # Запрос вне явно обозначенного этапа.
    AUTHOR = "author"  # Установление доверенного автора записи.
    RELEASE_LOOKUP = "release_lookup"  # Проверка наличия опубликованного Release.
    RELEASE_PREPARE = "release_prepare"  # Подготовка кандидата к публикации.
    RELEASE_PUBLISH = "release_publish"  # Повторный допуск и запись тега и Release.
    RELEASE_SELECT = "release_select"  # Сверка опубликованного Release с тегом и CI.
    DEPLOYMENT_BASE = "deployment_base"  # Запросы для выбора или подтверждения принятой базы.
    DEPLOYMENT_PUBLISH = "deployment_publish"  # Запись и подтверждение результата выкладки.
    VPS_PREPARE = "vps_prepare"  # Подготовка установки на VPS.
    VPS_RECHECK = "vps_recheck"  # Повторная сверка допуска перед переключением.
    VPS_ACCEPT = "vps_accept"  # Техническая сверка установленного выпуска.
    VPS_CLEANUP = "vps_cleanup"  # Сверка выпуска для очистки прежней SQLite.


class _Target(StrEnum):
    GITHUB = "github_api"
    GRAPHQL_QUERY = "github_graphql_query"  # Чтение GitHub с HTTP-методом POST.
    STORAGE = "artifact_storage"  # Запрос после redirect от API скачивания ZIP.


@dataclass
class _Totals:
    attempts: int = 0
    pages: int = 0
    errors: int = 0
    elapsed_ns: int = 0


class HttpMetrics:
    """Накапливает счётчики по месту, этапу, получателю и методу HTTP."""

    def __init__(self, location: HttpLocation) -> None:
        """Создаёт пустой набор счётчиков для указанного места выполнения."""
        self._location = HttpLocation(location)
        self._totals: dict[tuple[HttpPhase, _Target, str], _Totals] = {}

    def rows(self) -> list[dict[str, str | int]]:
        """Возвращает отсортированный снимок агрегатов для JSON-отчёта."""
        return [
            {"location": self._location.value, "phase": phase.value, "target": target.value,
             "method": method, "attempts": total.attempts, "pages": total.pages,
             "errors": total.errors, "elapsed_ns": total.elapsed_ns}
            for (phase, target, method), total in sorted(self._totals.items())
        ]


_active_metrics: ContextVar[HttpMetrics | None] = ContextVar("github_http_metrics", default=None)
_active_phase: ContextVar[HttpPhase] = ContextVar("github_http_phase", default=HttpPhase.OTHER)
_immutable_reads: ContextVar[dict[tuple[str, str], object] | None] = ContextVar("immutable_git_reads", default=None)


@contextmanager
def immutable_git_reads(*, enabled: bool = True) -> Iterator[None]:
    """Переиспользует ответы по точному SHA внутри одной команды выпуска.

    Args:
        enabled: Разрешить кэш неизменяемого Git-содержимого для этой команды.

    Yields:
        Управление без общего кэша между независимыми командами.
    """
    token = _immutable_reads.set({} if enabled else None)
    try:
        yield
    finally:
        _immutable_reads.reset(token)


@contextmanager
def collect_http_metrics(location: HttpLocation) -> Iterator[HttpMetrics]:
    """Включает сбор в текущем контексте и возвращает агрегат этой операции."""
    metrics = HttpMetrics(location)
    token = _active_metrics.set(metrics)
    phase_token = _active_phase.set(HttpPhase.OTHER)
    try:
        yield metrics
    finally:
        _active_phase.reset(phase_token)
        _active_metrics.reset(token)


@contextmanager
def http_phase(phase: HttpPhase) -> Iterator[None]:
    """Назначает HTTP-метку для блока и восстанавливает прежнюю при выходе."""
    token = _active_phase.set(HttpPhase(phase))
    try:
        yield
    finally:
        _active_phase.reset(token)


class _Attempt:
    def __init__(self, totals: _Totals, page: bool) -> None:
        self._totals = totals
        self._failed = False
        totals.attempts += 1
        totals.pages += page

    def fail(self) -> None:
        """Учитывает отказ один раз, даже если ошибку видят несколько слоёв."""
        if not self._failed:
            self._failed = True
            self._totals.errors += 1

    def measure(self, operation, *args, **kwargs):
        """Прибавляет время сетевой операции к начатому запросу."""
        started = perf_counter_ns()
        try:
            return operation(*args, **kwargs)
        except Exception:
            self.fail()
            raise
        finally:
            self._totals.elapsed_ns += perf_counter_ns() - started


class _MeasuredResponse:
    def __init__(self, response, attempt: _Attempt) -> None:
        self._response = response
        self._attempt = attempt

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        self._response.__enter__()
        return self

    def __exit__(self, *args):
        return self._response.__exit__(*args)

    def read(self, *args, **kwargs):
        """Измеряет чтение тела поверх времени соединения и заголовков."""
        return self._attempt.measure(self._response.read, *args, **kwargs)


class _MeasuredHandler(HTTPHandler, HTTPSHandler):
    def __init__(self, metrics: HttpMetrics, original: Request, *,
                 target: _Target, artifact: bool, page: bool) -> None:
        super().__init__()
        self._metrics = metrics
        self._original = original
        self._target = target
        self._artifact = artifact
        self._page = page
        self.last_attempt: _Attempt | None = None

    def _open(self, request: Request, operation):
        original = request is self._original
        target = _Target.STORAGE if self._artifact and not original else self._target
        method = request.get_method()
        key = (_active_phase.get(), target, method if method in {"GET", "POST"} else "OTHER")
        totals = self._metrics._totals.setdefault(key, _Totals())
        attempt = self.last_attempt = _Attempt(totals, self._page and original)
        response = attempt.measure(operation, request)
        if response.code >= 400:
            attempt.fail()
        return _MeasuredResponse(response, attempt)

    def http_open(self, request: Request):
        """Измеряет одну отправку HTTP через стандартный handler."""
        return self._open(request, super().http_open)

    def https_open(self, request: Request):
        """Измеряет одну отправку HTTPS через стандартный handler."""
        return self._open(request, super().https_open)


def open_http(request: Request, *, timeout: int, fallback=urlopen,
              artifact_redirect: HTTPRedirectHandler | None = None, page: bool = False,
              graphql_query: bool = False):
    """Открывает запрос и при активном замере считает каждый HTTP-переход.

    Args:
        request: Запрос GitHub API, данные которого не сохраняются в метриках.
        timeout: Сетевой таймаут в секундах.
        fallback: Обычный opener клиента для вызовов без замера.
        artifact_redirect: Политика редиректов ZIP; последующие запросы
            учитываются как обращения к хранилищу.
        page: Первый запрос получает страницу списка с явным номером.
        graphql_query: Операция чтения GraphQL через HTTP POST.

    Returns:
        HTTP-ответ, поддерживающий контекстный менеджер и чтение тела.

    Raises:
        OSError: Сбой обращения или HTTP-ошибка из opener.
    """
    metrics = _active_metrics.get()
    handlers = [] if artifact_redirect is None else [artifact_redirect]
    if metrics is None:
        opener = build_opener(*handlers).open if handlers else fallback
        return opener(request, timeout=timeout)

    # urllib вызывает этот handler заново после каждого HTTP redirect.
    target = _Target.GRAPHQL_QUERY if graphql_query else _Target.GITHUB
    measured = _MeasuredHandler(metrics, request, target=target, artifact=artifact_redirect is not None, page=page)
    try:
        return build_opener(measured, *handlers).open(request, timeout=timeout)
    except Exception:
        if measured.last_attempt is not None:
            measured.last_attempt.fail()
        raise


class GitHub:
    """Читает данные репозитория через REST и GraphQL GitHub."""

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
        """Получает JSON через REST API репозитория.

        Args:
            path: Путь REST API относительно репозитория.
            **parameters: Параметры запроса, включая номер страницы списка.

        Returns:
            Десериализованный ответ GitHub.

        Raises:
            ControllerError: Запрос или чтение JSON завершились ошибкой.
        """
        url = self.root + path
        if parameters:
            url += "?" + urlencode(parameters)
        cache = _immutable_reads.get()
        immutable = (not parameters and re.fullmatch(r"/git/tags/[0-9a-f]{40}", path)
                     or path.startswith("/contents/") and parameters.keys() == {"ref"}
                     and SHA_PATTERN.fullmatch(str(parameters["ref"])))
        if cache is None or not immutable:
            return self._json(url, path, page="page" in parameters)
        key = self.token, url
        if key not in cache:
            cache[key] = self._json(url, path)
        return deepcopy(cache[key])

    def graphql(self, query: str, **variables: str) -> dict:
        """Выполняет запрос чтения и отклоняет частичный ответ GraphQL.

        Args:
            query: Документ GraphQL с операцией чтения.
            **variables: Значения переменных операции.

        Returns:
            Объект data полного ответа.

        Raises:
            ControllerError: GitHub не подтвердил выполнение запроса целиком.
        """
        body = json.dumps({"query": query, "variables": variables}).encode()
        result = self._json("https://api.github.com/graphql", "graphql", body=body, graphql_query=True)
        if not isinstance(result, dict) or "errors" in result or not isinstance(result.get("data"), dict):
            raise ControllerError("Получен неполный ответ GitHub GraphQL")
        return result["data"]

    def _json(self, url: str, path: str, *, page: bool = False, body: bytes | None = None,
              graphql_query: bool = False) -> object:
        """Отправляет измеряемый запрос и читает JSON."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "compadre-full-ci-controller",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, headers=headers, data=body)
        try:
            with open_http(request, timeout=20, fallback=urlopen, page=page, graphql_query=graphql_query) as response:
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
