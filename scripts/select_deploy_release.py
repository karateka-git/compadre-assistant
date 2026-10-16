"""Проверяет опубликованный обычный выпуск перед production-развёртыванием."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
import re
import sys
from urllib.parse import quote

from scripts.create_release_branch import EXPECTED_REPOSITORY, ReleaseBranchError, repository_from_origin
from scripts.full_ci_admission import (
    AdmissionError, MAIN_WORKFLOW, RELEASE_WORKFLOW, ReadOnlyGitHub, SHA_PATTERN,
    select_successful_run,
)
from scripts.full_ci_controller import ControllerError, GitHub
from scripts.publish_release import PublishError, _tag_protection, _version


class DeploySelectionError(RuntimeError):
    """Означает, что обычный выпуск нельзя допустить к развёртыванию."""


@dataclass(frozen=True)
class SelectedRelease:
    """Фиксирует точную версию, commit, тип выпуска и наблюдаемый полный CI."""

    repository: str
    version: str
    branch: str
    sha: str
    release_id: int
    workflow: str
    run_id: int
    run_attempt: int
    kind: str = "ordinary"
    reason: str | None = None
    ci_state: str = "success"
    ci_source: str | None = None
    published_ci_state: str | None = None
    published_ci_source: str | None = None


def _sha(value: object, label: str) -> str:
    """Проверяет полный commit SHA из обязательного поля API."""
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise DeploySelectionError(f"Некорректный полный SHA: {label}")
    return value


def _positive(value: object, label: str) -> int:
    """Проверяет положительный числовой ID или номер попытки."""
    if type(value) is not int or value <= 0:
        raise DeploySelectionError(f"Некорректное число: {label}")
    return value


def _release(api: ReadOnlyGitHub, version: str) -> dict:
    """Получает ровно названный опубликованный immutable GitHub Release."""
    value = api.get(f"/releases/tags/{quote(version, safe='')}")
    if not isinstance(value, dict):
        raise DeploySelectionError("Некорректный объект GitHub Release")
    if (value.get("tag_name") != version or value.get("name") != version or
            value.get("draft") is not False or value.get("prerelease") is not False or
            value.get("immutable") is not True or
            not isinstance(value.get("published_at"), str) or not value["published_at"]):
        raise DeploySelectionError("Указанная версия не является опубликованным immutable Release")
    _positive(value.get("id"), "Release ID")
    _sha(value.get("target_commitish"), "Release target_commitish")
    if not isinstance(value.get("body"), str):
        raise DeploySelectionError("Отсутствует манифест Release")
    return value


def _tag_commit(api: ReadOnlyGitHub, version: str) -> str:
    """Требует прямой commit tag без промежуточного annotated tag."""
    value = api.get(f"/git/ref/tags/{quote(version, safe='')}")
    if (not isinstance(value, dict) or value.get("ref") != f"refs/tags/{version}" or
            not isinstance(value.get("object"), dict) or value["object"].get("type") != "commit"):
        raise DeploySelectionError("Тег отсутствует или не указывает напрямую на commit")
    return _sha(value["object"].get("sha"), "tag")


def _manifest(body: str, version: str, repository: str) -> tuple[str, str, int, int]:
    """Читает единственный служебный блок обычного выпуска."""
    fields: dict[str, str] = {}
    labels = ("Тип выпуска", "Commit", "Ветка", "Полный CI")
    for line in body.splitlines():
        for label in labels:
            if line.startswith(f"{label}:"):
                if label in fields:
                    raise DeploySelectionError(f"Повторяется поле Release: {label}")
                fields[label] = line.removeprefix(f"{label}:").strip()
    if set(fields) != set(labels) or fields["Тип выпуска"] != "обычный":
        raise DeploySelectionError("Release не содержит манифест обычного выпуска")
    sha = fields["Commit"]
    if not re.fullmatch(r"`[0-9a-f]{40}`", sha):
        raise DeploySelectionError("Манифест содержит неверный commit SHA")
    branch = fields["Ветка"]
    major, minor, _ = _version(version)
    if branch != f"`release/{major}.{minor}`":
        raise DeploySelectionError("Ветка в Release не соответствует версии")
    url = fields["Полный CI"]
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)",
        url,
    )
    if not match:
        raise DeploySelectionError("Release не закрепляет точный GitHub CI run/attempt")
    return sha[1:-1], branch[1:-1], int(match[1]), int(match[2])


def _branch_head(api: ReadOnlyGitHub, branch: str) -> str:
    """Читает полный SHA текущего ref релизной ветки."""
    value = api.get(f"/git/ref/heads/{branch}")
    if (not isinstance(value, dict) or value.get("ref") != f"refs/heads/{branch}" or
            not isinstance(value.get("object"), dict) or value["object"].get("type") != "commit"):
        raise DeploySelectionError("Некорректный ref релизной ветки")
    return _sha(value["object"].get("sha"), branch)


def _check_ancestor(api: ReadOnlyGitHub, sha: str, head: str) -> None:
    """Доказывает достижимость выпуска из текущей релизной ветки."""
    value = api.get(f"/compare/{sha}...{head}")
    if (not isinstance(value, dict) or value.get("status") not in ("ahead", "identical") or
            not isinstance(value.get("base_commit"), dict) or
            value["base_commit"].get("sha") != sha or
            not isinstance(value.get("head_commit"), dict) or
            value["head_commit"].get("sha") != head):
        raise DeploySelectionError("Commit не доказан предком релизной ветки")


def select_release(api: ReadOnlyGitHub, version: str) -> SelectedRelease:
    """Выбирает точный опубликованный выпуск с защитой и успешным полным CI.

    Args:
        api: Читающий клиент GitHub для публичного репозитория проекта.
        version: Явная каноническая версия vMAJOR.MINOR.PATCH.

    Returns:
        Манифест для следующей стадии развёртывания.

    Raises:
        DeploySelectionError: Выпуск или его происхождение не подтверждены.
        AdmissionError: Результат полного CI не может быть проверен.
        ControllerError: GitHub API недоступен или вернул неверный список.
    """
    if api.repository != EXPECTED_REPOSITORY or repository_from_origin() != EXPECTED_REPOSITORY:
        raise DeploySelectionError("Репозиторий API или origin не совпадает с проектом")
    major, minor, patch = _version(version)
    branch = f"release/{major}.{minor}"
    release = _release(api, version)
    tag_sha = _tag_commit(api, version)
    manifest_sha, manifest_branch, run_id, attempt = _manifest(release["body"], version, api.repository)
    if (manifest_branch != branch or release["target_commitish"] != tag_sha or
            manifest_sha != tag_sha):
        raise DeploySelectionError("SHA тега, Release и манифеста не совпадают")
    head = _branch_head(api, branch)
    _check_ancestor(api, tag_sha, head)
    # immutable конкретного Release виден без admin token; settings endpoint
    # /immutable-releases требует Administration(read) и на VPS недоступен.
    _tag_protection(api, version, require_visible_bypass=False)
    workflow = MAIN_WORKFLOW if patch == 0 else RELEASE_WORKFLOW
    ci_branch = "main" if patch == 0 else branch
    admission = select_successful_run(
        api, api.repository, workflow, tag_sha, ci_branch, run_id, attempt,
    )
    if admission is None:
        raise DeploySelectionError("Закреплённый полный CI не подтвердил точный SHA")
    # Изменение Release, тега или ветки во время чтения делает снимок непригодным.
    if _release(api, version) != release or _tag_commit(api, version) != tag_sha or _branch_head(api, branch) != head:
        raise DeploySelectionError("GitHub Release, tag или ветка изменились во время проверки")
    return SelectedRelease(api.repository, version, branch, tag_sha, release["id"],
                           workflow, admission.run_id, admission.run_attempt)


def _emergency_manifest(body: str, version: str, repository: str,
                        workflow: str) -> tuple[str, str, str, str, str]:
    """Разбирает обязательные поля неизменяемого аварийного манифеста.

    Args:
        body: Опубликованный текст Release.
        version: Каноническая версия.
        repository: Ожидаемый GitHub-репозиторий.
        workflow: Полный CI для типа версии.

    Returns:
        SHA, ветку, причину и опубликованные состояние и источник CI.

    Raises:
        DeploySelectionError: Служебные поля отсутствуют или неоднозначны.
    """
    labels = ("Тип выпуска", "Commit", "Ветка", "Причина", "Полный CI")
    fields: dict[str, str] = {}
    for line in body.splitlines():
        for label in labels:
            if line.startswith(f"{label}:"):
                if label in fields:
                    raise DeploySelectionError(f"Повторяется поле Release: {label}")
                fields[label] = line.removeprefix(f"{label}:").strip()
    if set(fields) != set(labels) or fields["Тип выпуска"] != "аварийный":
        raise DeploySelectionError("Release не содержит аварийный манифест")
    sha_field = fields["Commit"]
    if not re.fullmatch(r"`[0-9a-f]{40}`", sha_field):
        raise DeploySelectionError("Манифест содержит неверный commit SHA")
    major, minor, _ = _version(version)
    branch = f"release/{major}.{minor}"
    if fields["Ветка"] != f"`{branch}`":
        raise DeploySelectionError("Ветка в аварийном Release не соответствует версии")
    reason = fields["Причина"]
    if not reason or len(reason) > 1000 or any(c in reason for c in "\r\n\x00"):
        raise DeploySelectionError("Причина аварийного выпуска отсутствует или некорректна")
    ci = re.fullmatch(r"(main-full\.yml|release-full\.yml) "
                      r"(success|failure|pending|not_run|unverified) — (.+)",
                      fields["Полный CI"])
    if not ci or ci[1] != workflow:
        raise DeploySelectionError("Манифест содержит неверный полный CI")
    source = ci[3]
    expected_url = rf"https://github\.com/{re.escape(repository)}/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*"
    static_states = {
        "GitHub Actions: полный run не идентифицирован": "unverified",
        "GitHub Actions: подходящий full run не найден": "not_run",
        "GitHub Actions: метаданные full run неполны": "unverified",
    }
    if (not re.fullmatch(expected_url, source) and
            static_states.get(source) != ci[2]):
        raise DeploySelectionError("Источник состояния полного CI некорректен")
    if re.fullmatch(expected_url, source) and ci[2] == "not_run":
        raise DeploySelectionError("Отсутствующий CI не может ссылаться на конкретный run")
    pr_rows = re.findall(r"(?m)^- PR #([1-9][0-9]*) -> (main|release/[1-9][0-9]*\.[0-9]+); "
                         r"merge `([0-9a-f]{40})` \(([^()]+)\); "
                         r"pr-fast=(success|failure|pending|not_run|unverified) "
                         r"\[([^\]\r\n]+)\]; bypass=(yes|no|unverified)"
                         r"(?:; rule suite [1-9][0-9]*)?$", body)
    if ("PR и состояние на момент merge:" not in body or not pr_rows or
            len({row[0] for row in pr_rows}) != len(pr_rows) or
            any(row[1] not in ("main", branch) for row in pr_rows)):
        raise DeploySelectionError("Аварийный манифест не содержит проверяемый список PR")
    if ci[2] != "success" and "Неподтверждённые или неуспешные проверки:" not in body:
        raise DeploySelectionError("Манифест не перечисляет непрошедший полный CI")
    if ("Неподтверждённые или неуспешные проверки:" not in body and
            "Все наблюдаемые проверки успешны; аварийный допуск всё равно явный." not in body):
        raise DeploySelectionError("Аварийный манифест не содержит итог проверки")
    return sha_field[1:-1], branch, reason, ci[2], source


def select_emergency_release(api: ReadOnlyGitHub, version: str) -> SelectedRelease:
    """Выбирает только опубликованный аварийный Release точного SHA.

    Полный CI читается заново и показывается как факт, но его успех не требуется.

    Args:
        api: Читающий клиент GitHub выбранного репозитория.
        version: Явная каноническая версия выпуска.

    Returns:
        Проверенный снимок для аварийного развёртывания.

    Raises:
        DeploySelectionError: Происхождение или манифест не подтверждены.
    """
    if api.repository != EXPECTED_REPOSITORY or repository_from_origin() != EXPECTED_REPOSITORY:
        raise DeploySelectionError("Репозиторий API или origin не совпадает с проектом")
    major, minor, patch = _version(version)
    branch = f"release/{major}.{minor}"
    workflow = MAIN_WORKFLOW if patch == 0 else RELEASE_WORKFLOW
    ci_branch = "main" if patch == 0 else branch
    release = _release(api, version)
    tag_sha = _tag_commit(api, version)
    manifest_sha, manifest_branch, reason, published_state, published_source = _emergency_manifest(
        release["body"], version, api.repository, workflow,
    )
    if (manifest_sha != tag_sha or manifest_branch != branch or
            release["target_commitish"] != tag_sha):
        raise DeploySelectionError("SHA тега, Release и аварийного манифеста не совпадают")
    head = _branch_head(api, branch)
    _check_ancestor(api, tag_sha, head)
    _tag_protection(api, version, require_visible_bypass=False)
    # Чтение состояния CI обязательно: ошибка API не считается отсутствием run.
    from scripts.publish_emergency_release import _full_snapshot
    current = _full_snapshot(api, workflow, tag_sha, ci_branch)
    if _release(api, version) != release or _tag_commit(api, version) != tag_sha or _branch_head(api, branch) != head:
        raise DeploySelectionError("GitHub Release, tag или ветка изменились во время проверки")
    run = re.fullmatch(
        rf"https://github\.com/{re.escape(api.repository)}/actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)",
        current.source,
    )
    return SelectedRelease(api.repository, version, branch, tag_sha, release["id"], workflow,
                           int(run[1]) if run else 0, int(run[2]) if run else 0,
                           "emergency", reason, current.state, current.source,
                           published_state, published_source)


def main() -> None:
    """Печатает проверенный JSON-манифест без остановки ботов и записей на VPS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="точная опубликованная версия vMAJOR.MINOR.PATCH")
    args = parser.parse_args()
    try:
        api = GitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""), allow_anonymous=True)
        selected = select_release(api, args.version)
        print(json.dumps(asdict(selected), ensure_ascii=False, sort_keys=True))
    except (AdmissionError, ControllerError, DeploySelectionError, PublishError,
            ReleaseBranchError, KeyError, TypeError, ValueError) as error:
        print(f"Deploy selector: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
