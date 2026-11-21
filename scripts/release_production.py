"""Устанавливает обычный выпуск, очищает прежнюю SQLite и записывает приёмку."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote

from scripts import deploy_metrics as metrics
from scripts import publish_deployment_acceptance as deployments
from scripts import publish_release as releases
from scripts import publish_signed_patch as signed_patch
from scripts import deployment_writer as writer
from scripts import deployment_acceptance as records
from scripts.check_hotfix_pairs import HotfixError
from scripts.create_release_branch import EXPECTED_REPOSITORY, ReleaseBranchError, repository_from_origin
from scripts.deploy_database_switch import OLD_CLEANUP_VERSION
from scripts.deploy_prepare import PreparationError, parse_state_object
from scripts.deployment_acceptance import DeploymentAcceptanceError, DeploymentRecord
from scripts.full_ci_admission import AdmissionError, SHA_PATTERN
from scripts.full_ci_controller import ControllerError, HttpLocation, HttpPhase, http_phase, immutable_git_reads
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.patch_admission import SSH_INPUT_LIMIT
from scripts.publish_installation_tag import InstallationReceipt, InstallationTagError, _receipt, run_vps_command
from scripts.select_deploy_release import DeploySelectionError, SelectedRelease, _release, select_release


# Операторскому SSH-входу нужен только установленный root-контроллер.
_CONTROLLER = ("sudo", "-n", "/usr/local/sbin/deploy-prod")
_GENERATION = re.compile(r"[0-9a-f]{32}")


class ProductionReleaseError(RuntimeError):
    """Означает, что цепочка выпуска остановилась на одной из проверок."""


def _select_or_publish(api: PublishingGitHub, version: str, sha: str, *,
                       change: str | None, notes: str | None) -> SelectedRelease:
    """Публикует только отсутствующий Release и перечитывает его допуск."""
    if change is not None:
        try:
            with http_phase(HttpPhase.RELEASE_LOOKUP):
                api.get(f"/releases/tags/{quote(version, safe='')}")
        except ControllerError as error:
            if not isinstance(error.__cause__, HTTPError) or error.__cause__.code != 404:
                raise
            # prepare повторно проверит отсутствие версии и тега перед любой записью.
            assert notes is not None
            with http_phase(HttpPhase.RELEASE_PREPARE):
                prepared = releases.prepare(api, version, sha, change, notes, github_history=change == "patch")
            print("Публикация GitHub Release", flush=True)
            try:
                with http_phase(HttpPhase.RELEASE_PUBLISH):
                    releases.publish(api, prepared)
            except (releases.PublishError, PublishingError, ControllerError, HotfixError) as error:
                raise ProductionReleaseError(
                    f"Публикация Release не подтверждена. Проверьте тег и Release перед повтором. Причина: {error}"
                ) from error
    with http_phase(HttpPhase.RELEASE_SELECT):
        selected = select_release(api, version)
    if selected.sha != sha:
        raise ProductionReleaseError("Опубликованный Release относится к другому SHA")
    return selected


def _require_cleanup(output: str, receipt: InstallationReceipt) -> None:
    """Подтверждает JSON завершения только для принятого поколения SQLite."""
    if len(output) > 4096 or len(output.splitlines()) != 1:
        raise ProductionReleaseError("VPS не вернул одну запись завершения очистки")
    try:
        record = parse_state_object(output)
    except (PreparationError, ValueError) as error:
        raise ProductionReleaseError("Некорректная запись завершения очистки VPS") from error
    fields = {"version", "transition_id", "old_generation_id", "old_host_path",
              "old_device", "old_inode", "new_generation_id"}
    old = record.get("old_generation_id")
    path = record.get("old_host_path")
    if (record.keys() != fields or type(record["version"]) is not int or record["version"] != OLD_CLEANUP_VERSION
            or not isinstance(record["transition_id"], str) or not _GENERATION.fullmatch(record["transition_id"])
            or record["new_generation_id"] != receipt.generation_id
            or not isinstance(old, str) or (old != "legacy" and not _GENERATION.fullmatch(old))
            or old == receipt.generation_id or not isinstance(path, str)
            or not PurePosixPath(path).is_absolute()
            or PurePosixPath(path).name != ("app.db" if old == "legacy" else f"app-{old}.db")
            or type(record["old_device"]) is not int or record["old_device"] < 0
            or type(record["old_inode"]) is not int or record["old_inode"] <= 0):
        raise ProductionReleaseError("Очистка VPS не подтверждена для принятого поколения SQLite")


def _v2_route(resume):
    """Проверяет включение писателя и исходную попытку до любой публикации."""
    mode = os.environ.get("COMPADRE_DEPLOYMENT_WRITER", "v1")
    source = os.environ.get("RELEASE_SOURCE_ATTEMPT", "")
    if mode == "v1" and not source:
        return None
    if mode not in ("v2", "resume-only") or mode == "resume-only" and not resume or not records._reader_v2():
        raise ProductionReleaseError("Настройки писателя не разрешают эту команду")
    owner = f"{os.environ.get('GITHUB_RUN_ID', '')}:{os.environ.get('GITHUB_RUN_ATTEMPT', '')}"
    key = source if resume else owner
    snapshot = os.environ.get("COMPADRE_CONTROLLER_SHA", "")
    if (not all(re.fullmatch(r"[1-9][0-9]{0,19}:[1-9][0-9]{0,9}", item) for item in (owner, key))
            or bool(source) != resume or not SHA_PATTERN.fullmatch(snapshot)):
        raise ProductionReleaseError("Требуются ключ исходной попытки и проверенный snapshot")
    return key, owner, snapshot


def _run_attempt(api, selected, route, resume, remote, *, signed=None):
    """Завершает одну установку по исходному ключу и перечитывает её итог."""
    key, owner, snapshot = route
    deployment_id = 0
    status_id = 0

    def call(action):
        arguments = [action, selected.sha, key, owner, snapshot, str(deployment_id)]
        options = {}
        option = "--attempt"
        if signed is not None:
            option = "--patch-attempt"
            arguments.append(str(status_id))
            if action in ("reserve", "resume"):
                options["input_text"] = json.dumps({
                    "admission": signed[1].decode("ascii"), "release_id": selected.release_id,
                    "tag_object_sha": selected.tag_object_sha,
                })
                if len(options["input_text"].encode("ascii")) > SSH_INPUT_LIMIT:
                    raise ProductionReleaseError("Пакет резервации PATCH превышает лимит SSH")
        raw = remote(metrics.HttpOperation.ATTEMPT, option, *arguments,
                     timeout=3600 if action in ("install", "complete") else 300, **options)
        # Контроллер заканчивает вывод одной JSON-строкой, установка до неё пишет этапы.
        lines = raw.stdout.splitlines()
        if not lines or len(lines[-1]) > 8192:
            raise ProductionReleaseError("Отсутствует квитанция команды Deployment")
        return parse_state_object(lines[-1])

    reserved = call("resume" if resume else "reserve")
    attempt = records._attempt({"id": 0, "sha": selected.sha, "payload": reserved["attempt"]})
    if ((attempt.attempt_key, attempt.version, attempt.kind, attempt.release_id, attempt.run_id,
         attempt.run_attempt, attempt.baseline) != (key, selected.version, selected.kind, selected.release_id,
         selected.run_id, selected.run_attempt, getattr(selected.compatibility, "baseline", None))
            or type(reserved.get("started")) is not bool):
        raise ProductionReleaseError("Резервация VPS расходится с проверенным Release")

    def claim_creation(action):
        return call(action).get("granted") is True

    def claim_status(action):
        grant = call(action)
        return InstallationReceipt(**grant["receipt"]) if grant.get("granted") is True else None

    bound = reserved["deployment_id"]
    with http_phase(HttpPhase.DEPLOYMENT_PUBLISH):
        if bound is None:
            record = (writer.create_reserved(api, attempt, claim_creation) if signed is not None else
                      writer.create_attempt(api, attempt, claim_creation))
            deployment_id = record.deployment_id
        else:
            deployment_id = records._positive(bound, "D18")
            record, _ = writer.current_attempt(api, attempt, deployment_id)
        if record.state is records.DeploymentState.MISSING:
            if signed is None:
                record = writer.write_status(api, attempt, deployment_id, records.DeploymentState.IN_PROGRESS, claim_status)
            else:
                record = writer.write_reserved_status(api, attempt, record, records.DeploymentState.IN_PROGRESS,
                                                      signed[0].migration, claim_status)
        status_id = record.status_id
    if record.state is records.DeploymentState.IN_PROGRESS:
        if record.description != f"v1 attempt={key}":
            raise ProductionReleaseError("D18 не подтвердил исходный ключ")
        if not reserved["started"]:
            try:
                call("install")
            except InstallationTagError:
                # Ответ SSH мог потеряться в любой момент; разрешена только сверка.
                print("Установка не подтверждена по SSH. Сверка сохранённого этапа", flush=True)
    elif record.state not in (records.DeploymentState.SUCCESS, records.DeploymentState.FAILURE):
        raise ProductionReleaseError("Текущий статус D18 не допускает продолжения")
    result = call("complete")
    if result["outcome"] not in ("success", "restored"):
        raise ProductionReleaseError("Не подтверждены установка или возврат")
    target = records.DeploymentState.SUCCESS if result["outcome"] == "success" else records.DeploymentState.FAILURE
    receipt = InstallationReceipt(**result["receipt"])
    with http_phase(HttpPhase.DEPLOYMENT_PUBLISH):
        if signed is None:
            record = writer.write_status(api, attempt, deployment_id, target, claim_status, receipt)
        else:
            record = writer.write_reserved_status(api, attempt, record, target, signed[0].migration, claim_status, receipt)
        status_id = record.status_id
    if call("close") != result:
        raise ProductionReleaseError("Закрытие попытки не подтверждено для проверенного runtime")
    if record.state is records.DeploymentState.FAILURE:
        raise ProductionReleaseError(f"Deployment {deployment_id} завершён возвратом на {attempt.baseline.version}")
    return record


def _run_release(api: PublishingGitHub, version: str, sha: str, *, change: str | None, notes: str | None,
                 ssh_host: str, known_hosts: Path, identity: Path | None) -> DeploymentRecord:
    """Проводит выпуск через GitHub и контроллер с повторными сверками VPS."""
    releases._version(version)
    deployments._require_deployment_version(version)
    if not SHA_PATTERN.fullmatch(sha):
        raise ProductionReleaseError("Требуется полный 40-символьный SHA")
    route = _v2_route(change is None)
    signer = os.environ.get("RELEASE_SIGNER_ATTEMPT", "")
    enabled = os.environ.get("PRODUCTION_SIGNED_PATCH", "false") == "true"
    signed = None
    print("Проверка GitHub Release", flush=True)
    if signer:
        if not enabled or route is None or change not in (None, "patch"):
            raise ProductionReleaseError("Подписанный допуск требует включённого PATCH и Deployment v2")
        with http_phase(HttpPhase.RELEASE_PREPARE):
            signed = signed_patch.download_admission(api, signer, Path(os.environ["PATCH_ALLOWED_SIGNERS_FILE"]),
                                                     os.environ["COMPATIBILITY_POLICY_SHA"], version, sha)
        selected = signed_patch.publish_or_select(api, signed[0], notes)
    else:
        if enabled and change == "patch":
            # Готовый старый Release сохраняет прежний процесс установки.
            # Отсутствие допуска для новой публикации закрывает её до записи.
            with http_phase(HttpPhase.RELEASE_LOOKUP):
                _release(api, version)
        with http_phase(HttpPhase.AUTHOR):
            deployments._require_trusted_author(api)
        selected = _select_or_publish(api, version, sha, change=change, notes=notes)
    print(f"Release {selected.release_id}, CI {selected.run_id}/{selected.run_attempt}, SHA {selected.sha}", flush=True)
    accepted: InstallationReceipt | None = None

    def remote(operation: metrics.HttpOperation, *arguments: str, timeout: int,
               input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        """Сохраняет агрегат SSH-процесса, включая завершение с ошибкой."""
        try:
            result = run_vps_command((*_CONTROLLER, version, *arguments), ssh_host=ssh_host,
                                     known_hosts=known_hosts, identity=identity, capture_output=True,
                                     timeout=timeout, **({"input_text": input_text} if input_text is not None else {}))
        except InstallationTagError as error:
            cause = error.__cause__
            if isinstance(cause, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
                metrics.relay_vps_http_report(cause.stderr, operation)
            raise
        metrics.relay_vps_http_report(result.stderr, operation)
        return result

    if route is not None:
        return _run_attempt(api, selected, route, change is None, remote, signed=signed)

    def read_fresh() -> InstallationReceipt:
        """Удостоверяет точную приёмку и сохраняет runtime на весь сеанс."""
        nonlocal accepted
        result = remote(metrics.HttpOperation.VERIFY_INSTALLED, "--verify-installed", sha, timeout=240)
        receipt = _receipt(result.stdout, version, sha)
        if ((receipt.kind, receipt.release_id, receipt.run_id, receipt.run_attempt) !=
                (selected.kind, selected.release_id, selected.run_id, selected.run_attempt)):
            raise InstallationTagError("Приёмка VPS не совпадает с Release и полным CI")
        if accepted is not None and receipt != accepted:
            raise InstallationTagError("Runtime или техническая приёмка VPS изменились")
        accepted = receipt
        return receipt

    if change is None:
        print("Продолжение с установленного Release", flush=True)
    else:
        print("Установка Release на VPS", flush=True)
        try:
            remote(metrics.HttpOperation.INSTALL, timeout=3600)
        except InstallationTagError as error:
            raise ProductionReleaseError(
                "Установка не подтверждена. Проверьте состояние VPS. "
                "Если выпуск установлен, продолжите через --resume."
            ) from error

    try:
        receipt = read_fresh()
        print("Очистка прежнего поколения SQLite", flush=True)
        result = remote(metrics.HttpOperation.CLEANUP_OLD, "--cleanup-old", sha, receipt.runtime_id, timeout=240)
        _require_cleanup(result.stdout, receipt)
    except (InstallationTagError, ProductionReleaseError) as error:
        raise ProductionReleaseError(
            "Очистка SQLite не подтверждена. Проверьте состояние VPS. "
            "Повторите --resume либо продолжите сохранённую операцию очистки."
        ) from error
    print("Очистка прежнего поколения SQLite подтверждена", flush=True)
    try:
        with http_phase(HttpPhase.DEPLOYMENT_PUBLISH):
            record = deployments.publish(api, read_fresh)
    except (deployments.DeploymentPublishError, InstallationTagError, DeploymentAcceptanceError,
            ControllerError, DeploySelectionError, PublishingError) as error:
        raise ProductionReleaseError(
            "Очистка SQLite подтверждена, Deployment не подтверждён. Для записи повторите --resume."
        ) from error
    print(f"Deployment {record.deployment_id}, статус {record.status_id} ({record.state.value})", flush=True)
    return record


def main() -> None:
    """Запускает обычный production-выпуск или продолжает принятую установку."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="точный релиз vMAJOR.MINOR.PATCH")
    parser.add_argument("sha", help="полный ожидаемый SHA выпуска")
    parser.add_argument("--ssh-host", required=True, help="проверенный адрес user@host")
    parser.add_argument("--known-hosts", required=True, type=Path, help="файл с проверенным ключом VPS")
    parser.add_argument("--identity", type=Path, help="локальный путь к ключу SSH")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--release", action="store_true", help="опубликовать и установить Release")
    mode.add_argument("--resume", action="store_true", help="завершить очистку и запись Deployment после установки")
    parser.add_argument("--change", choices=("initial", "major", "minor", "patch"), help="тип нового выпуска")
    parser.add_argument("--notes-file", type=Path, help="файл release notes в UTF-8")
    args = parser.parse_args()
    if args.release and (args.change is None or args.notes_file is None):
        parser.error("--release требует --change и --notes-file")
    if args.resume and (args.change is not None or args.notes_file is not None):
        parser.error("--change и --notes-file доступны только для --release")

    operation = metrics.HttpOperation.RELEASE if args.release else metrics.HttpOperation.RESUME
    try:
        v2 = os.environ.get("COMPADRE_DEPLOYMENT_WRITER") in ("v2", "resume-only")
        with metrics.github_http_report(HttpLocation.RUNNER, operation), immutable_git_reads(enabled=v2):
            if repository_from_origin() != EXPECTED_REPOSITORY:
                raise ProductionReleaseError("Origin не совпадает с репозиторием проекта")
            api = PublishingGitHub(EXPECTED_REPOSITORY, os.environ.get("GITHUB_TOKEN", ""))
            notes = args.notes_file.read_text(encoding="utf-8") if args.notes_file is not None else None
            record = _run_release(api, args.version, args.sha, change=args.change, notes=notes,
                                  ssh_host=args.ssh_host, known_hosts=args.known_hosts, identity=args.identity)
            print(json.dumps({"deployment_id": record.deployment_id, "status_id": record.status_id,
                              "state": record.state.value, "cleanup": "completed"}, ensure_ascii=False))
    except (ProductionReleaseError, deployments.DeploymentPublishError, DeploymentAcceptanceError,
            InstallationTagError, DeploySelectionError, releases.PublishError, ReleaseBranchError,
            AdmissionError, ControllerError, PublishingError, HotfixError, OSError, KeyError,
            TypeError, ValueError) as error:
        print(f"Production-выпуск: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
