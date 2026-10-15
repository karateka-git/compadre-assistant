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
    """Фиксирует точную версию, commit и успешную попытку полного CI."""

    repository: str
    version: str
    branch: str
    sha: str
    release_id: int
    workflow: str
    run_id: int
    run_attempt: int


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
    """Получает ровно названный опубликованный обычный GitHub Release."""
    value = api.get(f"/releases/tags/{quote(version, safe='')}")
    if not isinstance(value, dict):
        raise DeploySelectionError("Некорректный объект GitHub Release")
    if (value.get("tag_name") != version or value.get("name") != version or
            value.get("draft") is not False or value.get("prerelease") is not False or
            value.get("immutable") is not True or
            not isinstance(value.get("published_at"), str) or not value["published_at"]):
        raise DeploySelectionError("Указанная версия не является опубликованным обычным Release")
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
