"""Читает и проверяет запись независимой приёмки в GitHub Deployments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deploy_prepare import SHA_PATTERN, VERSION_PATTERN
from scripts.full_ci_controller import GitHub
from scripts.publish_installation_tag import InstallationReceipt, InstallationTagError, _published_release
from scripts.publish_release import PublishError
from scripts.release_sqlite_summary import InstallationSchemaEvidence, build_summary, expected_migration_at
from scripts.select_deploy_release import DeploySelectionError


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
    status_created_at: datetime | None = None


@dataclass(frozen=True)
class AcceptedBaseline:
    """Фиксирует Release и точный текущий статус приёмки для базы CI."""

    sha: str
    version: str
    release_id: int
    deployment_id: int
    status_id: int

    def __post_init__(self) -> None:
        """Проверяет каноническую форму реквизитов сохранённой базы."""
        if (not isinstance(self.sha, str) or not SHA_PATTERN.fullmatch(self.sha)
                or not isinstance(self.version, str) or not VERSION_PATTERN.fullmatch(self.version)):
            raise DeploymentAcceptanceError("Некорректные SHA или версия принятой базы")
        _positive(self.release_id, "Release")
        _positive(self.deployment_id, "Deployment")
        _positive(self.status_id, "статуса Deployment")


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
    evidence = _schema_evidence(receipt)
    release_id, run_id, run_attempt = _published_release(api, receipt.version, receipt.sha, receipt.kind)
    if (release_id != evidence.release_id
            or (run_id is not None and (run_id, run_attempt) != (evidence.run_id, evidence.run_attempt))
            or expected_migration_at(api, receipt.sha) != evidence.migration):
        raise DeploymentAcceptanceError("Release, CI или миграция расходятся с приёмкой")
    return {key: value for key, value in asdict(receipt).items() if key != "runtime_id"}


def _schema_evidence(receipt: InstallationReceipt) -> InstallationSchemaEvidence:
    """Проверяет каноническую форму реквизитов версии, CI и SQLite."""
    try:
        return InstallationSchemaEvidence.from_receipt(asdict(receipt), receipt.version, receipt.sha)
    except (TypeError, ValueError) as error:
        raise DeploymentAcceptanceError("Некорректная техническая приёмка") from error


def _deployment(api: GitHub, raw: object) -> tuple[int, str]:
    """Проверяет происхождение и commit записи в ответе GitHub."""
    if (api.repository != EXPECTED_REPOSITORY or not isinstance(raw, dict)
            or not isinstance(raw.get("sha"), str) or not SHA_PATTERN.fullmatch(raw["sha"])
            or raw.get("ref") != raw["sha"] or raw.get("task") != TASK or raw.get("environment") != ENVIRONMENT
            or raw.get("transient_environment") is not False or raw.get("production_environment") is not True):
        raise DeploymentAcceptanceError("Deployment не соответствует production-приёмке")
    _owner(raw.get("creator"), "Deployment")
    return _positive(raw.get("id"), "Deployment"), raw["sha"]


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
    deployment_id, sha = _deployment(api, raw)
    if sha != receipt.sha or raw.get("payload") != expected:
        raise DeploymentAcceptanceError("Deployment не соответствует точной приёмке")
    return _current_status(api, deployment_id)


def _current_status(api: GitHub, deployment_id: int) -> DeploymentRecord:
    """Подтверждает единственный текущий статус и его автора."""
    statuses = api.pages(f"/deployments/{deployment_id}/statuses")
    if not statuses:
        return DeploymentRecord(deployment_id, DeploymentState.MISSING, None)
    ids: set[int] = set()
    dated = []
    for candidate in statuses:
        if not isinstance(candidate, dict):
            raise DeploymentAcceptanceError("Некорректный статус Deployment")
        status_id = _positive(candidate.get("id"), "статуса Deployment")
        if status_id in ids:
            raise DeploymentAcceptanceError("Дубликат статуса Deployment")
        ids.add(status_id)
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
    return DeploymentRecord(deployment_id, state, status_id, newest)


def _claimed_receipt(raw: dict) -> InstallationReceipt:
    """Читает только реквизиты Deployment для сверки с опубликованным Release."""
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise DeploymentAcceptanceError("Некорректный payload Deployments")
    try:
        # Заглушка нужна для проверки общей формы; при чтении GitHub VPS не опрашивается.
        return InstallationReceipt(**payload, runtime_id="0" * 64)
    except (TypeError, ValueError) as error:
        raise DeploymentAcceptanceError("Некорректный payload Deployments") from error


def _baseline_claim(raw: dict, sha: str) -> InstallationReceipt:
    """Подтверждает форму базы и принадлежность payload тому же коммиту."""
    receipt = _claimed_receipt(raw)
    if receipt.sha != sha or receipt.version == LEGACY_INSTALLATION_VERSION:
        raise DeploymentAcceptanceError("Deployment и payload не подтверждают одну базу")
    _schema_evidence(receipt)
    return receipt


def _accepted_baseline(api: GitHub, receipt: InstallationReceipt, record: DeploymentRecord) -> AcceptedBaseline:
    """Сверяет принятую запись с Release, тегом и миграцией точного коммита."""
    if record.state is not DeploymentState.SUCCESS or record.status_id is None:
        raise DeploymentAcceptanceError("Текущий статус базы не подтверждает приёмку")
    try:
        _expected_payload(api, receipt)
    except (DeploySelectionError, InstallationTagError, PublishError, ValueError) as error:
        raise DeploymentAcceptanceError("Release, тег или миграция базы не подтверждены") from error
    return AcceptedBaseline(receipt.sha, receipt.version, receipt.release_id, record.deployment_id, record.status_id)


def resolve_accepted_baseline(api: GitHub) -> AcceptedBaseline:
    """Выбирает последний доверенный текущий success по времени статуса.

    Args:
        api: Читающий GitHub-клиент репозитория выпуска.

    Returns:
        Принятая база CI с точным SHA, Release, Deployment и статусом.

    Raises:
        DeploymentAcceptanceError: Запись отсутствует, не подтверждена или неоднозначна.
        ControllerError: GitHub API недоступен или список страниц неполон.
    """
    if api.repository != EXPECTED_REPOSITORY:
        raise DeploymentAcceptanceError("Репозиторий Deployment не совпадает с проектом")
    ids: set[int] = set()
    shas: set[str] = set()
    accepted = []
    for raw in api.pages("/deployments", task=TASK, environment=ENVIRONMENT):
        deployment_id, sha = _deployment(api, raw)
        if deployment_id in ids or sha in shas:
            raise DeploymentAcceptanceError("Найдены дубликаты записей приёмки")
        ids.add(deployment_id)
        shas.add(sha)
        receipt = _baseline_claim(raw, sha)
        record = _current_status(api, deployment_id)
        if record.state is DeploymentState.UNKNOWN:
            raise DeploymentAcceptanceError("Неизвестный текущий статус Deployment")
        if record.state is DeploymentState.SUCCESS:
            if record.status_created_at is None:
                raise DeploymentAcceptanceError("Не подтверждена дата приёмки Deployment")
            accepted.append((record.status_created_at, receipt, record))
    if not accepted:
        raise DeploymentAcceptanceError("Принятая база в GitHub Deployments отсутствует")
    newest = max(created_at for created_at, _, _ in accepted)
    latest = [(receipt, record) for created_at, receipt, record in accepted if created_at == newest]
    if len(latest) != 1:
        raise DeploymentAcceptanceError("Неоднозначная последняя принятая база в GitHub Deployments")
    receipt, record = latest[0]
    return _accepted_baseline(api, receipt, record)


def verify_accepted_baseline(api: GitHub, deployment_id: int, status_id: int) -> AcceptedBaseline:
    """Сверяет ранее выбранную базу по точным Deployment и текущему статусу.

    Args:
        api: Читающий GitHub-клиент репозитория выпуска.
        deployment_id: ID выбранной записи приёмки.
        status_id: ID статуса, сохранённый вместе с записью.

    Returns:
        Принятая база с прежним доверенным success.

    Raises:
        DeploymentAcceptanceError: Реквизиты записи или её текущий статус изменились.
        ControllerError: GitHub API недоступен или список страниц неполон.
    """
    if api.repository != EXPECTED_REPOSITORY:
        raise DeploymentAcceptanceError("Репозиторий Deployment не совпадает с проектом")
    _positive(deployment_id, "Deployment")
    _positive(status_id, "статуса Deployment")
    raw = api.get(f"/deployments/{deployment_id}")
    actual_id, sha = _deployment(api, raw)
    if actual_id != deployment_id:
        raise DeploymentAcceptanceError("GitHub вернул другую запись приёмки")
    record = _current_status(api, deployment_id)
    if record.status_id != status_id:
        raise DeploymentAcceptanceError("Текущий статус выбранной базы изменился")
    return _accepted_baseline(api, _baseline_claim(raw, sha), record)


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
    record_claim = _claimed_receipt(records[0])
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
