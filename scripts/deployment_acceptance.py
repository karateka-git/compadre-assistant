"""Читает и проверяет запись независимой приёмки в GitHub Deployments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import os
import re

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deploy_prepare import SHA_PATTERN, VERSION_PATTERN
from scripts.full_ci_controller import GitHub, HttpPhase, http_phase
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
# REST нужен только для числовых ID и реквизитов записи с известным GraphQL ID.
_MATCH_LIMIT = 10
# До проверки прав CI/VPS установленный snapshot пользуется REST-контрактом v1.
_DEFAULT_READER = "v1"
_ORDERED = """
    deployments(first:2, environments:["production"], orderBy:{field:CREATED_AT, direction:DESC}) {
      totalCount nodes { id createdAt latestStatus { id } }
    }"""
_LATEST = "query($owner:String!, $name:String!) { repository(owner:$owner,name:$name) {nameWithOwner " + _ORDERED + "} }"
_FOR_COMMIT = """query($owner:String!, $name:String!, $sha:GitObjectID!) {
  repository(owner:$owner,name:$name) { nameWithOwner object(oid:$sha) { ... on Commit { oid """ + _ORDERED + "} } } }"
_EXACT = """query($id:ID!) {
  node(id:$id) { ... on Deployment { id createdAt latestStatus { id } } }
}"""


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
    description: str | None = None


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


def _reader_v2() -> bool:
    """Проверяет выбранный доверенным процессом контракт чтения."""
    mode = os.environ.get("COMPADRE_DEPLOYMENT_READER", _DEFAULT_READER)
    if mode not in ("v1", "v2"):
        raise DeploymentAcceptanceError("Неизвестный режим читателя Deployment")
    return mode == "v2"


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
        created = _timestamp(candidate.get("created_at"))
        dated.append((created, status_id, candidate))
    newest = max(item[0] for item in dated)
    latest = [item for item in dated if item[0] == newest]
    # GitHub документирует текущий статус как самый новый, но не порядок списка.
    # При одинаковой точности timestamp невозможно доказать, какой статус текущий.
    if len(latest) != 1:
        raise DeploymentAcceptanceError("Неоднозначный текущий статус Deployment")
    return _parse_status(deployment_id, latest[0][2])


def _timestamp(value: object) -> datetime:
    """Проверяет время UTC, используемое GitHub для Deployments."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as error:
        raise DeploymentAcceptanceError("Некорректная дата Deployment или статуса") from error


def _parse_status(deployment_id: int, status: dict) -> DeploymentRecord:
    """Сверяет доверенного автора и распознаёт состояние текущего статуса."""
    if not isinstance(status, dict) or status.get("environment") != ENVIRONMENT:
        raise DeploymentAcceptanceError("Статус относится к другому окружению")
    _owner(status.get("creator"), "статуса Deployment")
    status_id = _positive(status.get("id"), "статуса Deployment")
    value = status.get("state")
    known = {item.value for item in DeploymentState if item not in (DeploymentState.MISSING, DeploymentState.UNKNOWN)}
    state = DeploymentState(value) if isinstance(value, str) and value in known else DeploymentState.UNKNOWN
    return DeploymentRecord(deployment_id, state, status_id, _timestamp(status.get("created_at")),
                            status.get("description"))


def _node_id(node: object, key: str = "id") -> str:
    """Требует непрозрачный глобальный ID и сохраняет его без декодирования."""
    if not isinstance(node, dict) or not isinstance(node.get(key), str) or not node[key]:
        raise DeploymentAcceptanceError("Отсутствует глобальный ID GitHub")
    return node[key]


def _match_node(api: GitHub, path: str, node: dict | None, **filters: str) -> dict | None:
    """Ищет известный глобальный ID в ограниченной REST-странице."""
    page = api.get(path, page=1, per_page=_MATCH_LIMIT, **filters)
    if not isinstance(page, list) or len(page) > _MATCH_LIMIT:
        raise DeploymentAcceptanceError("Некорректная страница Deployments")
    if node is None and not page:
        return None
    expected = _node_id(node)
    matches = [row for row in page if _node_id(row, "node_id") == expected]
    if len(matches) != 1:
        raise DeploymentAcceptanceError("REST не подтвердил единственную запись GraphQL")
    return matches[0]


def _graph_status(api: GitHub, raw: dict, node: dict) -> DeploymentRecord:
    """Связывает latestStatus GitHub с точным числовым ID REST."""
    deployment_id, _ = _deployment(api, raw)
    if (_node_id(node) != _node_id(raw, "node_id")
            or _timestamp(node.get("createdAt")) != _timestamp(raw.get("created_at"))
            or "latestStatus" not in node):
        raise DeploymentAcceptanceError("GraphQL не подтвердил реквизиты Deployment")
    status = _match_node(api, f"/deployments/{deployment_id}/statuses", node["latestStatus"])
    if status is None:
        return DeploymentRecord(deployment_id, DeploymentState.MISSING, None)
    return _parse_status(deployment_id, status)


def _latest_production(api: GitHub) -> tuple[dict, DeploymentRecord]:
    """Читает свежую попытку независимо от порядка строк REST."""
    pair = _latest_for(api)
    if pair is None:
        raise DeploymentAcceptanceError("Отсутствует принятая база production")
    return pair


def _latest_for(api: GitHub, sha: str | None = None) -> tuple[dict, DeploymentRecord] | None:
    """Читает свежую попытку всего окружения или точного коммита."""
    if api.repository != EXPECTED_REPOSITORY:
        raise DeploymentAcceptanceError("Репозиторий Deployment не совпадает с проектом")
    owner, name = api.repository.split("/")
    params = {"owner": owner, "name": name}
    if sha is not None:
        params["sha"] = sha
    repo = api.graphql(_LATEST if sha is None else _FOR_COMMIT, **params).get("repository")
    if not isinstance(repo, dict) or repo.get("nameWithOwner") != api.repository:
        raise DeploymentAcceptanceError("GraphQL не подтвердил репозиторий")
    source = repo if sha is None else repo.get("object")
    if not isinstance(source, dict) or (sha is not None and source.get("oid") != sha):
        raise DeploymentAcceptanceError("GraphQL не подтвердил коммит")
    connection = source.get("deployments")
    if not isinstance(connection, dict):
        raise DeploymentAcceptanceError("Отсутствует список production Deployment")
    nodes, count = connection.get("nodes"), connection.get("totalCount")
    if (type(count) is not int or count < 0 or not isinstance(nodes, list)
            or len(nodes) != min(2, count) or len({_node_id(node) for node in nodes}) != len(nodes)):
        raise DeploymentAcceptanceError("Неполная или пустая выборка production Deployment")
    if not nodes:
        return None
    node = nodes[0]
    if len(nodes) == 2 and _timestamp(node.get("createdAt")) <= _timestamp(nodes[1].get("createdAt")):
        raise DeploymentAcceptanceError("Неоднозначная последняя попытка production")
    filters = {"environment": ENVIRONMENT}
    if sha is not None:
        filters["sha"] = sha
    raw = _match_node(api, "/deployments", node, **filters)
    if sha is not None and raw.get("sha") != sha:
        raise DeploymentAcceptanceError("REST не подтвердил коммит попытки")
    return raw, _graph_status(api, raw, node)


def _exact_production(api: GitHub, deployment_id: int) -> tuple[dict, DeploymentRecord]:
    """Подтверждает Deployment по числовому ID и читает его актуальный статус."""
    raw = api.get(f"/deployments/{_positive(deployment_id, 'Deployment')}")
    actual_id, _ = _deployment(api, raw)
    if actual_id != deployment_id:
        raise DeploymentAcceptanceError("GitHub вернул другую запись приёмки")
    return raw, _exact_status(api, raw)


def _exact_status(api: GitHub, raw: dict) -> DeploymentRecord:
    """Читает текущий статус уже известной REST-записи без обхода истории."""
    node = api.graphql(_EXACT, id=_node_id(raw, "node_id")).get("node")
    return _graph_status(api, raw, node)


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


@dataclass(frozen=True)
class DeploymentAttempt:
    """Сохраняет неизменные реквизиты начала установки до появления результата."""

    schema_version: int
    attempt_key: str
    version: str
    sha: str
    kind: str
    release_id: int
    run_id: int
    run_attempt: int
    baseline: AcceptedBaseline

    def __post_init__(self) -> None:
        """Проверяет ключ исходного запуска, версию схемы и точные реквизиты CI."""
        if (type(self.schema_version) is not int or self.schema_version != 2
                or not isinstance(self.attempt_key, str) or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", self.attempt_key)
                or not isinstance(self.sha, str) or not SHA_PATTERN.fullmatch(self.sha)
                or not isinstance(self.version, str) or not VERSION_PATTERN.fullmatch(self.version)
                or self.version == LEGACY_INSTALLATION_VERSION or self.kind not in ("ordinary", "emergency")
                or not isinstance(self.baseline, AcceptedBaseline)):
            raise DeploymentAcceptanceError("Реквизиты попытки v2 не подтверждены")
        for value in (self.release_id, self.run_id, self.run_attempt):
            _positive(value, "Release или попытки CI")


def _attempt(raw: dict) -> DeploymentAttempt:
    """Строго проверяет payload v2 и прямую ссылку на принятую базу."""
    payload = raw.get("payload")
    try:
        fields = dict(payload)
        fields["baseline"] = AcceptedBaseline(**fields["baseline"])
        result = DeploymentAttempt(**fields)
    except (TypeError, KeyError, ValueError) as error:
        raise DeploymentAcceptanceError("Некорректный payload попытки v2") from error
    if not isinstance(payload, dict) or result.sha != raw["sha"] or result.baseline.deployment_id == raw["id"]:
        raise DeploymentAcceptanceError("Повреждена ссылка попытки v2")
    return result


def _v1(raw: dict) -> bool:
    """Разрешает старый разбор только для явной целочисленной версии 1."""
    payload = raw.get("payload")
    return isinstance(payload, dict) and type(payload.get("schema_version")) is int and payload["schema_version"] == 1


def _check_attempt_release(api: GitHub, attempt: DeploymentAttempt) -> None:
    """Связывает попытку с опубликованным SHA, Release, CI и базой обычного тега."""
    try:
        release, run, retry = _published_release(api, attempt.version, attempt.sha, attempt.kind, baseline=attempt.baseline)
    except (DeploySelectionError, InstallationTagError, PublishError, ValueError) as error:
        raise DeploymentAcceptanceError("Release попытки v2 не подтверждён") from error
    if release != attempt.release_id or (run is not None and (run, retry) != (attempt.run_id, attempt.run_attempt)):
        raise DeploymentAcceptanceError("Release или CI расходится с попыткой v2")


def _v2_receipt(api: GitHub, attempt: DeploymentAttempt, record: DeploymentRecord) -> InstallationReceipt:
    """Восстанавливает результат приёмки только из текущего доверенного success."""
    description = record.description
    if record.state is not DeploymentState.SUCCESS or not isinstance(description, str) or len(description) > 140:
        raise DeploymentAcceptanceError("Нет результата свежей приёмки v2")
    match = re.fullmatch(r"v1 gen=([0-9a-f]{32}) migration=([0-9]{4}_[a-z0-9_]+\.sql)", description)
    if match is None:
        raise DeploymentAcceptanceError("Некорректный результат приёмки v2")
    result = InstallationReceipt(1, attempt.version, attempt.sha, attempt.kind, attempt.release_id,
                                 attempt.run_id, attempt.run_attempt, match[1], match[2], "0" * 64)
    _schema_evidence(result)
    _check_attempt_release(api, attempt)
    if expected_migration_at(api, attempt.sha) != result.migration:
        raise DeploymentAcceptanceError("Миграция статуса расходится с коммитом")
    return result


def _selected(api: GitHub, raw: dict, record: DeploymentRecord) -> AcceptedBaseline:
    """Подтверждает success указанной записи без следования по ссылкам."""
    if _v1(raw):
        return _accepted_baseline(api, _baseline_claim(raw, raw["sha"]), record)
    receipt = _v2_receipt(api, _attempt(raw), record)
    return AcceptedBaseline(receipt.sha, receipt.version, receipt.release_id, record.deployment_id, record.status_id)


def baseline_receipt(api: GitHub, baseline: AcceptedBaseline) -> InstallationReceipt:
    """Читает поколение, миграцию и CI точной уже принятой установки.

    Args:
        api: Читающий GitHub-клиент репозитория.
        baseline: Сохранённая прямая ссылка на прежний success.

    Returns:
        Реквизиты установки с техническим заполнителем runtime_id.

    Raises:
        DeploymentAcceptanceError: Текущий статус или реквизиты базы изменились.
    """
    raw, status = _exact_production(api, baseline.deployment_id)
    if _v1(raw):
        receipt = _baseline_claim(raw, raw["sha"])
        actual = _accepted_baseline(api, receipt, status)
    else:
        receipt = _v2_receipt(api, _attempt(raw), status)
        actual = AcceptedBaseline(receipt.sha, receipt.version, receipt.release_id, status.deployment_id, status.status_id)
    if actual != baseline:
        raise DeploymentAcceptanceError("Точная принятая база изменилась")
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


@http_phase(HttpPhase.DEPLOYMENT_BASE)
def resolve_accepted_baseline(api: GitHub) -> AcceptedBaseline:
    """Выбирает принятую базу по контракту v1 или последней попытке v2.

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
    bounded = _reader_v2()
    if bounded:
        raw, record = _latest_production(api)
        if not _v1(raw):
            if record.state is not DeploymentState.FAILURE:
                return _selected(api, raw, record)
            attempt = _attempt(raw)
            base = attempt.baseline
            if record.description != f"v1 restored={base.deployment_id}/{base.status_id}":
                raise DeploymentAcceptanceError("Не подтверждён возврат на принятую базу")
            _check_attempt_release(api, attempt)
            previous, status = _exact_production(api, base.deployment_id)
            if (_timestamp(previous.get("created_at")) >= _timestamp(raw.get("created_at"))
                    or (not _v1(previous) and _attempt(previous).baseline.deployment_id == record.deployment_id)
                    or status.status_id != base.status_id or _selected(api, previous, status) != base):
                raise DeploymentAcceptanceError("Возврат не соответствует принятой базе")
            return base
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
        record = _exact_status(api, raw) if bounded else _current_status(api, deployment_id)
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


@http_phase(HttpPhase.DEPLOYMENT_BASE)
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
    if _reader_v2():
        raw, record = _exact_production(api, deployment_id)
        if record.status_id != status_id:
            raise DeploymentAcceptanceError("Текущий статус выбранной базы изменился")
        return _selected(api, raw, record)
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
    if _reader_v2():
        pair = _latest_for(api, receipt.sha)
        if pair is None:
            raise DeploymentAcceptanceError("Запись приёмки отсутствует")
        raw, record = pair
        if _v1(raw):
            observed = _baseline_claim(raw, receipt.sha)
            _expected_payload(api, observed)
        else:
            observed = _v2_receipt(api, _attempt(raw), record)
        if asdict(observed) | {"runtime_id": receipt.runtime_id} != asdict(receipt):
            raise DeploymentAcceptanceError("Текущая приёмка расходится с Deployment")
    else:
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
    if _reader_v2():
        pair = _latest_for(api, sha)
        if pair is None:
            return summary | {"deployment_id": None, "deployment_status_id": None, "deployment_state": "absent"}
        raw, record = pair
        if _v1(raw):
            record_claim = _baseline_claim(raw, sha)
            _expected_payload(api, record_claim)
        else:
            attempt = _attempt(raw)
            if ((attempt.version, attempt.sha, attempt.release_id) != (version, sha, release_id)
                    or (run_id is not None and (attempt.run_id, attempt.run_attempt) != (run_id, run_attempt))):
                raise DeploymentAcceptanceError("Попытка расходится с опубликованным Release")
            if record.state is not DeploymentState.SUCCESS:
                _check_attempt_release(api, attempt)
                return summary | {"deployment_id": record.deployment_id, "deployment_status_id": record.status_id,
                                  "deployment_state": record.state.value}
            record_claim = _v2_receipt(api, attempt, record)
    else:
        records = api.pages("/deployments", sha=sha, task=TASK, environment=ENVIRONMENT)
        if len(records) > 1 or (records and not isinstance(records[0], dict)):
            raise DeploymentAcceptanceError("Неоднозначная запись Deployments для сводки SQLite")
        if records:
            record_claim = _claimed_receipt(records[0])
            record = read_deployment_record(api, record_claim)
            if record is None:
                raise DeploymentAcceptanceError("Запись Deployments исчезла во время сверки")
        else:
            return summary | {"deployment_id": None, "deployment_status_id": None, "deployment_state": "absent"}
    if ((record_claim.version, record_claim.sha, record_claim.release_id, record_claim.migration)
            != (version, sha, release_id, expected_migration)
            or (run_id is not None and (record_claim.run_id, record_claim.run_attempt) != (run_id, run_attempt))):
        raise DeploymentAcceptanceError("Запись Deployments расходится с опубликованным Release")
    result = summary | {"deployment_id": record.deployment_id, "deployment_status_id": record.status_id,
                        "deployment_state": record.state.value}
    if record.state is not DeploymentState.SUCCESS:
        return result
    return result | {"installation_status": "accepted", "run_id": record_claim.run_id,
                     "run_attempt": record_claim.run_attempt, "generation_id": record_claim.generation_id,
                     "applied_migration": record_claim.migration}
