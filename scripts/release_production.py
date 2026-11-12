"""Публикует обычный выпуск, устанавливает его и завершает очистку SQLite."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote

from scripts import publish_deployment_acceptance as deployments
from scripts import publish_release as releases
from scripts.check_hotfix_pairs import HotfixError
from scripts.create_release_branch import EXPECTED_REPOSITORY, ReleaseBranchError, repository_from_origin
from scripts.deploy_database_switch import OLD_CLEANUP_VERSION
from scripts.deploy_prepare import PreparationError, parse_state_object
from scripts.deployment_acceptance import DeploymentAcceptanceError, DeploymentRecord, require_accepted_deployment
from scripts.full_ci_admission import AdmissionError, SHA_PATTERN
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.publish_installation_tag import InstallationReceipt, InstallationTagError, _receipt, run_vps_command
from scripts.select_deploy_release import DeploySelectionError, SelectedRelease, select_release


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
            api.get(f"/releases/tags/{quote(version, safe='')}")
        except ControllerError as error:
            if not isinstance(error.__cause__, HTTPError) or error.__cause__.code != 404:
                raise
            # prepare повторно проверит отсутствие версии и тега перед любой записью.
            assert notes is not None
            prepared = releases.prepare(api, version, sha, change, notes)
            print("Публикация GitHub Release", flush=True)
            try:
                releases.publish(api, prepared)
            except (releases.PublishError, PublishingError, ControllerError) as error:
                raise ProductionReleaseError(
                    f"Публикация Release не подтверждена. Проверьте тег и Release перед повтором. Причина: {error}"
                ) from error
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


def _run_release(api: PublishingGitHub, version: str, sha: str, *, change: str | None, notes: str | None,
                 ssh_host: str, known_hosts: Path, identity: Path | None) -> DeploymentRecord:
    """Проводит выпуск через GitHub и контроллер с повторными сверками VPS."""
    releases._version(version)
    deployments._require_deployment_version(version)
    if not SHA_PATTERN.fullmatch(sha):
        raise ProductionReleaseError("Требуется полный 40-символьный SHA")
    deployments._require_trusted_author(api)
    print("Проверка GitHub Release", flush=True)
    selected = _select_or_publish(api, version, sha, change=change, notes=notes)
    print(f"Release {selected.release_id}, CI {selected.run_id}/{selected.run_attempt}, SHA {selected.sha}", flush=True)
    accepted: InstallationReceipt | None = None

    def read_fresh() -> InstallationReceipt:
        """Удостоверяет точную приёмку и сохраняет runtime на весь сеанс."""
        nonlocal accepted
        result = run_vps_command(
            (*_CONTROLLER, version, "--verify-installed", sha),
            ssh_host=ssh_host, known_hosts=known_hosts, identity=identity, capture_output=True, timeout=240,
        )
        receipt = _receipt(result.stdout, version, sha)
        if ((receipt.kind, receipt.release_id, receipt.run_id, receipt.run_attempt) !=
                (selected.kind, selected.release_id, selected.run_id, selected.run_attempt)):
            raise InstallationTagError("Приёмка VPS не совпадает с Release и полным CI")
        if accepted is not None and receipt != accepted:
            raise InstallationTagError("Runtime или техническая приёмка VPS изменились")
        accepted = receipt
        return receipt

    def deploy() -> None:
        """Устанавливает Release через штатный контроллер VPS."""
        print("Установка Release на VPS", flush=True)
        try:
            run_vps_command(
                (*_CONTROLLER, version),
                ssh_host=ssh_host, known_hosts=known_hosts, identity=identity, capture_output=True, timeout=3600,
            )
        except InstallationTagError as error:
            raise ProductionReleaseError(
                "Установка не подтверждена. Проверьте состояние VPS. "
                "Если выпуск установлен, продолжите через --resume."
            ) from error

    try:
        if change is None:
            print("Продолжение с установленного Release", flush=True)
            record = deployments.publish(api, read_fresh)
        else:
            record = deployments.deploy_and_publish(api, version, sha, kind="ordinary",
                                                    deploy=deploy, read_fresh=read_fresh)
    except (deployments.DeploymentPublishError, InstallationTagError, DeploymentAcceptanceError,
            ControllerError, DeploySelectionError) as error:
        raise ProductionReleaseError(
            "Deployment не подтверждён. Для принятой установки повторите --resume. "
            "При частичном удалении SQLite продолжите сохранённую операцию на VPS."
        ) from error
    print(f"Deployment {record.deployment_id}, статус {record.status_id} ({record.state.value})", flush=True)

    try:
        receipt = read_fresh()
        if require_accepted_deployment(api, receipt) != record:
            raise ProductionReleaseError("Текущий Deployment изменился перед очисткой")
        print("Очистка прежнего поколения SQLite", flush=True)
        result = run_vps_command(
            (*_CONTROLLER, version, "--cleanup-old", sha, receipt.runtime_id),
            ssh_host=ssh_host, known_hosts=known_hosts, identity=identity, capture_output=True, timeout=240,
        )
        _require_cleanup(result.stdout, receipt)
        read_fresh()
    except (InstallationTagError, ProductionReleaseError, DeploymentAcceptanceError, ControllerError,
            DeploySelectionError) as error:
        raise ProductionReleaseError(
            "Очистка SQLite не подтверждена. Проверьте состояние VPS. "
            "Повторите --resume либо продолжите сохранённую операцию очистки."
        ) from error
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
    mode.add_argument("--resume", action="store_true", help="завершить Deployment и очистку после установки")
    parser.add_argument("--change", choices=("initial", "major", "minor", "patch"), help="тип нового выпуска")
    parser.add_argument("--notes-file", type=Path, help="файл release notes в UTF-8")
    args = parser.parse_args()
    if args.release and (args.change is None or args.notes_file is None):
        parser.error("--release требует --change и --notes-file")
    if args.resume and (args.change is not None or args.notes_file is not None):
        parser.error("--change и --notes-file доступны только для --release")

    try:
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
