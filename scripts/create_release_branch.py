"""Создаёт релизную ветку от проверенного полного SHA main."""

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
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError, PublishingGitHub


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


def prepare(api: ReadOnlyGitHub, branch: str, sha: str, *, publish_remote: bool = False) -> Admission:
    """Проверяет точный commit, успешный main-full и состояние ветки.

    Args:
        api: Клиент GitHub API с доступом на чтение.
        branch: Новое имя release/X.Y.
        sha: Полный SHA кандидата из main.
        publish_remote: Требовать уже созданный локальный ref того же SHA.

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
    local_ref = f"refs/heads/{branch}"
    local_exists = git("show-ref", "--verify", "--quiet", local_ref, allowed_codes=(0, 1)).returncode == 0
    if publish_remote:
        if not local_exists or git("rev-parse", "--verify", local_ref).stdout.strip() != sha:
            raise ReleaseBranchError("Локальная релизная ветка отсутствует или указывает на другой SHA")
    elif local_exists:
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


def publish_remote_branch(api: PublishingGitHub, branch: str, sha: str) -> Admission:
    """Атомарно создаёт удалённый ref и подтверждает его чтением GitHub API.

    Args:
        api: GitHub-клиент с правом записи Git refs.
        branch: Имя release/X.Y, ранее созданное локально.
        sha: Полный SHA успешного main-full.

    Returns:
        Повторно проверенный допуск полного CI.

    Raises:
        ReleaseBranchError: Ответ или read-back не подтверждает точный ref.
        PublishingError: GitHub отверг создание, включая конфликт имени.
    """
    admission = prepare(api, branch, sha, publish_remote=True)
    ref = f"refs/heads/{branch}"
    created = api.post("/git/refs", {"ref": ref, "sha": sha})
    if (not isinstance(created, dict) or created.get("ref") != ref
            or not isinstance(created.get("object"), dict)
            or created["object"].get("sha") != sha):
        raise ReleaseBranchError("Ответ создания ветки не подтверждает ref и SHA; проверьте GitHub вручную")
    read_back = api.get(f"/git/ref/heads/{branch}")
    if (not isinstance(read_back, dict) or read_back.get("ref") != ref
            or not isinstance(read_back.get("object"), dict)
            or read_back["object"].get("sha") != sha):
        raise ReleaseBranchError("GitHub не подтвердил созданную ветку при повторном чтении")
    return admission


def main() -> None:
    """Проверяет допуск и явно создаёт локальную либо удалённую ветку."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch", help="новая ветка release/X.Y")
    parser.add_argument("sha", help="полный SHA успешного main-full")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-only", action="store_true", help="только проверить допуск")
    modes.add_argument("--publish-remote", action="store_true", help="создать ветку на GitHub после локальной")
    arguments = parser.parse_args()
    try:
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if arguments.publish_remote:
            admission = publish_remote_branch(api, arguments.branch, arguments.sha)
            action = "Создана удалённая ветка"
        else:
            admission = prepare(api, arguments.branch, arguments.sha)
        if not arguments.check_only and not arguments.publish_remote:
            create_local_branch(arguments.branch, arguments.sha)
            action = "Создана локальная ветка"
        elif arguments.check_only:
            action = "Проверено"
        print(f"{action}: {arguments.branch} -> {admission.sha}; "
              f"main-full run={admission.run_id}, attempt={admission.run_attempt}")
    except (AdmissionError, ControllerError, PublishingError, ReleaseBranchError,
            KeyError, TypeError, ValueError) as error:
        print(f"Release branch: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
