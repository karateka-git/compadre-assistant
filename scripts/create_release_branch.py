"""Создаёт локальную релизную ветку от проверенного полного SHA main."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from scripts.full_ci_admission import (
    Admission, AdmissionError, MAIN_WORKFLOW, RELEASE_BRANCH_PATTERN,
    SHA_PATTERN, ReadOnlyGitHub, select_successful_run,
)
from scripts.full_ci_controller import ControllerError, GitHub


EXPECTED_REPOSITORY = "karateka-git/compadre-assistant"
ZERO_SHA = "0" * 40


class ReleaseBranchError(RuntimeError):
    """Означает, что безопасное создание релизной ветки невозможно."""


def git(*arguments: str, allowed_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    """Выполняет Git в текущем checkout и проверяет код завершения.

    Args:
        arguments: Аргументы Git без имени исполняемого файла.
        allowed_codes: Допустимые коды завершения команды.

    Returns:
        Завершённый процесс Git.

    Raises:
        ReleaseBranchError: Git завершился с неожиданным кодом.
    """
    result = subprocess.run(["git", *arguments], capture_output=True, text=True)
    if result.returncode not in allowed_codes:
        raise ReleaseBranchError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result


def repository_from_origin() -> str:
    """Определяет GitHub-репозиторий checkout по его origin.

    Returns:
        Полное имя репозитория owner/repo.

    Raises:
        ReleaseBranchError: Origin отсутствует или указывает на иной хост.
    """
    url = git("remote", "get-url", "origin").stdout.strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
        url,
    )
    if not match:
        raise ReleaseBranchError("origin должен указывать на GitHub-репозиторий проекта")
    return match.group(1)


def _sha_field(value: object, description: str) -> str:
    """Читает полный SHA из обязательного поля ответа API."""
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise ReleaseBranchError(f"Некорректный SHA GitHub API: {description}")
    return value


def checked_main_head(api: ReadOnlyGitHub, sha: str) -> str:
    """Подтверждает, что candidate достижим из текущего main на GitHub.

    Args:
        api: Клиент GitHub API с доступом на чтение.
        sha: Полный SHA кандидата.

    Returns:
        Точный SHA наблюдавшегося tip main.

    Raises:
        ReleaseBranchError: Ответ API не доказывает принадлежность истории main.
    """
    reference = api.get("/git/ref/heads/main")
    if not isinstance(reference, dict) or not isinstance(reference.get("object"), dict):
        raise ReleaseBranchError("Некорректный ref main в GitHub API")
    head = _sha_field(reference["object"].get("sha"), "main")
    comparison = api.get(f"/compare/{sha}...{head}")
    if (not isinstance(comparison, dict) or
            comparison.get("status") not in ("ahead", "identical") or
            not isinstance(comparison.get("base_commit"), dict) or
            comparison["base_commit"].get("sha") != sha):
        raise ReleaseBranchError("SHA не доказан предком текущего main")
    return head


def _ensure_remote_branch_absent(api: ReadOnlyGitHub, branch: str) -> None:
    """Отвергает уже существующую или некорректно описанную ветку GitHub."""
    remote_branches = api.pages("/branches")
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
           for item in remote_branches):
        raise ReleaseBranchError("Некорректный список веток GitHub API")
    if any(item["name"] == branch for item in remote_branches):
        raise ReleaseBranchError("Удалённая релизная ветка уже существует")


def prepare(api: ReadOnlyGitHub, branch: str, sha: str) -> Admission:
    """Проверяет точный commit, успешный main-full и свободное имя ветки.

    Args:
        api: Клиент GitHub API с доступом на чтение.
        branch: Новое имя release/X.Y.
        sha: Полный SHA кандидата из main.

    Returns:
        Проверенная попытка полного CI.

    Raises:
        ReleaseBranchError: Идентичность, история или имя ветки не подтверждены.
        AdmissionError: Ответ CI неоднозначен или повреждён.
    """
    if not RELEASE_BRANCH_PATTERN.fullmatch(branch):
        raise ReleaseBranchError("Требуется имя release/X.Y")
    if not SHA_PATTERN.fullmatch(sha):
        raise ReleaseBranchError("Требуется полный 40-символьный SHA")
    if api.repository != EXPECTED_REPOSITORY or repository_from_origin() != EXPECTED_REPOSITORY:
        raise ReleaseBranchError("Репозиторий API и origin должны совпадать с проектом")
    if git("cat-file", "-t", sha).stdout.strip() != "commit":
        raise ReleaseBranchError("SHA отсутствует как commit в локальном Git; сначала выполните fetch")
    if git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", allowed_codes=(0, 1)).returncode == 0:
        raise ReleaseBranchError("Локальная релизная ветка уже существует")
    _ensure_remote_branch_absent(api, branch)
    checked_main_head(api, sha)
    admission = select_successful_run(api, EXPECTED_REPOSITORY, MAIN_WORKFLOW, sha, "main")
    if admission is None:
        raise ReleaseBranchError("Нет успешного полного main-full для точного SHA")
    # Повторное чтение обнаруживает движение или переписывание main во время проверки CI.
    checked_main_head(api, sha)
    _ensure_remote_branch_absent(api, branch)
    return admission


def create_local_branch(branch: str, sha: str) -> None:
    """Атомарно создаёт отсутствующий локальный ref без переключения checkout.

    Args:
        branch: Проверенное имя release/X.Y.
        sha: Проверенный полный SHA.

    Raises:
        ReleaseBranchError: Ref уже возник или Git не смог его создать.
    """
    git("update-ref", f"refs/heads/{branch}", sha, ZERO_SHA)


def main() -> None:
    """Проверяет допуск и по запросу создаёт только локальную ветку."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch", help="новая ветка release/X.Y")
    parser.add_argument("sha", help="полный SHA успешного main-full")
    parser.add_argument("--check-only", action="store_true", help="только проверить допуск")
    arguments = parser.parse_args()
    try:
        api = GitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        admission = prepare(api, arguments.branch, arguments.sha)
        if not arguments.check_only:
            create_local_branch(arguments.branch, arguments.sha)
        action = "Проверено" if arguments.check_only else "Создана локальная ветка"
        print(f"{action}: {arguments.branch} -> {admission.sha}; "
              f"main-full run={admission.run_id}, attempt={admission.run_attempt}")
    except (AdmissionError, ControllerError, ReleaseBranchError, KeyError, TypeError, ValueError) as error:
        print(f"Release branch: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
