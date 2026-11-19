"""Резервирует выпуск Deployment v2 между отдельными соединениями SSH."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
import re

from scripts import deploy_acceptance as acceptance
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deployment_acceptance as records
from scripts import deployment_writer as writer
from scripts import predeploy_compatibility as compatibility
from scripts.probe_deployment_reader import compare_readers
from scripts.publish_installation_tag import InstallationReceipt

# Все процессы проверяют этот файл под одним deploy.lock; срок действия отсутствует.
JOURNAL = "deployment-attempt.json"
_KEY = re.compile(r"[1-9][0-9]{0,19}:[1-9][0-9]{0,9}")


def _save(host, journal):
    prod._atomic_text(host.state_dir / JOURNAL, json.dumps(journal, separators=(",", ":")) + "\n")


def _read(host):
    path = host.state_dir / JOURNAL
    if not os.path.lexists(path):
        return None
    value = prep.parse_state_object(compatibility.private_text(path))
    fields = {"protocol", "closed", "attempt", "deployment_id", "claims", "owner", "snapshot", "started", "base_receipt"}
    claims = value.get("claims")
    deployment_id = value.get("deployment_id")
    if (set(value) != fields or type(value["protocol"]) is not int or value["protocol"] != 2
            or type(value["closed"]) is not bool or type(value["started"]) is not bool
            or not _KEY.fullmatch(value["owner"]) or not prep.SHA_PATTERN.fullmatch(value["snapshot"])
            or claims not in ([], ["create"], ["create", "in_progress"], ["create", "in_progress", "finish"])
            or deployment_id is not None and (type(deployment_id) is not int or deployment_id <= 0 or not claims)
            or (value["closed"] or value["started"]) and deployment_id is None):
        raise prod.DeployError("Повреждён журнал владения Deployment")
    records._attempt({"id": deployment_id or 0, "sha": value["attempt"]["sha"], "payload": value["attempt"]})
    records._schema_evidence(InstallationReceipt(**value["base_receipt"]))
    return value


@contextmanager
def mutation_lock(host):
    """Занимает lock для прежнего входа либо использует lock команды попытки.

    Args:
        host: Объект контроллера внутри текущего процесса.

    Yields:
        Управление операции, имеющей право изменять установленный runtime.

    Raises:
        DeployError: Продолжается выпуск с другим владельцем.
    """
    if getattr(host, "locked_attempt", None) is not None:
        yield
        return
    with prep.deploy_lock(host.state_dir, host.checkout):
        require_unreserved(host)
        yield


def require_unreserved(host):
    """Проверяет под уже занятым lock, что прежний вход не обходит владельца D18."""
    journal = _read(host)
    if journal is not None and not journal["closed"]:
        raise prod.DeployError("Установкой владеет открытая попытка Deployment")


def _workflow(api, key, status):
    run_id, retry = map(int, key.split(":"))
    run = api.get(f"/actions/runs/{run_id}/attempts/{retry}")
    if (not isinstance(run, dict) or (run.get("repository") or {}).get("full_name") != api.repository
            or (run.get("head_repository") or {}).get("full_name") != api.repository
            or (run.get("id"), run.get("run_attempt")) != (run_id, retry)
            or run.get("path") != ".github/workflows/production-release.yml" or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main" or run.get("status") != status):
        raise prod.DeployError("Не подтверждён владелец запуска production-release")


def _base(host, api, attempt):
    base = attempt.baseline
    fresh = InstallationReceipt(**acceptance._verify_locked(host, base.version, base.sha))
    expected = records.baseline_receipt(api, base)
    if replace(fresh, runtime_id=expected.runtime_id) != expected:
        raise prod.DeployError("Фактическая приёмка VPS расходится с D17")
    return fresh


def _reserve(host, api, version, sha, key, snapshot):
    selected = host.select(version)
    base = getattr(selected.compatibility, "baseline", None)
    if selected.sha != sha or selected.kind != "ordinary" or not isinstance(base, records.AcceptedBaseline):
        raise prod.DeployError("Release и аннотация не подтвердили обычный выпуск и D17")
    raw, _ = records._latest_production(api)
    actual = compare_readers(api) if records._v1(raw) else records.resolve_accepted_baseline(api)
    if base != actual:
        raise prod.DeployError("Тег не подтвердил текущую принятую базу")
    attempt = records.DeploymentAttempt(2, key, version, sha, selected.kind, selected.release_id,
                                        selected.run_id, selected.run_attempt, base)
    records._check_attempt_release(api, attempt)
    if prod._version(version) <= prod._version(base.version) or sha == base.sha:
        raise prod.DeployError("Нет допуска к повторной установке или понижению версии")
    fresh = _base(host, api, attempt)
    journal = {"protocol": 2, "attempt": asdict(attempt), "owner": key, "snapshot": snapshot,
               "base_receipt": asdict(fresh), "claims": [], "deployment_id": None,
               "started": False, "closed": False}
    _save(host, journal)
    return journal


def require_current(host):
    """Под удерживаемым lock повторно подтверждает GitHub D18 перед переключением."""
    ownership = getattr(host, "locked_attempt", None)
    if ownership is not None:
        attempt, deployment_id = ownership
        status = writer.current_attempt(host.release_api(), attempt, deployment_id)
        if (status.state is not records.DeploymentState.IN_PROGRESS
                or status.description != f"v1 attempt={attempt.attempt_key}"):
            raise prod.DeployError("D18 больше не разрешает установку")


def execute(host, version: str, action: str, sha: str, key: str, owner: str, snapshot: str, number: str) -> dict:
    """Выполняет одну команду ограниченного SSH-контракта под deploy.lock.

    Args:
        host: Доверенный системный адаптер.
        version: Каноническая версия обычного Release.
        action: Одна из зарегистрированных команд попытки.
        sha: Полный SHA кандидата.
        key: Неизменный ключ изначального запуска.
        owner: Ключ исполняющего команду запуска production-release.
        snapshot: Ожидаемый SHA отдельно установленного snapshot.
        number: Числовой ID D18, до создания передаётся 0.

    Returns:
        Только реквизиты попытки или результат запрошенного действия.

    Raises:
        DeployError: Проверка владельца, этапа, базы или snapshot завершилась отказом.
    """
    prod._version(version)
    if (action not in {"reserve", "resume", "create", "in_progress", "finish", "install", "base"}
            or not _KEY.fullmatch(key) or not _KEY.fullmatch(owner)
            or not prep.SHA_PATTERN.fullmatch(sha) or not prep.SHA_PATTERN.fullmatch(snapshot)
            or not re.fullmatch(r"0|[1-9][0-9]{0,19}", number)):
        raise prod.DeployError("Некорректные аргументы команды Deployment")
    with prep.deploy_lock(host.state_dir, host.checkout):
        if host.controller_sha() != snapshot or not records._reader_v2():
            raise prod.DeployError("Установленный controller snapshot не готов к Deployment v2")
        api = host.release_api()
        _workflow(api, owner, "in_progress")
        journal = _read(host)
        if action == "reserve" and owner == key and number == "0":
            if journal is None or journal["closed"] and journal["attempt"]["attempt_key"] != key:
                journal = _reserve(host, api, version, sha, key, snapshot)
        if not journal or journal["closed"] or journal["snapshot"] != snapshot:
            raise prod.DeployError("Открытая резервация этого snapshot отсутствует")
        attempt = records._attempt({"id": 0, "sha": sha, "payload": journal["attempt"]})
        if (attempt.version, attempt.sha, attempt.attempt_key) != (version, sha, key):
            raise prod.DeployError("Установкой владеет другая попытка")
        if owner != journal["owner"]:
            if action != "resume":
                raise prod.DeployError("Устаревший runner не владеет попыткой")
            _workflow(api, journal["owner"], "completed")
            journal["owner"] = owner
            _save(host, journal)
        deployment_id = int(number)
        bound = journal["deployment_id"]
        if deployment_id and (bound is None or bound == deployment_id):
            writer.current_attempt(api, attempt, deployment_id)
            if bound is None:
                if "create" not in journal["claims"]:
                    raise prod.DeployError("Не было намерения создать этот D18")
                journal["deployment_id"] = deployment_id
                _save(host, journal)
        elif deployment_id != (bound or 0) and (action != "resume" or deployment_id != 0):
            raise prod.DeployError("Изменился ID Deployment")
        if action in {"reserve", "resume"}:
            return {"attempt": asdict(attempt), "deployment_id": journal["deployment_id"], "started": journal["started"]}
        if action in {"create", "in_progress", "finish"}:
            if (action == "create") != (deployment_id == 0):
                raise prod.DeployError("Неподходящий ID для записи")
            if action in journal["claims"]:
                return {"granted": False}
            if action == "create":
                raw, _ = records._latest_production(api)
                base = compare_readers(api) if records._v1(raw) else records.resolve_accepted_baseline(api)
                if base != attempt.baseline:
                    raise prod.DeployError("База изменилась перед созданием D18")
            elif action == "finish" and "in_progress" not in journal["claims"]:
                raise prod.DeployError("Отсутствует выданное право in_progress")
            if action in {"create", "in_progress"}:
                if asdict(_base(host, api, attempt)) != journal["base_receipt"]:
                    raise prod.DeployError("Изменился runtime принятой базы")
            journal["claims"].append(action)
            _save(host, journal)
            return {"granted": True}
        if not deployment_id:
            raise prod.DeployError("Требуется точный D18")
        if action == "base":
            return asdict(_base(host, api, attempt))
        host.locked_attempt = (attempt, deployment_id)
        try:
            require_current(host)
            if journal["started"] or asdict(_base(host, api, attempt)) != journal["base_receipt"]:
                raise prod.DeployError("Установка уже начата или runtime изменился")
            journal["started"] = True
            _save(host, journal)
            if prod.deploy(host, version) != sha:
                raise prod.DeployError("Не подтверждена установка кандидата")
            return {"installed": sha}
        finally:
            del host.locked_attempt
