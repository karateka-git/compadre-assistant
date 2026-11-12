"""Читает и проверяет запись независимой приёмки в GitHub Deployments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.full_ci_controller import GitHub
from scripts.publish_installation_tag import InstallationReceipt, _published_release
from scripts.release_sqlite_summary import InstallationSchemaEvidence, build_summary, expected_migration_at


ENVIRONMENT = "production"
TASK = "compadre:acceptance"
OWNER_ID = 42288695
OWNER_LOGIN = "karateka-git"
# Единственный выпуск, принятый до появления записи Deployments.
LEGACY_INSTALLATION_VERSION = "v3.2.0"


class DeploymentAcceptanceError(RuntimeError):
    """Означает, что GitHub не доказал единственную точную запись приёмки."""


class DeploymentState(Enum):
    """Представляет текущее состояние учётной записи приёмки."""

    # Deployment создан, но статус ещё не записан; повтор публикации возможен.
    MISSING = "missing"
    # Только этот статус подтверждает завершённую независимую приёмку.
    SUCCESS = "success"
    # Известные незавершённые и отказные состояния не дают допуска.
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    QUEUED = "queued"
    FAILURE = "failure"
    ERROR = "error"
    INACTIVE = "inactive"
    # Неизвестное значение GitHub не приравнивается к успешной приёмке.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DeploymentRecord:
    """Связывает проверенный Deployment с его текущим статусом."""

    deployment_id: int
    state: DeploymentState
    status_id: int | None


def _positive(value: object, label: str) -> int:
    """Проверяет обязательный положительный ID GitHub."""
    if type(value) is not int or value <= 0:
        raise DeploymentAcceptanceError(f"Некорректный ID {label}")
    return value


def _owner(value: object, label: str) -> None:
    """Проверяет постоянный ID и login доверенного автора."""
    if (not isinstance(value, dict) or value.get("id") != OWNER_ID
            or value.get("login") != OWNER_LOGIN):
        raise DeploymentAcceptanceError(f"Недоверенный автор {label}")


def _expected_payload(api: GitHub, receipt: InstallationReceipt) -> dict[str, object]:
    """Сверяет свежую приёмку с immutable Release и миграцией коммита."""
    if api.repository != EXPECTED_REPOSITORY:
        raise DeploymentAcceptanceError("Репозиторий Deployment не совпадает с проектом")
    try:
        evidence = InstallationSchemaEvidence.from_receipt(asdict(receipt), receipt.version, receipt.sha)
    except (TypeError, ValueError) as error:
        raise DeploymentAcceptanceError("Некорректная техническая приёмка") from error
    release_id, run_id, run_attempt = _published_release(api, receipt.version, receipt.sha, receipt.kind)
    if (release_id != evidence.release_id
            or (run_id is not None and (run_id, run_attempt) != (evidence.run_id, evidence.run_attempt))
            or expected_migration_at(api, receipt.sha) != evidence.migration):
        raise DeploymentAcceptanceError("Release, CI или миграция расходятся с приёмкой")
    return {key: value for key, value in asdict(receipt).items() if key != "runtime_id"}


def read_deployment_record(api: GitHub, receipt: InstallationReceipt) -> DeploymentRecord | None:
    """Находит единственный Deployment и проверяет его текущий статус.

    Args:
        api: Читающий GitHub-клиент репозитория выпуска.
        receipt: Свежая техническая приёмка точного установленного выпуска.

    Returns:
        Проверенная запись либо None, если Deployment ещё отсутствует.

    Raises:
        DeploymentAcceptanceError: Найден конфликт, дубликат или плохой ответ.
        ControllerError: GitHub API недоступен или список страниц неполон.
        DeploySelectionError: Immutable Release не подтвердился.
    """
    expected = _expected_payload(api, receipt)
    path = "/deployments"
    records = api.pages(path, sha=receipt.sha, task=TASK, environment=ENVIRONMENT)
    if len(records) > 1:
        raise DeploymentAcceptanceError("Найдено несколько записей приёмки для точного SHA")
    if not records:
        return None
    raw = records[0]
    if (not isinstance(raw, dict) or raw.get("sha") != receipt.sha or raw.get("ref") != receipt.sha
            or raw.get("task") != TASK or raw.get("environment") != ENVIRONMENT
            or raw.get("payload") != expected or raw.get("transient_environment") is not False
            or raw.get("production_environment") is not True):
        raise DeploymentAcceptanceError("Deployment не соответствует точной приёмке")
    _owner(raw.get("creator"), "Deployment")
    deployment_id = _positive(raw.get("id"), "Deployment")
    statuses = api.pages(f"/deployments/{deployment_id}/statuses")
    if not statuses:
        return DeploymentRecord(deployment_id, DeploymentState.MISSING, None)
    dated = []
    for candidate in statuses:
        if not isinstance(candidate, dict):
            raise DeploymentAcceptanceError("Некорректный статус Deployment")
        status_id = _positive(candidate.get("id"), "статуса Deployment")
        created_at = candidate.get("created_at")
        try:
            created = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError) as error:
            raise DeploymentAcceptanceError("Некорректная дата статуса Deployment") from error
        dated.append((created, status_id, candidate))
    newest = max(item[0] for item in dated)
    latest = [item for item in dated if item[0] == newest]
    # GitHub документирует текущий статус как самый новый, но не порядок списка.
    # При одинаковой точности timestamp невозможно доказать, какой статус текущий.
    if len(latest) != 1:
        raise DeploymentAcceptanceError("Неоднозначный текущий статус Deployment")
    status = latest[0][2]
    if not isinstance(status, dict) or status.get("environment") != ENVIRONMENT:
        raise DeploymentAcceptanceError("Статус относится к другому окружению")
    _owner(status.get("creator"), "статуса Deployment")
    status_id = latest[0][1]
    value = status.get("state")
    known = {item.value for item in DeploymentState if item not in (DeploymentState.MISSING, DeploymentState.UNKNOWN)}
    state = DeploymentState(value) if isinstance(value, str) and value in known else DeploymentState.UNKNOWN
    return DeploymentRecord(deployment_id, state, status_id)


def require_accepted_deployment(api: GitHub, receipt: InstallationReceipt) -> DeploymentRecord:
    """Допускает только текущий доверенный `success` единственной записи.

    Args:
        api: Читающий GitHub-клиент репозитория выпуска.
        receipt: Свежая техническая приёмка точного установленного выпуска.

    Returns:
        Подтверждённый Deployment со статусом `success`.

    Raises:
        DeploymentAcceptanceError: Записи нет либо её текущий статус не `success`.
        ControllerError: GitHub API недоступен или список страниц неполон.
        DeploySelectionError: Immutable Release не подтвердился.
    """
    record = read_deployment_record(api, receipt)
    if record is None or record.state is not DeploymentState.SUCCESS:
        raise DeploymentAcceptanceError("Текущая приёмка GitHub Deployments не подтверждена")
    return record


def summarize_deployment(api: GitHub, version: str, sha: str, release_id: int,
                         run_id: int | None, run_attempt: int | None,
                         expected_migration: str) -> dict[str, object]:
    """Показывает сведения SQLite из проверенной записи Deployments.

    Args:
        api: Читающий GitHub-клиент репозитория выпуска.
        version: Каноническая версия опубликованного выпуска.
        sha: Полный SHA этого выпуска.
        release_id: ID проверенного immutable Release.
        run_id: Прогон CI обычного выпуска или None для аварийного.
        run_attempt: Попытка CI обычного выпуска или None для аварийного.
        expected_migration: Последняя миграция в точном коммите.

    Returns:
        Сводка записи при текущем доверенном success. Она не удостоверяет
        актуальное состояние VPS без свежей технической приёмки.

    Raises:
        DeploymentAcceptanceError: Запись, payload или источник расходятся.
        ControllerError: GitHub не ответил на обязательное чтение.
    """
    try:
        summary = build_summary(version, sha, release_id, run_id, run_attempt, expected_migration, None)
    except ValueError as error:
        raise DeploymentAcceptanceError("Исходные данные сводки SQLite не подтверждены") from error
    records = api.pages("/deployments", sha=sha, task=TASK, environment=ENVIRONMENT)
    if not records:
        return summary | {"deployment_id": None, "deployment_status_id": None,
                          "deployment_state": "absent"}
    if len(records) != 1 or not isinstance(records[0], dict):
        raise DeploymentAcceptanceError("Неоднозначная запись Deployments для сводки SQLite")
    payload = records[0].get("payload")
    if not isinstance(payload, dict):
        raise DeploymentAcceptanceError("Некорректный payload Deployments для сводки SQLite")
    try:
        # Заглушка нужна только для общей проверки формы; VPS здесь не читается.
        record_claim = InstallationReceipt(**payload, runtime_id="0" * 64)
    except (TypeError, ValueError) as error:
        raise DeploymentAcceptanceError("Некорректный payload Deployments для сводки SQLite") from error
    if ((record_claim.version, record_claim.sha, record_claim.release_id, record_claim.migration)
            != (version, sha, release_id, expected_migration)
            or (run_id is not None and (record_claim.run_id, record_claim.run_attempt) != (run_id, run_attempt))):
        raise DeploymentAcceptanceError("Запись Deployments расходится с опубликованным Release")
    record = read_deployment_record(api, record_claim)
    if record is None:
        raise DeploymentAcceptanceError("Запись Deployments исчезла во время сверки")
    result = summary | {"deployment_id": record.deployment_id, "deployment_status_id": record.status_id,
                        "deployment_state": record.state.value}
    if record.state is not DeploymentState.SUCCESS:
        return result
    return result | {"installation_status": "accepted", "run_id": record_claim.run_id,
                     "run_attempt": record_claim.run_attempt, "generation_id": record_claim.generation_id,
                     "applied_migration": record_claim.migration}
