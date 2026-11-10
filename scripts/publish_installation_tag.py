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
from scripts.full_ci_controller import ControllerError, GitHub
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.publish_release import PublishError, _version
from scripts.release_sqlite_summary import (
    InstallationSchemaEvidence, InstallationTag, build_summary, expected_migration_at, read_installation_tag,
)
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


def require_published_release(api: GitHub, receipt: InstallationReceipt) -> None:
    """Подтверждает, что опубликованный Release и его тег описывают принятый SHA.

    Args:
        api: Клиент GitHub ожидаемого репозитория.
        receipt: Только что прочитанная техническая приёмка.

    Raises:
        InstallationTagError: Репозиторий, ID Release, тег или SHA не совпали.
        DeploySelectionError: Release либо его манифест не подтверждён.
        ControllerError: GitHub не ответил на проверку.
    """
    release_id, run_id, run_attempt = _published_release(api, receipt.version, receipt.sha, receipt.kind)
    if release_id != receipt.release_id:
        raise InstallationTagError("Опубликованный Release не совпадает с приёмкой")
    if receipt.kind == "ordinary" and (run_id, run_attempt) != (receipt.run_id, receipt.run_attempt):
        raise InstallationTagError("Прогон опубликованного Release не совпадает с приёмкой")


def _published_release(api: GitHub, version: str, sha: str,
                       kind: str | None = None) -> tuple[int, int | None, int | None]:
    """Читает точный immutable Release и удостоверяет его тег и манифест."""
    _version(version)
    if api.repository != EXPECTED_REPOSITORY or not SHA_PATTERN.fullmatch(sha):
        raise InstallationTagError("Некорректный репозиторий или SHA тега установки")
    release = _release(api, version)
    if release["target_commitish"] != sha:
        raise InstallationTagError("Опубликованный Release относится к другому SHA")
    body = release["body"]
    if kind is None:
        kind = "emergency" if "Тип выпуска: аварийный" in body.splitlines() else "ordinary"
    tag_sha, _, annotation = _tag(api, version, allow_annotation=kind == "ordinary")
    run_id = run_attempt = None
    if kind == "ordinary":
        manifest_sha, _, run_id, run_attempt = _manifest(body, version, api.repository)
        if annotation is not None and (annotation.run_id, annotation.run_attempt) != (run_id, run_attempt):
            raise InstallationTagError("Прогон опубликованного Release не совпадает с аннотацией")
    else:
        workflow = MAIN_WORKFLOW if _version(version)[2] == 0 else RELEASE_WORKFLOW
        manifest_sha, *_ = _emergency_manifest(body, version, api.repository, workflow)
    if tag_sha != sha or manifest_sha != sha:
        raise InstallationTagError("SHA релизного тега и манифеста не совпадают с ожидаемым")
    return release["id"], run_id, run_attempt


def installation_tag_exists(api: GitHub, version: str, sha: str, *,
                            receipt: InstallationReceipt | None = None) -> bool:
    """Проверяет точный ref тега установки без записи в GitHub.

    Args:
        api: Клиент GitHub ожидаемого репозитория.
        version: Каноническая версия установленного выпуска.
        sha: Полный SHA принятых Release и VPS.
        receipt: Свежая приёмка для сверки SQLite аннотированного тега.

    Returns:
        True при точном совпадении, False при отсутствии тега.

    Raises:
        InstallationTagError: Ref, объект тега или данные приёмки расходятся.
        ControllerError: GitHub не ответил на проверку.
        PublishError: Версия не каноническая.
    """
    existing = _read_installation_tag(api, version, sha)
    if (existing is not None and existing.evidence is not None and receipt is not None
            and existing.evidence != _sqlite_evidence(receipt, version, sha)):
        raise InstallationTagError("Сведения SQLite существующего тега расходятся со свежей приёмкой")
    return existing is not None


def _read_installation_tag(api: GitHub, version: str, sha: str) -> InstallationTag | None:
    """Читает прямой ref или канонический объект тега для принятого коммита."""
    _version(version)
    if api.repository != EXPECTED_REPOSITORY or not SHA_PATTERN.fullmatch(sha):
        raise InstallationTagError("Некорректный репозиторий или SHA тега установки")
    name = f"tags/production/{version}"
    try:
        reference = api.get(f"/git/ref/{name}")
    except ControllerError as error:
        if isinstance(error.__cause__, HTTPError) and error.__cause__.code == 404:
            return None
        raise
    try:
        return read_installation_tag(reference, version, sha, lambda object_sha: api.get(f"/git/tags/{object_sha}"))
    except ValueError as error:
        raise InstallationTagError(f"Тег установки не подтверждён: {error}") from error


def _expected_migration(api: GitHub, sha: str) -> str:
    """Сохраняет сообщение о некорректном каталоге в контракте команды."""
    try:
        return expected_migration_at(api, sha)
    except ValueError as error:
        raise InstallationTagError(f"Не подтверждена ожидаемая миграция SQLite: {error}") from error


def summarize(api: GitHub, version: str, sha: str) -> dict[str, object]:
    """Собирает сводку SQLite из точного опубликованного коммита и тега установки.

    Args:
        api: Клиент GitHub ожидаемого репозитория с правом чтения.
        version: Каноническая версия опубликованного Release.
        sha: Полный SHA релизного коммита.

    Returns:
        Ожидаемая миграция и, при наличии аннотации, принятая SQLite.

    Raises:
        InstallationTagError: Источники расходятся или не подтверждены.
        DeploySelectionError: Release или его релизный тег не подтверждён.
        ControllerError: GitHub не ответил на проверку.
    """
    release_id, run_id, run_attempt = _published_release(api, version, sha)
    expected = _expected_migration(api, sha)
    installation = _read_installation_tag(api, version, sha)
    try:
        return build_summary(version, sha, release_id, run_id, run_attempt, expected, installation)
    except ValueError as error:
        raise InstallationTagError(f"Сводка SQLite не подтверждена: {error}") from error


def _sqlite_evidence(receipt: InstallationReceipt, version: str, sha: str) -> InstallationSchemaEvidence:
    """Выделяет точные реквизиты SQLite из записи приёмки."""
    try:
        return InstallationSchemaEvidence.from_receipt(asdict(receipt), version, sha)
    except ValueError as error:
        raise InstallationTagError(f"SQLite в приёмке не подтверждена: {error}") from error


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
    evidence = _sqlite_evidence(receipt, receipt.version, receipt.sha)
    if installation_tag_exists(api, receipt.version, receipt.sha, receipt=receipt):
        return receipt
    if evidence.migration != _expected_migration(api, receipt.sha):
        raise InstallationTagError("Миграция принятой SQLite не совпадает с коммитом выпуска")
    tag_name = f"production/{receipt.version}"
    ref_name = f"refs/tags/{tag_name}"
    tag = api.post("/git/tags", {"tag": tag_name, "message": evidence.message(),
                                  "object": receipt.sha, "type": "commit"})
    object_sha = tag.get("sha") if isinstance(tag, dict) else None
    try:
        created = read_installation_tag({"ref": ref_name, "object": {"type": "tag", "sha": object_sha}},
                                        receipt.version, receipt.sha, lambda _: tag)
    except ValueError as error:
        raise InstallationTagError(f"Не подтверждён созданный объект тега установки: {error}") from error
    if created.evidence != evidence:
        raise InstallationTagError("Созданный объект тега установки содержит другие сведения SQLite")
    try:
        api.post("/git/refs", {"ref": ref_name, "sha": created.object_sha})
    except PublishingError as error:
        # Запись могла пройти до потери ответа, либо такой же ref создал другой процесс.
        if installation_tag_exists(api, receipt.version, receipt.sha, receipt=receipt):
            return receipt
        raise InstallationTagError("Создание тега не подтверждено GitHub") from error
    if not installation_tag_exists(api, receipt.version, receipt.sha, receipt=receipt):
        raise InstallationTagError("После создания точный тег установки не подтверждён")
    return receipt


def main() -> None:
    """Проверяет приёмку, публикует тег владельца или выводит сводку SQLite."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="точный опубликованный релиз vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный ожидаемый SHA выпуска")
    parser.add_argument("--ssh-host", help="проверенный адрес user@host")
    parser.add_argument("--known-hosts", type=Path, help="файл с проверенным ключом VPS")
    parser.add_argument("--identity", type=Path, help="локальный путь к ключу SSH")
    parser.add_argument("--confirm-live", metavar="RUNTIME_ID",
                        help="подтвердить живые ответы Telegram и VK для этого runtime")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="прочитать приёмку без записи")
    mode.add_argument("--publish", action="store_true", help="создать и подтвердить тег установки")
    mode.add_argument("--summary", action="store_true", help="показать опубликованную сводку SQLite без SSH")
    args = parser.parse_args()
    if args.publish != (args.confirm_live is not None):
        parser.error("--confirm-live обязателен только при --publish")
    if not args.summary and (args.ssh_host is None or args.known_hosts is None):
        parser.error("--ssh-host и --known-hosts обязательны при --check и --publish")
    if args.summary and any(value is not None for value in (args.ssh_host, args.known_hosts, args.identity)):
        parser.error("--summary не использует настройки SSH")

    def read_fresh() -> InstallationReceipt:
        return read_receipt(args.version, args.sha, ssh_host=args.ssh_host,
                            known_hosts=args.known_hosts, identity=args.identity)

    try:
        if repository_from_origin() != EXPECTED_REPOSITORY:
            raise InstallationTagError("Origin не совпадает с репозиторием проекта")
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if args.summary:
            print(json.dumps(summarize(api, args.version, args.sha), ensure_ascii=False, indent=2))
        elif args.publish:
            receipt = publish(api, read_fresh, args.confirm_live)
            print(f"Тег установки подтверждён: production/{receipt.version} -> {receipt.sha}")
        else:
            receipt = read_fresh()
            require_published_release(api, receipt)
            installation_tag_exists(api, receipt.version, receipt.sha, receipt=receipt)
            print(json.dumps(asdict(receipt), ensure_ascii=False, separators=(",", ":")))
    except (ControllerError, DeploySelectionError, InstallationTagError, PublishError, PublishingError,
            ReleaseBranchError) as error:
        print(f"Тег установки: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
