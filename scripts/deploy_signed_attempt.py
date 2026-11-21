"""Ведёт подписанную попытку PATCH под блокировкой установленного контроллера."""

from dataclasses import asdict
import re

from scripts import deploy_attempt as attempts
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deployment_acceptance as records
from scripts import predeploy_compatibility as compatibility
from scripts.ci_repository_compatibility import DeploymentReleaseTagEvidence
from scripts.patch_admission import SSH_INPUT_LIMIT, read_signed_admission
from scripts.publish_installation_tag import InstallationReceipt
from scripts.publish_signed_patch import _select
from scripts.select_deploy_release import SelectedRelease, _release, _tag


def _packet(data):
    if not data or len(data) > SSH_INPUT_LIMIT:
        raise prod.DeployError("Требуется ограниченный пакет резервации")
    packet = prep.parse_state_object(data.decode("ascii"))
    if (packet.keys() != {"admission", "release_id", "tag_object_sha"}
            or not isinstance(packet["admission"], str) or not packet["admission"].isascii()
            or type(packet["release_id"]) is not int or packet["release_id"] <= 0
            or not isinstance(packet["tag_object_sha"], str)
            or not prep.SHA_PATTERN.fullmatch(packet["tag_object_sha"])):
        raise prod.DeployError("Некорректные реквизиты резервации PATCH")
    return packet


def _admitted(host, packet, version, sha):
    keys = host.state_dir / "patch-allowed-signers"
    compatibility.private_text(keys)
    proof = read_signed_admission(packet["admission"].encode("ascii"), keys)
    policy = compatibility.private_text(host.state_dir / "compatibility-policy.sha")
    if (proof.version, proof.candidate_sha, proof.policy_sha) != (version, sha, policy):
        raise prod.DeployError("Подписанный PATCH не соответствует версии, SHA или политике VPS")
    return proof


def _selection(proof, packet):
    evidence = DeploymentReleaseTagEvidence(proof.baseline.sha, proof.policy_sha, proof.run_id,
                                           proof.run_attempt, proof.schema_fingerprint, proof.baseline)
    return SelectedRelease(proof.repository, proof.version, proof.branch, proof.candidate_sha,
                           packet["release_id"], proof.workflow, proof.run_id, proof.run_attempt,
                           tag_object_sha=packet["tag_object_sha"], compatibility=evidence)


def _baseline(host, api, previous, expected):
    # Закрытая подписанная попытка закрепляет ID текущего статуса и квитанцию.
    # Первый запуск после прежнего контроллера один раз сверяет старую историю.
    if previous and previous.get("patch", {}).get("accepted"):
        saved = previous["patch"]["accepted"]
        if records.AcceptedBaseline(**saved["baseline"]) != expected:
            raise prod.DeployError("Допуск получен для другой принятой установки")
        return InstallationReceipt(**saved["receipt"])
    return attempts._current_base(api, expected)


def _reserve(host, api, previous, packet, proof, owner, snapshot):
    selected = _selection(proof, packet)
    raw = _release(api, proof.version)
    sha, tag, evidence = _tag(api, proof.version, allow_annotation=True)
    if ((sha, tag, evidence) != (proof.candidate_sha, selected.tag_object_sha, selected.compatibility)
            or _select(proof, raw, tag, evidence) != selected):
        raise prod.DeployError("Опубликованный Release или тег расходится с допуском")
    base = proof.baseline
    if prod._version(proof.version) <= prod._version(base.version) or sha == base.sha:
        raise prod.DeployError("Повторная установка и понижение PATCH запрещены")
    receipt = attempts._base(host, _baseline(host, api, previous, base))
    attempt = records.DeploymentAttempt(2, owner, proof.version, sha, "ordinary", selected.release_id,
                                        proof.run_id, proof.run_attempt, base)
    journal = {"protocol": 2, "closed": False, "started": False, "deployment_id": None, "claims": [],
               "attempt": asdict(attempt), "base_receipt": asdict(receipt), "owner": owner, "snapshot": snapshot,
               "patch": {"packet": packet, "in_progress": None, "terminal": None}}
    attempts._save(host, journal)
    return journal


def _bind(journal, number, action):
    if number <= 0 or "create" not in journal["claims"] or (
            journal["deployment_id"] != number and not (journal["deployment_id"] is None and action == "in_progress")):
        raise prod.DeployError("ID Deployment не соответствует резервации")
    journal["deployment_id"] = number


def _record(attempt, journal, status_id, action):
    patch = journal["patch"]
    if status_id <= 0 or "in_progress" not in journal["claims"]:
        raise prod.DeployError("Отсутствует подтверждение in_progress")
    if status_id == patch["in_progress"] or patch["in_progress"] is None and action == "install":
        if action == "close":
            raise prod.DeployError("Закрытие требует подтверждённого терминального статуса")
        patch["in_progress"] = status_id
        return records.DeploymentRecord(journal["deployment_id"], records.DeploymentState.IN_PROGRESS,
                                        status_id, description=f"v1 attempt={attempt.attempt_key}")
    if (action not in {"complete", "close"} or "finish" not in journal["claims"] or "result" not in journal
            or patch["terminal"] not in (None, status_id)):
        raise prod.DeployError("ID статуса расходится с журналом")
    outcome = journal["result"]["outcome"]
    state = records.DeploymentState.SUCCESS if outcome == "success" else records.DeploymentState.FAILURE
    return records.DeploymentRecord(journal["deployment_id"], state, status_id)


def _transition(host, journal, attempt, action, number, status_id):
    claims = journal["claims"]
    base_receipt = InstallationReceipt(**journal["base_receipt"])
    if action in {"create", "in_progress"}:
        if status_id or (action == "create") != (number == 0):
            raise prod.DeployError("Некорректные ID для выдачи намерения")
        if action == "in_progress":
            _bind(journal, number, action)
        if action in claims:
            return {"granted": False}
        if claims != ([] if action == "create" else ["create"]):
            raise prod.DeployError("Нарушен порядок создания Deployment")
        receipt = asdict(attempts._base(host, base_receipt))
        if receipt != journal["base_receipt"]:
            raise prod.DeployError("Изменился runtime принятой базы")
        claims.append(action)
        attempts._save(host, journal)
        return {"granted": True, **({"receipt": receipt} if action == "in_progress" else {})}
    _bind(journal, number, action)
    record = _record(attempt, journal, status_id, action)
    if action == "install":
        if journal["started"] or asdict(attempts._base(host, base_receipt)) != journal["base_receipt"]:
            raise prod.DeployError("Установка уже начата или исходный runtime изменился")
        journal["started"] = True
        attempts._save(host, journal)
        if prod.deploy(host, attempt.version) != attempt.sha:
            raise prod.DeployError("Установка не подтвердила SHA")
        return {"installed": attempt.sha}
    if action == "finish" and "result" not in journal:
        raise prod.DeployError("Отсутствует подтверждение завершения установки")
    result = attempts._complete(host, attempt, journal, record, base_receipt,
                                recover=action == "complete" and not journal["closed"])
    if action == "finish":
        if "finish" in claims:
            return {"granted": False}
        claims.append(action)
        attempts._save(host, journal)
        return {"granted": True, "receipt": result["receipt"]}
    if action == "close":
        baseline = attempt.baseline
        if result["outcome"] == "success":
            baseline = records.AcceptedBaseline(attempt.sha, attempt.version, attempt.release_id, number, status_id)
        journal["patch"].update(terminal=status_id, accepted={"baseline": asdict(baseline), "receipt": result["receipt"]})
        journal["closed"] = True
        attempts._save(host, journal)
    return result


def execute(host, version, action, sha, key, owner, snapshot, number, status_id, data):
    """Обрабатывает одну команду подписанного PATCH в едином журнале попыток.

    Args:
        host: Контроллер с приватным состоянием вне checkout.
        version: Выпускаемая версия.
        action: Запрошенный переход.
        sha: SHA кандидата.
        key: Ключ исходной попытки.
        owner: Исполняющая командами попытка Actions.
        snapshot: SHA установленного контроллера.
        number: ID Deployment или ноль до привязки.
        status_id: ID статуса либо ноль до его создания.
        data: Пакет подписанта при reserve и resume.

    Returns:
        Квитанция текущего перехода.

    Raises:
        prod.DeployError: Выпуск, подпись, владелец либо состояние расходятся.
        ValueError: Получены повреждённые данные.
    """
    prod._version(version)
    reservation = action in ("reserve", "resume")
    if (not reservation and action not in ("create", "in_progress", "install", "complete", "finish", "close")
            or not all(attempts._KEY.fullmatch(i) for i in (owner, key))
            or not all(prep.SHA_PATTERN.fullmatch(i) for i in (sha, snapshot))
            or not all(re.fullmatch(r"0|[1-9][0-9]{0,19}", i) for i in (number, status_id))
            or reservation and (number, status_id) != ("0", "0") or bool(data) != reservation
            or action == "reserve" and owner != key):
        raise prod.DeployError("Некорректная команда подписанного PATCH")
    with prep.deploy_lock(host.state_dir, host.checkout):
        if host.controller_sha() != snapshot or not records._reader_v2():
            raise prod.DeployError("Установленный snapshot не подтвердил поддержку PATCH")
        journal = attempts._read(host)
        api = host.release_api()
        if reservation:
            packet = _packet(data)
            proof = _admitted(host, packet, version, sha)
            attempts._workflow(api, owner, "in_progress")
            if action == "reserve" and (not journal or journal["closed"] and journal["attempt"]["attempt_key"] != key):
                journal = _reserve(host, api, journal, packet, proof, owner, snapshot)
        if (not journal or "patch" not in journal or journal["snapshot"] != snapshot
                or journal["closed"] and action not in ("resume", "complete", "close")):
            raise prod.DeployError("Отсутствует доступная резервация подписанного PATCH")
        attempt = records._attempt({"id": 0, "sha": sha, "payload": journal["attempt"]})
        if (attempt.version, attempt.sha, attempt.attempt_key) != (version, sha, key):
            raise prod.DeployError("Установкой владеет другая попытка")
        saved = journal["patch"]["packet"]
        if reservation:
            if saved != packet:
                raise prod.DeployError("Пакет расходится с сохранённой резервацией")
        else:
            proof = _admitted(host, saved, version, sha)
        if journal["owner"] != owner:
            if action != "resume":
                raise prod.DeployError("Команда от прежнего владельца")
            attempts._workflow(api, journal["owner"], "completed")
            journal["owner"] = owner
            attempts._save(host, journal)
        if reservation:
            return {"attempt": asdict(attempt), "deployment_id": journal["deployment_id"], "started": journal["started"]}
        host.patch_selection = _selection(proof, saved)
        host.patch_owner, host.patch_proof = owner, proof
        host.locked_attempt = (attempt, int(number))
        try:
            return _transition(host, journal, attempt, action, int(number), int(status_id))
        finally:
            del host.locked_attempt, host.patch_owner, host.patch_selection, host.patch_proof
