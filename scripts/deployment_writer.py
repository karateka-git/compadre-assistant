"""Связывает записи GitHub с устойчивым намерением единственного писателя VPS."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
import re
from urllib.error import HTTPError

from scripts import deployment_acceptance as records
from scripts.deployment_acceptance import DeploymentAttempt, DeploymentRecord, DeploymentState
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.full_ci_controller import GitHub
from scripts.publish_deployment_acceptance import DeploymentPublishError, _require_trusted_author
from scripts.publish_installation_tag import InstallationReceipt
from scripts.release_sqlite_summary import expected_migration_at


def _description(generation: str, migration: str) -> str:
    """Проверяет представимость результата в ограниченном поле статуса GitHub."""
    value = f"v1 gen={generation} migration={migration}"
    if len(value) > 140 or not re.fullmatch(r"v1 gen=[0-9a-f]{32} migration=[0-9]{4}_[a-z0-9_]+\.sql", value):
        raise DeploymentPublishError("Результат установки не помещается в статус Deployment")
    return value


def current_attempt(api: GitHub, attempt: DeploymentAttempt, deployment_id: int) -> DeploymentRecord:
    """Проверяет владельца и payload самой свежей попытки production.

    Args:
        api: Читающий GitHub-клиент контроллера или runner.
        attempt: Реквизиты из сохранённого допуска.
        deployment_id: Точный ID D18, выданный GitHub.

    Returns:
        Текущий доверенный статус этой попытки.

    Raises:
        DeploymentPublishError: Другая попытка получила владение.
    """
    raw, status = records._latest_production(api)
    if status.deployment_id != deployment_id or records._attempt(raw) != attempt:
        raise DeploymentPublishError("Production принадлежит другой попытке")
    records._check_attempt_release(api, attempt)
    records.baseline_receipt(api, attempt.baseline)
    return status


def _post_once(api, path, body, claim, action, read):
    """Запрашивает долговечное право одной отправки и независимо перечитывает результат."""
    if claim(action) is not True:
        raise DeploymentPublishError("Намерение POST уже сохранено. Результат требует сверки")
    try:
        response = api.post(path, body)
    except PublishingError as error:
        cause = error.__cause__
        if isinstance(cause, HTTPError) and cause.code in (400, 401, 403, 404, 409, 422):
            raise DeploymentPublishError(f"GitHub отклонил POST: HTTP {cause.code}") from error
        # Транспорт мог потерять ответ уже выполненного запроса.
        return read(None)
    object_id = records._positive(response.get("id"), "ответа POST")
    return read(object_id)


def create_attempt(api: PublishingGitHub, attempt: DeploymentAttempt,
                   claim: Callable[[str], bool]) -> DeploymentRecord:
    """Создаёт D18 либо подтверждает уже созданную попытку по исходному ключу.

    Args:
        api: Клиент доверенного автора с выбранным читателем v2.
        attempt: Неизменные реквизиты попытки и проверенная база допуска.
        claim: Под блокировкой VPS сохраняет намерение для данной попытки,
            возвращая True только при первой выдаче права отправить этот POST.

    Returns:
        Проверенная свежая запись этой попытки.

    Raises:
        DeploymentPublishError: База изменилась или создание не подтверждено.
    """
    if not records._reader_v2():
        raise DeploymentPublishError("Писатель v2 требует включённого читателя v2")
    _require_trusted_author(api)
    records._check_attempt_release(api, attempt)
    _description("0" * 32, expected_migration_at(api, attempt.sha))

    def observe(expected_id):
        raw, status = records._latest_production(api)
        if (records._v1(raw) or records._attempt(raw) != attempt
                or expected_id is not None and status.deployment_id != expected_id):
            raise DeploymentPublishError("Создание точной попытки не подтверждено")
        records.baseline_receipt(api, attempt.baseline)
        return status

    raw, status = records._latest_production(api)
    if not records._v1(raw) and records._attempt(raw).attempt_key == attempt.attempt_key:
        return observe(status.deployment_id)
    if records.resolve_accepted_baseline(api) != attempt.baseline:
        raise DeploymentPublishError("Перед созданием попытки изменилась принятая база")
    body = {"ref": attempt.sha, "task": records.TASK, "auto_merge": False, "required_contexts": [],
            "payload": asdict(attempt), "environment": records.ENVIRONMENT,
            "transient_environment": False, "production_environment": True}
    return _post_once(api, "/deployments", body, claim, "create", observe)


def write_status(api: PublishingGitHub, attempt: DeploymentAttempt, deployment_id: int, state: DeploymentState,
                 claim: Callable[[str], bool], read_fresh: Callable[[], InstallationReceipt]) -> DeploymentRecord:
    """Записывает переход D18 после сверки свежей приёмки VPS.

    Args:
        api: Клиент доверенного автора.
        attempt: Сохранённые реквизиты исходной попытки.
        deployment_id: Точный ID D18.
        state: in_progress, success или failure после подтверждённого возврата.
        claim: Однократное долговечное право POST статуса для этой попытки.
        read_fresh: При success подтверждает завершённую очистку и runtime кандидата,
            иначе возвращает свежую приёмку прежнего поколения под владением попытки.

    Returns:
        Перечитанный статус GitHub.

    Raises:
        DeploymentPublishError: Переход, результат или его запись не подтверждены.
    """
    _require_trusted_author(api)
    before = current_attempt(api, attempt, deployment_id)
    receipt = read_fresh()
    records._schema_evidence(receipt)
    if state is DeploymentState.SUCCESS:
        if ((receipt.version, receipt.sha, receipt.kind, receipt.release_id, receipt.run_id, receipt.run_attempt) !=
                (attempt.version, attempt.sha, attempt.kind, attempt.release_id, attempt.run_id, attempt.run_attempt)
                or receipt.migration != expected_migration_at(api, attempt.sha)):
            raise DeploymentPublishError("Приёмка VPS расходится с кандидатом")
        description = _description(receipt.generation_id, receipt.migration)
    elif state in (DeploymentState.IN_PROGRESS, DeploymentState.FAILURE):
        base = attempt.baseline
        expected = records.baseline_receipt(api, base)
        if replace(receipt, runtime_id=expected.runtime_id) != expected:
            raise DeploymentPublishError("VPS не подтвердил прежнюю принятую установку")
        description = (f"v1 restored={base.deployment_id}/{base.status_id}"
                       if state is DeploymentState.FAILURE else f"v1 attempt={attempt.attempt_key}")
    else:
        raise DeploymentPublishError("Недопустимый переход статуса")
    if before.state is state and before.description == description:
        return before
    source = DeploymentState.MISSING if state is DeploymentState.IN_PROGRESS else DeploymentState.IN_PROGRESS
    if before.state is not source:
        raise DeploymentPublishError("Исходный статус попытки не разрешает переход")
    if source is DeploymentState.IN_PROGRESS and before.description != f"v1 attempt={attempt.attempt_key}":
        raise DeploymentPublishError("Статус in_progress не подтвердил исходный ключ")

    def observe(expected_id):
        observed = current_attempt(api, attempt, deployment_id)
        if (observed.state is not state or observed.description != description
                or expected_id is not None and observed.status_id != expected_id):
            raise DeploymentPublishError("Точный статус после POST не подтверждён")
        return observed

    body = {"state": state.value, "environment": records.ENVIRONMENT,
            "auto_inactive": False, "description": description}
    # При потере ответа success и failure делят одно право завершения D18.
    action = "finish" if source is DeploymentState.IN_PROGRESS else "in_progress"
    return _post_once(api, f"/deployments/{deployment_id}/statuses", body, claim, action, observe)
