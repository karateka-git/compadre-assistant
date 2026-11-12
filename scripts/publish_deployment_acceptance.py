"""Публикует подтверждённую приёмку выпуска в GitHub Deployments."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from scripts.create_release_branch import EXPECTED_REPOSITORY, ReleaseBranchError, repository_from_origin
from scripts.deployment_acceptance import (
    DeploymentAcceptanceError, DeploymentRecord, DeploymentState, ENVIRONMENT, LEGACY_INSTALLATION_VERSION,
    OWNER_ID, OWNER_LOGIN, TASK, _expected_payload, read_deployment_record,
)
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.publish_installation_tag import (
    InstallationReceipt, InstallationTagError, _published_release, read_receipt, run_vps_command,
)
from scripts.publish_release import PublishError
from scripts.select_deploy_release import DeploySelectionError


class DeploymentPublishError(RuntimeError):
    """Означает, что запись приёмки нельзя безопасно создать или завершить."""


_OPERATION_ERRORS = (
    ControllerError, DeploymentAcceptanceError, DeploymentPublishError, DeploySelectionError,
    InstallationTagError, PublishError, PublishingError, ReleaseBranchError,
)


def _trusted_ci_url(api: PublishingGitHub, receipt: InstallationReceipt) -> str:
    """Проверяет точную успешную попытку CI для ссылки статуса."""
    run = api.get(f"/actions/runs/{receipt.run_id}/attempts/{receipt.run_attempt}")
    expected_url = (f"https://github.com/{EXPECTED_REPOSITORY}/actions/runs/"
                    f"{receipt.run_id}/attempts/{receipt.run_attempt}")
    if (not isinstance(run, dict) or type(run.get("id")) is not int
            or type(run.get("run_attempt")) is not int
            or (run["id"], run["run_attempt"]) != (receipt.run_id, receipt.run_attempt)
            or run.get("head_sha") != receipt.sha or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("html_url") not in (expected_url, expected_url.rsplit("/attempts/", 1)[0])):
        raise DeploymentPublishError("Не подтверждена точная успешная попытка CI для ссылки")
    return run["html_url"]


def _read_after_post(api: PublishingGitHub, receipt: InstallationReceipt) -> DeploymentRecord | None:
    """Перечитывает GitHub после POST, включая возможную потерю ответа."""
    return read_deployment_record(api, receipt)


def _require_trusted_author(api: PublishingGitHub) -> None:
    """Проверяет репозиторий и доверенного автора будущей записи."""
    if api.repository != EXPECTED_REPOSITORY:
        raise DeploymentPublishError("Неверный репозиторий Deployments")
    actor = api.authenticated_user()
    if actor.get("id") != OWNER_ID or actor.get("login") != OWNER_LOGIN:
        raise DeploymentPublishError("Запись Deployments создаёт только доверенный владелец")


def _require_deployment_version(version: str) -> None:
    """Исключает историческую версию, принятую через тег установки."""
    if version == LEGACY_INSTALLATION_VERSION:
        raise DeploymentPublishError("Исторический v3.2.0 остаётся записью через тег установки")


def _require_same_receipt(read_fresh: Callable[[], InstallationReceipt], receipt: InstallationReceipt) -> None:
    """Отказывает в записи при изменении технической приёмки во время сеанса."""
    if read_fresh() != receipt:
        raise DeploymentPublishError("Техническая приёмка или runtime VPS изменились во время записи")


def publish(api: PublishingGitHub, read_fresh: Callable[[], InstallationReceipt]) -> DeploymentRecord:
    """Создаёт или подтверждает одну запись после свежей технической приёмки.

    Args:
        api: GitHub-клиент оператора с правом Deployments write.
        read_fresh: Повторная техническая приёмка с доверенного VPS.

    Returns:
        Единственный проверенный Deployment с текущим статусом success.

    Raises:
        DeploymentPublishError: Приёмка, автор или состояние записи расходятся.
        DeploymentAcceptanceError: GitHub вернул конфликтующие данные записи.
        ControllerError: GitHub недоступен во время обязательного чтения.
    """
    _require_trusted_author(api)
    return _publish(api, read_fresh)


def _publish(api: PublishingGitHub, read_fresh: Callable[[], InstallationReceipt]) -> DeploymentRecord:
    """Пишет техническую приёмку с уже проверенным автором."""
    receipt = read_fresh()
    _require_deployment_version(receipt.version)
    payload = _expected_payload(api, receipt)
    ci_url = _trusted_ci_url(api, receipt)
    record = read_deployment_record(api, receipt)
    if record is not None and record.state is DeploymentState.SUCCESS:
        _require_same_receipt(read_fresh, receipt)
        return record
    if record is not None and record.state is not DeploymentState.MISSING:
        raise DeploymentPublishError("Существующий Deployment имеет конфликтующий текущий статус")
    if record is None:
        body = {
            "ref": receipt.sha, "task": TASK, "auto_merge": False, "required_contexts": [],
            "payload": payload, "environment": ENVIRONMENT,
            "description": f"Приёмка {receipt.version} на VPS",
            "transient_environment": False, "production_environment": True,
        }
        _require_same_receipt(read_fresh, receipt)
        try:
            api.post("/deployments", body)
        except PublishingError as error:
            # Не повторяем POST вслепую: ответ мог потеряться после записи.
            record = _read_after_post(api, receipt)
            if record is None:
                raise DeploymentPublishError("Создание Deployment не подтверждено GitHub") from error
        else:
            record = _read_after_post(api, receipt)
            if record is None:
                raise DeploymentPublishError("Созданный Deployment не найден при перечитывании")
        if record.state is DeploymentState.SUCCESS:
            _require_same_receipt(read_fresh, receipt)
            return record
        if record.state is not DeploymentState.MISSING:
            raise DeploymentPublishError("Созданный Deployment получил конфликтующий статус")
    assert record is not None
    body = {
        "state": "success", "environment": ENVIRONMENT, "auto_inactive": False,
        "description": f"Принято {receipt.version}; CI run {receipt.run_id} attempt {receipt.run_attempt}",
        "log_url": ci_url,
    }
    _require_same_receipt(read_fresh, receipt)
    try:
        api.post(f"/deployments/{record.deployment_id}/statuses", body)
    except PublishingError as error:
        observed = _read_after_post(api, receipt)
        if observed is not None and observed.state is DeploymentState.SUCCESS:
            return observed
        raise DeploymentPublishError("Создание статуса не подтверждено GitHub") from error
    observed = _read_after_post(api, receipt)
    if observed is None or observed.state is not DeploymentState.SUCCESS:
        raise DeploymentPublishError("После записи статус success не подтверждён")
    return observed


def deploy_and_publish(api: PublishingGitHub, version: str, sha: str, *, kind: str,
                       deploy: Callable[[], None], read_fresh: Callable[[], InstallationReceipt]) -> DeploymentRecord:
    """Устанавливает точный immutable Release и записывает техническую приёмку.

    Args:
        api: GitHub-клиент оператора с правом Deployments write.
        version: Канонический тег опубликованного выпуска.
        sha: Полный ожидаемый SHA выпуска.
        kind: Вид выпуска ordinary или emergency.
        deploy: Выполняет установку и сигнализирует об отказе исключением.
        read_fresh: Повторно проверяет установленный выпуск на VPS.

    Returns:
        Единственный проверенный Deployment с текущим статусом success.

    Raises:
        DeploymentPublishError: Выпуск или автор не прошёл проверку, либо после установки
            запись приёмки не подтверждена. Ошибки проверки GitHub и установки также
            могут передаваться из соответствующих зависимостей.
    """
    _require_deployment_version(version)
    if kind not in ("ordinary", "emergency"):
        raise DeploymentPublishError("Неизвестный вид выпуска")
    _require_trusted_author(api)
    release_id, _, _ = _published_release(api, version, sha, kind)
    deploy()

    def read_expected() -> InstallationReceipt:
        receipt = read_fresh()
        if (receipt.version, receipt.sha, receipt.kind, receipt.release_id) != (version, sha, kind, release_id):
            raise DeploymentPublishError("Техническая приёмка VPS не совпала с проверенным перед установкой Release")
        return receipt

    try:
        return _publish(api, read_expected)
    except _OPERATION_ERRORS as error:
        raise DeploymentPublishError(
            "deploy-prod завершился успешно, запись Deployments не подтверждена. "
            f"Повторите только --publish для этого выпуска. Причина: {error}"
        ) from error


def main() -> None:
    """Устанавливает выпуск, завершает запись приёмки или проверяет VPS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="точный опубликованный релиз vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный ожидаемый SHA выпуска")
    parser.add_argument("--ssh-host", required=True, help="проверенный адрес user@host")
    parser.add_argument("--known-hosts", required=True, type=Path, help="файл с проверенным ключом VPS")
    parser.add_argument("--identity", type=Path, help="локальный путь к ключу SSH")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--deploy", action="store_true", help="установить выпуск и записать приёмку")
    mode.add_argument("--publish", action="store_true", help="записать приёмку уже установленного выпуска")
    parser.add_argument("--emergency", action="store_true", help="установить аварийный выпуск")
    parser.add_argument("--reason", help="причина установки аварийного выпуска")
    args = parser.parse_args()
    if args.emergency and not args.deploy:
        parser.error("--emergency используется только вместе с --deploy")
    if args.emergency != (args.reason is not None):
        parser.error("--reason обязателен только при --emergency")
    reason = args.reason.strip() if args.reason is not None else None
    if reason is not None and (not reason or len(reason) > 1000 or any(c in args.reason for c in "\r\n\x00")):
        parser.error("Причина аварийной установки должна быть однострочной, от 1 до 1000 символов")

    def read_fresh() -> InstallationReceipt:
        return read_receipt(args.version, args.sha, ssh_host=args.ssh_host,
                            known_hosts=args.known_hosts, identity=args.identity)

    def deploy() -> None:
        command = ("sudo", "-n", "deploy-prod", args.version)
        if reason is not None:
            command += ("--emergency", f"--reason={reason}")
        try:
            run_vps_command(command, ssh_host=args.ssh_host, known_hosts=args.known_hosts, identity=args.identity,
                            capture_output=False, timeout=3600)
        except InstallationTagError as error:
            raise DeploymentPublishError(
                f"Успех deploy-prod не подтверждён. Проверьте фактическое состояние VPS. Причина: {error}"
            ) from error

    try:
        if repository_from_origin() != EXPECTED_REPOSITORY:
            raise DeploymentPublishError("Origin не совпадает с репозиторием проекта")
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if args.deploy or args.publish:
            if args.deploy:
                kind = "emergency" if args.emergency else "ordinary"
                record = deploy_and_publish(api, args.version, args.sha, kind=kind, deploy=deploy, read_fresh=read_fresh)
            else:
                _require_deployment_version(args.version)
                record = publish(api, read_fresh)
            print(json.dumps({"deployment_id": record.deployment_id, "status_id": record.status_id,
                              "state": record.state.value}, ensure_ascii=False))
        else:
            receipt = read_fresh()
            _expected_payload(api, receipt)
            record = read_deployment_record(api, receipt)
            print(json.dumps({"receipt": asdict(receipt), "deployment_id": record.deployment_id if record else None,
                              "state": record.state.value if record else "absent"}, ensure_ascii=False))
    except _OPERATION_ERRORS as error:
        print(f"Приёмка Deployments: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
