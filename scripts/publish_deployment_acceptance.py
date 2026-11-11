"""Публикует подтверждённую приёмку выпуска в GitHub Deployments."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sys

from scripts.create_release_branch import EXPECTED_REPOSITORY, ReleaseBranchError, repository_from_origin
from scripts.deployment_acceptance import (
    DeploymentAcceptanceError, DeploymentRecord, DeploymentState, ENVIRONMENT, OWNER_ID, OWNER_LOGIN, TASK,
    _expected_payload, read_deployment_record,
)
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.publish_installation_tag import InstallationReceipt, InstallationTagError, read_receipt
from scripts.select_deploy_release import DeploySelectionError


class DeploymentPublishError(RuntimeError):
    """Означает, что запись приёмки нельзя безопасно создать или завершить."""


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


def publish(api: PublishingGitHub, read_fresh: Callable[[], InstallationReceipt],
            confirmed_runtime: str) -> DeploymentRecord:
    """Создаёт или подтверждает одну запись приёмки после живой проверки.

    Args:
        api: GitHub-клиент оператора с правом Deployments write.
        read_fresh: Повторная техническая приёмка с доверенного VPS.
        confirmed_runtime: Runtime, для которого владелец проверил Telegram и VK.

    Returns:
        Единственный проверенный Deployment с текущим статусом success.

    Raises:
        DeploymentPublishError: Приёмка, автор или состояние записи расходятся.
        DeploymentAcceptanceError: GitHub вернул конфликтующие данные записи.
        ControllerError: GitHub недоступен во время обязательного чтения.
    """
    if api.repository != EXPECTED_REPOSITORY or not re.fullmatch(r"[0-9a-f]{64}", confirmed_runtime):
        raise DeploymentPublishError("Нужно подтвердить живые ответы для точного runtime VPS")
    actor = api.authenticated_user()
    if actor.get("id") != OWNER_ID or actor.get("login") != OWNER_LOGIN:
        raise DeploymentPublishError("Запись Deployments создаёт только доверенный владелец")
    receipt = read_fresh()
    if receipt.version == "v3.2.0":
        raise DeploymentPublishError("Исторический v3.2.0 остаётся записью через тег установки")
    if receipt.runtime_id != confirmed_runtime:
        raise DeploymentPublishError("Runtime VPS изменился после проверки живых ответов")
    payload = _expected_payload(api, receipt)
    ci_url = _trusted_ci_url(api, receipt)
    record = read_deployment_record(api, receipt)
    if record is not None and record.state is DeploymentState.SUCCESS:
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
            return record
        if record.state is not DeploymentState.MISSING:
            raise DeploymentPublishError("Созданный Deployment получил конфликтующий статус")
    assert record is not None
    body = {
        "state": "success", "environment": ENVIRONMENT, "auto_inactive": False,
        "description": f"Принято {receipt.version}; CI run {receipt.run_id} attempt {receipt.run_attempt}",
        "log_url": ci_url,
    }
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


def main() -> None:
    """Проверяет VPS и по явному режиму публикует учётную запись приёмки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="точный опубликованный релиз vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный ожидаемый SHA выпуска")
    parser.add_argument("--ssh-host", required=True, help="проверенный адрес user@host")
    parser.add_argument("--known-hosts", required=True, type=Path, help="файл с проверенным ключом VPS")
    parser.add_argument("--identity", type=Path, help="локальный путь к ключу SSH")
    parser.add_argument("--publish", action="store_true", help="создать или завершить запись приёмки")
    parser.add_argument("--confirm-live", metavar="RUNTIME_ID", help="подтверждённый владельцем runtime")
    args = parser.parse_args()
    if args.publish != (args.confirm_live is not None):
        parser.error("--confirm-live обязателен только при --publish")

    def read_fresh() -> InstallationReceipt:
        return read_receipt(args.version, args.sha, ssh_host=args.ssh_host,
                            known_hosts=args.known_hosts, identity=args.identity)

    try:
        if repository_from_origin() != EXPECTED_REPOSITORY:
            raise DeploymentPublishError("Origin не совпадает с репозиторием проекта")
        api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
        if args.publish:
            record = publish(api, read_fresh, args.confirm_live)
            print(json.dumps({"deployment_id": record.deployment_id, "status_id": record.status_id,
                              "state": record.state.value}, ensure_ascii=False))
        else:
            receipt = read_fresh()
            _expected_payload(api, receipt)
            record = read_deployment_record(api, receipt)
            print(json.dumps({"receipt": asdict(receipt), "deployment_id": record.deployment_id if record else None,
                              "state": record.state.value if record else "absent"}, ensure_ascii=False))
    except (ControllerError, DeploymentAcceptanceError, DeploymentPublishError, DeploySelectionError,
            InstallationTagError, PublishingError, ReleaseBranchError) as error:
        print(f"Приёмка Deployments: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
