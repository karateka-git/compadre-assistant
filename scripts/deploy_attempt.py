"""Резервирует выпуск Deployment v2 между отдельными соединениями SSH."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
import re

from scripts import deploy_acceptance as acceptance
from scripts import deploy_cleanup_old as cleanup
from scripts import deploy_runtime as runtime
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
    if (set(value) - {"result", "installed_receipt"} != fields or type(value["protocol"]) is not int or value["protocol"] != 2
            or type(value["closed"]) is not bool or type(value["started"]) is not bool
            or not _KEY.fullmatch(value["owner"]) or not prep.SHA_PATTERN.fullmatch(value["snapshot"])
            or claims not in ([], ["create"], ["create", "in_progress"], ["create", "in_progress", "finish"])
            or deployment_id is not None and (type(deployment_id) is not int or deployment_id <= 0 or not claims)
            or (value["started"] or value["closed"] and claims) and deployment_id is None):
        raise prod.DeployError("Повреждён журнал владения Deployment")
    records._attempt({"id": deployment_id or 0, "sha": value["attempt"]["sha"], "payload": value["attempt"]})
    records._schema_evidence(InstallationReceipt(**value["base_receipt"]))
    if "result" in value:
        records._schema_evidence(InstallationReceipt(**value["result"]["receipt"]))
        if value["result"]["outcome"] not in ("success", "restored"):
            raise prod.DeployError("Повреждён результат установки")
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
            or type(run.get("id")) is not int or type(run.get("run_attempt")) is not int
            or (run.get("id"), run.get("run_attempt")) != (run_id, retry)
            or run.get("path") != ".github/workflows/production-release.yml" or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main" or run.get("status") != status):
        raise prod.DeployError("Не подтверждён владелец запуска production-release")


def _base(host, api, attempt):
    base = attempt.baseline
    expected = records.baseline_receipt(api, base)
    state = prep.read_state(host.state_dir, host.checkout) or {}
    if state.get("stage") == "rolled_back":
        fresh = _rolled_back(host, state, expected)
    else:
        fresh = InstallationReceipt(**acceptance._verify_locked(host, base.version, base.sha))
    if replace(fresh, runtime_id=expected.runtime_id) != expected:
        raise prod.DeployError("Фактическая приёмка VPS расходится с D17")
    return fresh


def _rolled_back(host, state, expected):
    """Подтверждает старый runtime и именно прежнее поколение SQLite после recovery."""
    acceptance._require_unblocked(host)
    good = state["last_good"]
    boot = runtime.verify(host, state, require_running=True)
    selected = host.select_emergency(expected.version) if expected.kind == "emergency" else host.select(expected.version)
    if (boot["outcome"] != "rolled_back" or state["admission_may_have_started"] is not False
            or (boot["sha"], boot["image_id"], boot["generation_id"]) !=
               (expected.sha, good["image_id"], expected.generation_id)
            or (good["version"], good["sha"], good["ci_run_id"], good["ci_run_attempt"]) !=
               (expected.version, expected.sha, expected.run_id, expected.run_attempt)
            or (selected.sha, selected.kind, selected.release_id, selected.run_id, selected.run_attempt) !=
               (expected.sha, expected.kind, expected.release_id, expected.run_id, expected.run_attempt)
            or state["old"]["manifest"][-1] != expected.migration
            or host.timer_snapshot() != state["timer_before"] or not host.unit_active(prod.SERVICE)):
        raise prod.DeployError("Возврат не подтвердил исходное поколение D17")
    prep.require_clean_checkout(host.checkout, expected.sha)
    prep.require_exact_tag(host.checkout, expected.version, expected.sha, tag_object_sha=selected.tag_object_sha)
    containers = acceptance._container_identity(host, good["image_id"])
    # Финализация уже подтвердила polling; при позднем resume его старт вышел из окна logs.
    if (runtime.verify(host, state, require_running=True) != boot
            or containers != acceptance._container_identity(host, good["image_id"])
            or state != prep.read_state(host.state_dir, host.checkout)):
        raise prod.DeployError("Runtime возврата изменился во время проверки")
    digest = hashlib.sha256(json.dumps([boot, containers], sort_keys=True).encode()).hexdigest()
    return replace(expected, runtime_id=digest)


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


def _complete(host, api, attempt, journal, status, *, recover):
    """Классифицирует durable этап; повторно проверяет результат перед закрытием."""
    state = prod._read_recovery_state(host)
    if not journal["started"]:
        raise prod.DeployError("Нет намерения установки")
    owned = state.get("deployment_attempt") == [attempt.attempt_key, journal["deployment_id"]]
    if state.get("stage") not in ("success", "rolled_back"):
        if not recover or not owned or state.get("stage") == "degraded":
            raise prod.DeployError("Состояние требует отдельной сверки recovery")
        candidate = state.get("candidate", {})
        old_preparation = (state.get("stage") == "preparing" and "old_generation_cleanup" in state
                           and state.get("last_good", {}).get("sha") == attempt.baseline.sha
                           and state.get("boot", {}).get("generation_id") == journal["base_receipt"]["generation_id"])
        if not old_preparation and (candidate.get("version"), candidate.get("sha"), candidate.get("release_id"),
                                    candidate.get("run_id"), candidate.get("run_attempt")) != (
                attempt.version, attempt.sha, attempt.release_id, attempt.run_id, attempt.run_attempt):
            raise prod.DeployError("Незавершённый state не принадлежит Release попытки")
        require_current(host)
        if state.get("stage") != "recovery_verified":
            prod._recover(host, state)
        if state.get("stage") == "recovery_verified":
            host.finalize_database(state, "rolled_back")
        state = prod._read_recovery_state(host)
    good = state.get("last_good") or {}
    if (good.get("version"), good.get("sha")) == (attempt.version, attempt.sha):
        if not owned or state.get("stage") != "success" or status.state is records.DeploymentState.FAILURE:
            raise prod.DeployError("Кандидат не подтверждён завершённым переходом этой попытки")
        previous = journal.get("installed_receipt")
        if previous is None:
            previous = acceptance._verify_locked(host, attempt.version, attempt.sha)
            journal["installed_receipt"] = previous
            _save(host, journal)
        if "old_generation_cleanup" not in state:
            if not recover or status.state is not records.DeploymentState.IN_PROGRESS:
                raise prod.DeployError("Отсутствует завершённая очистка")
            require_current(host)
            cleanup.cleanup_old(host, attempt.version, attempt.sha, previous["runtime_id"])
        state = prod._read_recovery_state(host)
        receipt = acceptance._verify_locked(host, attempt.version, attempt.sha)
        if (receipt != previous or state["old_generation_cleanup"] != prod.database_switch.old_cleanup_record(state)
                or state["candidate"]["sha"] != attempt.sha):
            raise prod.DeployError("Очистка не подтверждена для исходного runtime")
        cleanup._snapshot(host, state)
        outcome = "success"
    else:
        if status.state is records.DeploymentState.SUCCESS:
            raise prod.DeployError("Фактический runtime противоречит success")
        receipt = asdict(_base(host, api, attempt))
        if not owned and receipt != journal["base_receipt"]:
            raise prod.DeployError("Исходный runtime изменился вне попытки")
        outcome = "restored"
    result = {"outcome": outcome, "receipt": receipt}
    if journal.get("result", result) != result:
        raise prod.DeployError("Итоговый runtime изменился")
    if "result" not in journal:
        journal["result"] = result
        _save(host, journal)
    return result


def require_current(host):
    """Под удерживаемым lock повторно подтверждает GitHub D18 перед переключением."""
    ownership = getattr(host, "locked_attempt", None)
    if ownership is not None:
        attempt, deployment_id = ownership
        status = writer.current_attempt(host.release_api(), attempt, deployment_id)
        if (status.state is not records.DeploymentState.IN_PROGRESS
                or status.description != f"v1 attempt={attempt.attempt_key}"):
            raise prod.DeployError("D18 больше не разрешает установку")


def reconcile_post(host, version: str, sha: str, key: str, snapshot: str, action: str) -> None:
    """После операторской сверки снимает конкретное неисполненное намерение.

    Args:
        host: Контроллер, запущенный владельцем VPS; ключ runner сюда не допускается.
        version: Версия спорной попытки.
        sha: Полный SHA кандидата.
        key: Исходный ключ попытки.
        snapshot: Ожидаемый SHA установленного контроллера.
        action: Подтверждённо отклонённый POST или cancel до выдачи права create.

    Raises:
        DeployError: Процесс-владелец, GitHub или VPS не подтверждают границу.
    """
    with prep.deploy_lock(host.state_dir, host.checkout):
        journal = _read(host)
        if (not journal or journal["closed"] or not records._reader_v2()
                or journal["snapshot"] != snapshot or host.controller_sha() != snapshot):
            raise prod.DeployError("Не подтверждена открытая попытка этого snapshot")
        attempt = records._attempt({"id": 0, "sha": sha, "payload": journal["attempt"]})
        if (attempt.version, attempt.attempt_key) != (version, key):
            raise prod.DeployError("Реквизиты ручной сверки не совпадают")
        api = host.release_api()
        _workflow(api, journal["owner"], "completed")
        claims = journal["claims"]
        if action == "cancel":
            if claims or journal["started"] or journal["deployment_id"] is not None:
                raise prod.DeployError("Отмена возможна только до выдачи права POST")
        elif action not in ("create", "in_progress", "finish") or not claims or claims[-1] != action:
            raise prod.DeployError("Последнее сохранённое намерение не совпало")
        if action in ("create", "cancel"):
            raw, _ = records._latest_production(api)
            if (journal["deployment_id"] is not None or journal["started"]
                    or not records._v1(raw) and records._attempt(raw).attempt_key == key
                    or records.resolve_accepted_baseline(api) != attempt.baseline):
                raise prod.DeployError("Отсутствие D18 и исходная база не подтверждены")
            if asdict(_base(host, api, attempt)) != journal["base_receipt"]:
                raise prod.DeployError("Изменился исходный runtime")
        else:
            status = writer.current_attempt(api, attempt, journal["deployment_id"])
            expected = records.DeploymentState.MISSING if action == "in_progress" else records.DeploymentState.IN_PROGRESS
            if (status.state is not expected or action == "in_progress" and journal["started"]
                    or action == "finish" and status.description != f"v1 attempt={key}"):
                raise prod.DeployError("Текущий статус запрещает снятие намерения")
        if action == "cancel":
            journal["closed"] = True
        else:
            claims.pop()
        _save(host, journal)


def recover_forward(host, version: str, sha: str, key: str, snapshot: str) -> None:
    """Продолжает подтверждённый forward recovery владельцем VPS под lock D18.

    Args:
        host: Установленный контроллер с его приватным состоянием.
        version: Версия незакрытой попытки.
        sha: SHA кандидата.
        key: Исходный ключ установки.
        snapshot: Ожидаемый SHA установленного controller snapshot.

    Raises:
        DeployError: D18, runtime или условия штатного forward recovery не подтверждены.
    """
    from scripts import deploy_forward_recovery

    with prep.deploy_lock(host.state_dir, host.checkout):
        journal = _read(host)
        if (not journal or journal["closed"] or not journal["started"] or not records._reader_v2()
                or journal["snapshot"] != snapshot or host.controller_sha() != snapshot):
            raise prod.DeployError("Открытая установка не подтверждена")
        attempt = records._attempt({"id": 0, "sha": sha, "payload": journal["attempt"]})
        state = prod._read_recovery_state(host)
        number = journal["deployment_id"]
        if ((attempt.version, attempt.attempt_key) != (version, key)
                or state.get("deployment_attempt") != [key, number]
                or (state.get("candidate") or {}).get("sha") != sha):
            raise prod.DeployError("Forward recovery не подтвердил точную попытку")
        _workflow(host.release_api(), journal["owner"], "completed")
        host.locked_attempt = (attempt, number)
        try:
            require_current(host)
            deploy_forward_recovery.recover(host)
        finally:
            del host.locked_attempt


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
    if (action not in {"reserve", "resume", "create", "in_progress", "finish", "install", "base", "complete", "close"}
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
        if not journal or journal["closed"] and action not in {"resume", "complete", "close"} or journal["snapshot"] != snapshot:
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
            status = writer.current_attempt(api, attempt, deployment_id)
            if bound is None:
                if "create" not in journal["claims"]:
                    raise prod.DeployError("Не было намерения создать этот D18")
                journal["deployment_id"] = deployment_id
                _save(host, journal)
        elif deployment_id != (bound or 0) and (action not in {"resume", "reserve"} or deployment_id != 0):
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
            elif action == "finish" and "result" not in journal:
                raise prod.DeployError("VPS ещё не подтвердил итоговый runtime")
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
            if action in {"complete", "close"}:
                result = _complete(host, api, attempt, journal, status, recover=action == "complete" and not journal["closed"])
                if action == "close":
                    base = attempt.baseline
                    receipt = result["receipt"]
                    success = result["outcome"] == "success"
                    expected = ((records.DeploymentState.SUCCESS, writer._description(receipt["generation_id"], receipt["migration"]))
                                if success else (records.DeploymentState.FAILURE, f"v1 restored={base.deployment_id}/{base.status_id}"))
                    if (status.state, status.description) != expected or "finish" not in journal["claims"]:
                        raise prod.DeployError("GitHub не подтвердил статус проверенного runtime")
                    journal["closed"] = True
                    _save(host, journal)
                return result
            require_current(host)
            if ("in_progress" not in journal["claims"] or journal["started"]
                    or asdict(_base(host, api, attempt)) != journal["base_receipt"]):
                raise prod.DeployError("Установка уже начата или runtime изменился")
            journal["started"] = True
            _save(host, journal)
            if prod.deploy(host, version) != sha:
                raise prod.DeployError("Не подтверждена установка кандидата")
            return {"installed": sha}
        finally:
            del host.locked_attempt
