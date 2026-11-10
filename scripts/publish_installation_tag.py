"""Подтверждает приёмку на VPS и создаёт тег установки точного выпуска."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError

from scripts.create_release_branch import EXPECTED_REPOSITORY, ReleaseBranchError, repository_from_origin
from scripts.deploy_prepare import PreparationError, parse_state_object
from scripts.full_ci_admission import MAIN_WORKFLOW, RELEASE_WORKFLOW, SHA_PATTERN
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.publish_release import PublishError, _version
from scripts.select_deploy_release import DeploySelectionError, _emergency_manifest, _manifest, _release, _tag


class InstallationTagError(RuntimeError):
    """Означает, что приёмку или тег установки нельзя подтвердить."""


@dataclass(frozen=True)
class InstallationReceipt:
    """Содержит проверенное состояние VPS в момент технической приёмки."""

    schema_version: int
    version: str
    sha: str
    kind: str
    release_id: int
    run_id: int
    run_attempt: int
    generation_id: str
    migration: str
    runtime_id: str


def _receipt(output: str, version: str, sha: str) -> InstallationReceipt:
    """Принимает одну однозначную JSON-строку штатной технической приёмки."""
    if len(output) > 4096 or len(output.splitlines()) != 1:
        raise InstallationTagError("VPS не вернул одну запись технической приёмки")
    try:
        receipt = InstallationReceipt(**parse_state_object(output))
    except (PreparationError, TypeError, ValueError) as error:
        raise InstallationTagError("Некорректная запись технической приёмки VPS") from error
    if (type(receipt.schema_version) is not int or receipt.schema_version != 1
            or receipt.version != version or receipt.sha != sha
            or receipt.kind not in ("ordinary", "emergency")
            or any(type(value) is not int or value <= 0
                   for value in (receipt.release_id, receipt.run_id, receipt.run_attempt))
            or not isinstance(receipt.generation_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", receipt.generation_id)
            or not isinstance(receipt.migration, str)
            or not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", receipt.migration)
            or not isinstance(receipt.runtime_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt.runtime_id)):
        raise InstallationTagError("VPS не подтвердил точную версию и принятый runtime SQLite v2")
    return receipt


def read_receipt(version: str, sha: str, *, ssh_host: str, known_hosts: Path,
                 identity: Path | None = None) -> InstallationReceipt:
    """Читает свежую техническую приёмку с VPS по проверенному SSH-ключу хоста.

    Args:
        version: Канонический тег ожидаемого выпуска.
        sha: Полный ожидаемый SHA установленного коммита.
        ssh_host: Адрес SSH в форме host или user@host.
        known_hosts: Локальный файл с ранее проверенным ключом VPS.
        identity: Необязательный локальный SSH private key для подключения.

    Returns:
        Приёмка точной версии и SHA с новым поколением SQLite.

    Raises:
        InstallationTagError: SSH либо техническая приёмка не подтверждены.
        PublishError: Версия не каноническая.
    """
    _version(version)
    if not SHA_PATTERN.fullmatch(sha) or not re.fullmatch(
        r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*", ssh_host,
    ):
        raise InstallationTagError("Некорректный SHA или адрес SSH")
    try:
        host_keys = known_hosts.resolve(strict=True)
        host_keys_path = host_keys.as_posix()
        if (not host_keys.is_file() or "${" in host_keys_path
                or any(char in host_keys_path for char in '"%\r\n')):
            raise InstallationTagError("Требуется файл с проверенным ключом VPS")
        command = [
            "ssh", "-F", "none", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
            "-o", "GlobalKnownHostsFile=none", "-o", f'UserKnownHostsFile="{host_keys_path}"',
            "-o", "ClearAllForwardings=yes", "-o", "UpdateHostKeys=no",
            "-o", "ForwardAgent=no", "-T",
        ]
        if identity is not None:
            command.extend(("-o", "IdentitiesOnly=yes", "-i", str(identity.resolve(strict=True))))
        command.extend(("--", ssh_host, "sudo", "-n", "deploy-prod", version, "--verify-installed", sha))
        local_tokens = {"GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
        result = subprocess.run(
            command, check=True, capture_output=True, encoding="utf-8", timeout=240,
            env={key: value for key, value in os.environ.items() if key.upper() not in local_tokens},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise InstallationTagError("Не удалось подтвердить техническую приёмку через SSH") from error
    return _receipt(result.stdout, version, sha)


def require_published_release(api: PublishingGitHub, receipt: InstallationReceipt) -> None:
    """Подтверждает, что опубликованный Release и его тег описывают принятый SHA.

    Args:
        api: Клиент GitHub ожидаемого репозитория.
        receipt: Только что прочитанная техническая приёмка.

    Raises:
        InstallationTagError: Репозиторий, ID Release, тег или SHA не совпали.
        DeploySelectionError: Release либо его манифест не подтверждён.
        ControllerError: GitHub не ответил на проверку.
    """
    if api.repository != EXPECTED_REPOSITORY:
        raise InstallationTagError("Репозиторий тега установки не совпадает с проектом")
    release = _release(api, receipt.version)
    if release["id"] != receipt.release_id or release["target_commitish"] != receipt.sha:
        raise InstallationTagError("Опубликованный Release не совпадает с приёмкой")
    tag_sha, _, annotation = _tag(api, receipt.version, allow_annotation=receipt.kind == "ordinary")
    if receipt.kind == "ordinary":
        manifest_sha, _, run_id, run_attempt = _manifest(release["body"], receipt.version, api.repository)
        if ((run_id, run_attempt) != (receipt.run_id, receipt.run_attempt)
                or (annotation is not None and (annotation.run_id, annotation.run_attempt) != (run_id, run_attempt))):
            raise InstallationTagError("Прогон опубликованного Release не совпадает с приёмкой")
    else:
        workflow = MAIN_WORKFLOW if _version(receipt.version)[2] == 0 else RELEASE_WORKFLOW
        manifest_sha, *_ = _emergency_manifest(release["body"], receipt.version, api.repository, workflow)
    if tag_sha != receipt.sha or manifest_sha != receipt.sha:
        raise InstallationTagError("SHA релизного тега, манифеста и приёмки не совпадают")


def installation_tag_exists(api: PublishingGitHub, version: str, sha: str) -> bool:
    """Проверяет точный прямой ref тега установки без записи в GitHub.

    Args:
        api: Клиент GitHub ожидаемого репозитория.
        version: Каноническая версия установленного выпуска.
        sha: Полный SHA принятых Release и VPS.

    Returns:
        True при точном совпадении, False при отсутствии тега.

    Raises:
        InstallationTagError: Ref существует на другом SHA или имеет иной тип.
        ControllerError: GitHub не ответил на проверку.
        PublishError: Версия не каноническая.
    """
    _version(version)
    if api.repository != EXPECTED_REPOSITORY or not SHA_PATTERN.fullmatch(sha):
        raise InstallationTagError("Некорректный репозиторий или SHA тега установки")
    name = f"tags/production/{version}"
    try:
        reference = api.get(f"/git/ref/{name}")
    except ControllerError as error:
        if isinstance(error.__cause__, HTTPError) and error.__cause__.code == 404:
            return False
        raise
    target = reference.get("object") if isinstance(reference, dict) else None
    if (not isinstance(target, dict) or reference.get("ref") != f"refs/{name}"
            or target.get("type") != "commit" or target.get("sha") != sha):
        raise InstallationTagError("Тег установки существует с неподтверждённым типом или SHA")
    return True


def publish(api: PublishingGitHub, read_fresh: Callable[[], InstallationReceipt],
            confirmed_runtime: str) -> InstallationReceipt:
    """Создаёт либо подтверждает тег установки после явного решения владельца.

    Args:
        api: Локальный GitHub-клиент с правом записи refs от имени владельца.
        read_fresh: Чтение свежей приёмки с доверенного VPS в этой команде.
        confirmed_runtime: Идентификатор runtime, для которого владелец
            подтвердил живые ответы обоих каналов.

    Returns:
        Запись приёмки для точного подтверждённого тега установки.

    Raises:
        InstallationTagError: Приёмка, владелец или запись не подтверждены.
        ControllerError: GitHub не ответил на проверку.
    """
    if api.repository != EXPECTED_REPOSITORY or not re.fullmatch(r"[0-9a-f]{64}", confirmed_runtime):
        raise InstallationTagError("Нужно подтвердить живые ответы для точного runtime VPS")
    actor = api.authenticated_user()
    if actor.get("id") != 42288695 or actor.get("login") != "karateka-git":
        raise InstallationTagError("Создание тега установки подтверждает владелец проекта")
    receipt = read_fresh()
    if receipt.runtime_id != confirmed_runtime:
        raise InstallationTagError("Runtime VPS изменился после проверки живых ответов")
    require_published_release(api, receipt)
    if installation_tag_exists(api, receipt.version, receipt.sha):
        return receipt
    try:
        api.post("/git/refs", {"ref": f"refs/tags/production/{receipt.version}", "sha": receipt.sha})
    except PublishingError as error:
        # Запись могла пройти до потери ответа, либо такой же ref создал другой процесс.
        if installation_tag_exists(api, receipt.version, receipt.sha):
            return receipt
        raise InstallationTagError("Создание тега не подтверждено GitHub") from error
    if not installation_tag_exists(api, receipt.version, receipt.sha):
        raise InstallationTagError("После создания точный тег установки не подтверждён")
    return receipt


def main() -> None:
    """Проверяет приёмку или публикует тег по явному решению владельца."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="точный опубликованный релиз vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный ожидаемый SHA выпуска")
    parser.add_argument("--ssh-host", required=True, help="проверенный адрес user@host")
    parser.add_argument("--known-hosts", required=True, type=Path, help="файл с проверенным ключом VPS")
    parser.add_argument("--identity", type=Path, help="локальный путь к ключу SSH")
    parser.add_argument("--confirm-live", metavar="RUNTIME_ID",
                        help="подтвердить живые ответы Telegram и VK для этого runtime")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="прочитать приёмку без записи")
    mode.add_argument("--publish", action="store_true", help="создать и подтвердить тег установки")
    args = parser.parse_args()
    if args.publish != (args.confirm_live is not None):
        parser.error("--confirm-live обязателен при --publish и недопустим при --check")

    def read_fresh() -> InstallationReceipt:
        return read_receipt(args.version, args.sha, ssh_host=args.ssh_host,
                            known_hosts=args.known_hosts, identity=args.identity)

    try:
        if repository_from_origin() != EXPECTED_REPOSITORY:
            raise InstallationTagError("Origin не совпадает с репозиторием проекта")
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if args.publish:
            receipt = publish(api, read_fresh, args.confirm_live)
            print(f"Тег установки подтверждён: production/{receipt.version} -> {receipt.sha}")
        else:
            receipt = read_fresh()
            require_published_release(api, receipt)
            installation_tag_exists(api, receipt.version, receipt.sha)
            print(json.dumps(asdict(receipt), ensure_ascii=False, separators=(",", ":")))
    except (ControllerError, DeploySelectionError, InstallationTagError, PublishError, PublishingError,
            ReleaseBranchError) as error:
        print(f"Тег установки: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
