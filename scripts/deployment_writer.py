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


def create_reserved(api: PublishingGitHub, attempt: DeploymentAttempt,
                    claim: Callable[[str], bool]) -> DeploymentRecord:
    """Создаёт один Deployment по принятой резервации и проверяет ответ POST.

    Args:
        api: Клиент доверенного писателя.
        attempt: Идентичность попытки и базы из резервации.
        claim: Выдача однократного права записи контроллером.

    Returns:
        Созданная запись либо подтверждённое состояние после обрыва.

    Raises:
        DeploymentPublishError: Отправка уже разрешалась, но точный результат не найден.
    """
    if claim("create"):
        body = {"ref": attempt.sha, "task": records.TASK, "auto_merge": False, "required_contexts": [],
                "payload": asdict(attempt), "environment": records.ENVIRONMENT,
                "transient_environment": False, "production_environment": True}
        try:
            raw = api.post("/deployments", body)
        except PublishingError:
            pass  # При потере ответа разрешено только чтение результата.
        else:
            identifier, _ = records._deployment(api, raw)
            if records._attempt(raw) != attempt:
                raise DeploymentPublishError("Ответ создания Deployment относится к другой попытке")
            return DeploymentRecord(identifier, DeploymentState.MISSING, None)
    raw, status = records._latest_production(api)
    if records._v1(raw) or records._attempt(raw) != attempt:
        raise DeploymentPublishError("Исходный POST Deployment не подтверждён; требуется сверка")
    return status


def write_reserved_status(api: PublishingGitHub, attempt: DeploymentAttempt, before: DeploymentRecord,
                          target: DeploymentState, migration: str, claim: Callable[[str], InstallationReceipt | None],
                          completed: InstallationReceipt | None = None) -> DeploymentRecord:
    """Сверяет свежую приёмку VPS и подтверждает переход по ответу GitHub.

    Args:
        api: Клиент публикации.
        attempt: Закреплённая попытка и исходная база.
        before: Подтверждённое состояние этой записи.
        target: Требуемый статус.
        migration: Проверенная CI ожидаемая миграция кандидата.
        claim: Однократное право записи и фактическая приёмка с VPS.
        completed: Терминальный результат установки или возврата.

    Returns:
        Точный статус с ID. Для терминального результата сверяется также текущий статус.

    Raises:
        DeploymentPublishError: Состояние, приёмка или ответ GitHub расходятся.
    """
    terminal = target in (DeploymentState.SUCCESS, DeploymentState.FAILURE)
    if target is DeploymentState.IN_PROGRESS and completed is None:
        description = f"v1 attempt={attempt.attempt_key}"
    elif terminal and completed is not None:
        records._schema_evidence(completed)
        if target is DeploymentState.SUCCESS:
            if ((completed.version, completed.sha, completed.kind, completed.release_id, completed.run_id,
                 completed.run_attempt, completed.migration) != (attempt.version, attempt.sha, attempt.kind,
                 attempt.release_id, attempt.run_id, attempt.run_attempt, migration)):
                raise DeploymentPublishError("Приёмка не подтверждает установку выбранного PATCH")
            description = _description(completed.generation_id, completed.migration)
        else:
            base = attempt.baseline
            if (completed.version, completed.sha, completed.release_id) != (base.version, base.sha, base.release_id):
                raise DeploymentPublishError("Приёмка возврата расходится с исходной базой")
            description = f"v1 restored={base.deployment_id}/{base.status_id}"
    else:
        raise DeploymentPublishError("Недопустимый переход статуса PATCH")
    if before.state is target and before.description == description:
        # Восстановление уже опубликованного результата.
        return before
    source = DeploymentState.IN_PROGRESS if terminal else DeploymentState.MISSING
    if before.state is not source or terminal and before.description != f"v1 attempt={attempt.attempt_key}":
        raise DeploymentPublishError("Исходный статус не допускает этот переход")
    receipt = claim("finish" if terminal else "in_progress")
    expected_id = None
    if receipt is not None:
        records._schema_evidence(receipt)
        if terminal and receipt != completed:
            raise DeploymentPublishError("Приёмка изменилась перед записью результата")
        base = attempt.baseline
        if not terminal and (receipt.sha, receipt.version, receipt.release_id) != (base.sha, base.version, base.release_id):
            raise DeploymentPublishError("Резервация не подтверждает принятую базу")
        try:
            raw = api.post(f"/deployments/{before.deployment_id}/statuses", {
                "environment": records.ENVIRONMENT, "state": target.value, "description": description,
                "auto_inactive": False,
            })
        except PublishingError:
            pass
        else:
            result = records._parse_status(before.deployment_id, raw)
            if (result.state, result.description) != (target, description):
                raise DeploymentPublishError("Ответ POST не подтверждает запрошенный статус PATCH")
            expected_id = result.status_id
            if not terminal:
                return result
    result = records._current_status(api, before.deployment_id)
    if ((result.state, result.description) != (target, description)
            or expected_id is not None and result.status_id != expected_id):
        raise DeploymentPublishError("Не подтверждён текущий статус PATCH")
    return result


def current_attempt(
    api: GitHub, attempt: DeploymentAttempt, deployment_id: int,
) -> tuple[DeploymentRecord, InstallationReceipt]:
    """Проверяет владельца и payload самой свежей попытки production.

    Args:
        api: Читающий GitHub-клиент контроллера или runner.
        attempt: Реквизиты из сохранённого допуска.
        deployment_id: Точный ID D18, выданный GitHub.

    Returns:
        Текущий доверенный статус этой попытки и подтверждённая приёмка D17.

    Raises:
        DeploymentPublishError: Другая попытка получила владение.
    """
    raw, status = records._latest_production(api)
    if status.deployment_id != deployment_id or records._attempt(raw) != attempt:
        raise DeploymentPublishError("Production принадлежит другой попытке")
    records._check_attempt_release(api, attempt)
    return status, records.baseline_receipt(api, attempt.baseline)


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

    def verify_created(raw, status, expected_id):
        if (records._v1(raw) or records._attempt(raw) != attempt
                or expected_id is not None and status.deployment_id != expected_id):
            raise DeploymentPublishError("Создание точной попытки не подтверждено")
        records.baseline_receipt(api, attempt.baseline)
        return status

    raw, status = records._latest_production(api)
    if not records._v1(raw) and records._attempt(raw).attempt_key == attempt.attempt_key:
        return verify_created(raw, status, status.deployment_id)
    baseline, _ = records._resolve_observed_baseline(api, (raw, status))
    if baseline != attempt.baseline:
        raise DeploymentPublishError("Перед созданием попытки изменилась принятая база")
    body = {"ref": attempt.sha, "task": records.TASK, "auto_merge": False, "required_contexts": [],
            "payload": asdict(attempt), "environment": records.ENVIRONMENT,
            "transient_environment": False, "production_environment": True}

    def observe(expected_id):
        raw, status = records._latest_production(api)
        return verify_created(raw, status, expected_id)

    return _post_once(api, "/deployments", body, claim, "create", observe)


def write_status(api: PublishingGitHub, attempt: DeploymentAttempt, deployment_id: int, state: DeploymentState,
                 claim: Callable[[str], InstallationReceipt | None],
                 completed: InstallationReceipt | None = None) -> DeploymentRecord:
    """Записывает переход D18 после сверки свежей приёмки VPS.

    Args:
        api: Клиент доверенного автора.
        attempt: Сохранённые реквизиты исходной попытки.
        deployment_id: Точный ID D18.
        state: in_progress, success или failure после подтверждённого возврата.
        claim: Выдаёт долговечное право POST вместе со свежей приёмкой под lock.
            Возвращает None, если это право уже выдавалось.
        completed: Приёмка кандидата после очистки либо доказанного возврата.
            Требуется для терминального статуса.

    Returns:
        Перечитанный статус GitHub.

    Raises:
        DeploymentPublishError: Переход, результат или его запись не подтверждены.
    """
    _require_trusted_author(api)
    before, base_receipt = current_attempt(api, attempt, deployment_id)
    if completed is not None:
        records._schema_evidence(completed)
    if state is DeploymentState.IN_PROGRESS:
        description = f"v1 attempt={attempt.attempt_key}"
    elif state is DeploymentState.SUCCESS and completed is not None:
        if ((completed.version, completed.sha, completed.kind, completed.release_id,
             completed.run_id, completed.run_attempt) !=
                (attempt.version, attempt.sha, attempt.kind, attempt.release_id, attempt.run_id, attempt.run_attempt)
                or completed.migration != expected_migration_at(api, attempt.sha)):
            raise DeploymentPublishError("Приёмка VPS расходится с кандидатом")
        description = _description(completed.generation_id, completed.migration)
    elif state is DeploymentState.FAILURE and completed is not None:
        base = attempt.baseline
        if replace(completed, runtime_id=base_receipt.runtime_id) != base_receipt:
            raise DeploymentPublishError("VPS не подтвердил прежнюю принятую установку")
        description = f"v1 restored={base.deployment_id}/{base.status_id}"
    else:
        raise DeploymentPublishError("Недопустимый переход статуса")
    if before.state is state and before.description == description:
        return before
    source = DeploymentState.MISSING if state is DeploymentState.IN_PROGRESS else DeploymentState.IN_PROGRESS
    if before.state is not source:
        raise DeploymentPublishError("Исходный статус попытки не разрешает переход")
    if source is DeploymentState.IN_PROGRESS and before.description != f"v1 attempt={attempt.attempt_key}":
        raise DeploymentPublishError("Статус in_progress не подтвердил исходный ключ")

    def authorize(action):
        receipt = claim(action)
        if receipt is None:
            return False
        if state is DeploymentState.IN_PROGRESS:
            records._schema_evidence(receipt)
            if replace(receipt, runtime_id=base_receipt.runtime_id) != base_receipt:
                raise DeploymentPublishError("VPS не подтвердил прежнюю принятую установку")
        elif receipt != completed:
            raise DeploymentPublishError("Runtime VPS изменился после проверки")
        return True

    def observe(expected_id):
        observed, _ = current_attempt(api, attempt, deployment_id)
        if (observed.state is not state or observed.description != description
                or expected_id is not None and observed.status_id != expected_id):
            raise DeploymentPublishError("Точный статус после POST не подтверждён")
        return observed

    body = {"state": state.value, "environment": records.ENVIRONMENT,
            "auto_inactive": False, "description": description}
    # При потере ответа success и failure делят одно право завершения D18.
    action = "finish" if source is DeploymentState.IN_PROGRESS else "in_progress"
    return _post_once(api, f"/deployments/{deployment_id}/statuses", body, authorize, action, observe)
