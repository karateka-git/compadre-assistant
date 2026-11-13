"""Создаёт CI evidence из внешне закреплённой policy и точной пары Git SHA."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

# Прямой запуск из workflow использует только package закреплённого checkout.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_repository_compatibility import export_commit, snapshot
from scripts.repository_compatibility_contract import (
    DYNAMIC_POLICY_VERSION, load_policy, verify_inventory, verify_report,
)

if TYPE_CHECKING:
    from scripts.deployment_acceptance import AcceptedBaseline


# Имена в evidence v1 сохраняются для проверки уже опубликованных выпусков.
LEGACY_HARNESS = (
    "ci_repository_compatibility.py", "repository_compatibility_contract.py",
    "repository_compatibility_policy.json", "check_repository_compatibility.py",
    "repository_compatibility_worker.py", "repository_compatibility_cases.py",
)
# V2 использует пути от корня репозитория и закрепляет зависимости читателя Deployments.
DYNAMIC_HARNESS = (
    *(f"scripts/{name}" for name in LEGACY_HARNESS),
    "scripts/deployment_acceptance.py", "scripts/full_ci_controller.py", "scripts/full_ci_admission.py",
    "scripts/deploy_prepare.py", "scripts/create_release_branch.py", "scripts/publish_installation_tag.py",
    "scripts/publish_release.py", "scripts/github_publishing.py", "scripts/check_hotfix_pairs.py",
    "scripts/release_sqlite_summary.py", "scripts/select_deploy_release.py", "scripts/predeploy_compatibility.py",
    "compadre_assistant/__init__.py", "compadre_assistant/core/__init__.py",
    "compadre_assistant/core/data/__init__.py", "compadre_assistant/core/data/sqlite/__init__.py",
    "compadre_assistant/core/data/sqlite/runtime_schema.py",
    "compadre_assistant/core/data/sqlite/migration_runner.py",
)
WORKFLOWS = ("full-suite.yml", "main-full.yml", "release-full.yml")
EVIDENCE_VERSION = 2


@dataclass(frozen=True)
class ReleaseTagEvidence:
    """Хранит проверенные реквизиты CI в аннотации релизного тега."""

    previous_sha: str
    policy_sha: str
    run_id: int
    run_attempt: int
    schema_fingerprint: str

    def __post_init__(self) -> None:
        """Отклоняет недопустимые значения при чтении и публикации тега."""
        for value, length in ((self.previous_sha, 40), (self.policy_sha, 40),
                              (self.schema_fingerprint, 64)):
            if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
                raise ValueError("Некорректный SHA или отпечаток схемы в аннотации тега")
        if any(type(number) is not int or number <= 0 for number in (self.run_id, self.run_attempt)):
            raise ValueError("Некорректная попытка CI в аннотации тега")

    def message(self) -> str:
        """Возвращает каноническую аннотацию версии 1."""
        return json.dumps({"version": 1, **asdict(self)}, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_record(cls, data: dict) -> ReleaseTagEvidence:
        """Восстанавливает evidence из полей durable state.

        Args:
            data: Поля сохранённой аннотации, без номера её формата.

        Returns:
            Аннотация в форме, соответствующей сохранённому состоянию.

        Raises:
            ValueError: Набор полей или значения evidence недопустимы.
        """
        if not isinstance(data, dict):
            raise ValueError("Некорректные сохранённые поля evidence")
        try:
            if "baseline" in data:
                from scripts.deployment_acceptance import AcceptedBaseline, DeploymentAcceptanceError

                try:
                    baseline = AcceptedBaseline(**data["baseline"])
                except DeploymentAcceptanceError as error:
                    raise ValueError("Некорректный Deployment в аннотации тега") from error
                return DeploymentReleaseTagEvidence(**(data | {"baseline": baseline}))
            return cls(**data)
        except TypeError as error:
            raise ValueError("Неполная аннотация тега") from error

    @classmethod
    def from_message(cls, message: str) -> ReleaseTagEvidence:
        """Читает аннотацию версий 1 и 2 и отвергает неоднозначные поля.

        Args:
            message: Текст аннотации immutable Git-тега.

        Returns:
            Сверенные канонические реквизиты CI.

        Raises:
            ValueError: Формат, версия или значения аннотации недопустимы.
        """
        if not isinstance(message, str) or len(message) > 1024:
            raise ValueError("Некорректный размер или формат аннотации тега")
        body = message.rstrip("\n")
        data = json.loads(body)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data.pop("version") not in (1, 2):
            raise ValueError("Неизвестная версия аннотации тега")
        evidence = cls.from_record(data)
        if evidence.message() != body:
            raise ValueError("Аннотация тега не каноническая")
        return evidence


@dataclass(frozen=True)
class DeploymentReleaseTagEvidence(ReleaseTagEvidence):
    """Хранит аннотацию нового выпуска с реквизитами принятого Deployment."""

    baseline: AcceptedBaseline

    def __post_init__(self) -> None:
        """Сверяет форму реквизитов Deployment и SHA аннотации."""
        from scripts.deployment_acceptance import AcceptedBaseline

        super().__post_init__()
        if not isinstance(self.baseline, AcceptedBaseline) or self.baseline.sha != self.previous_sha:
            raise ValueError("Deployment не совпадает с SHA базы в аннотации тега")

    def message(self) -> str:
        """Возвращает каноническую аннотацию версии 2."""
        return json.dumps({"version": 2, **asdict(self)}, sort_keys=True, separators=(",", ":"))


def resolve_release_tag(reference: dict, version: str,
                        read_tag: Callable[[str], object] | None) -> tuple[str, str | None, str | None]:
    """Разыменовывает прямой commit ref или один объект аннотированного тега.

    Args:
        reference: Ответ Git refs API для указанной версии.
        version: Ожидаемое имя Git-тега.
        read_tag: Чтение объекта тега по SHA. None разрешает только прямой ref.

    Returns:
        SHA коммита, SHA объекта тега и его сообщение, если он аннотирован.

    Raises:
        ValueError: Тип, SHA или имя тега не подтверждены.
    """
    obj = reference.get("object") if isinstance(reference, dict) else None
    if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str) or not re.fullmatch(
            r"[0-9a-f]{40}", obj["sha"]):
        raise ValueError("Некорректный Git tag ref")
    if obj.get("type") == "commit":
        return obj["sha"], None, None
    if obj.get("type") != "tag" or read_tag is None:
        raise ValueError("Недопустимый тип Git-тега")
    tag = read_tag(obj["sha"])
    if (not isinstance(tag, dict) or tag.get("sha") != obj["sha"] or tag.get("tag") != version
            or not isinstance(tag.get("message"), str)):
        raise ValueError("Не подтверждён точный объект Git-тега")
    target = tag.get("object")
    if (not isinstance(target, dict) or target.get("type") != "commit"
            or not isinstance(target.get("sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", target["sha"])):
        raise ValueError("Аннотированный тег должен указывать прямо на commit")
    return target["sha"], obj["sha"], tag["message"]


def verify_baseline(policy: dict, release: dict, reference: dict, requested: str,
                    read_tag: Callable[[str], object] | None = None) -> None:
    """Подтверждает immutable Release и commit тега, закреплённые в policy.

    Args:
        policy: Manifest из внешне закреплённого checkout.
        release: Ответ GitHub Releases API для закреплённого tag.
        reference: Ответ Git refs API для того же tag.
        requested: SHA, переданный candidate controller для проверки равенства.
        read_tag: Чтение объекта аннотированного тега по SHA.

    Raises:
        ValueError: Любой признак базы не соответствует доверенной policy.
    """
    if (requested != policy["previous_sha"] or release.get("id") != policy["release_id"]
            or release.get("tag_name") != policy["release_tag"]
            or release.get("immutable") is not True or release.get("draft") is not False
            or release.get("prerelease") is not False or not release.get("published_at")
            or resolve_release_tag(reference, policy["release_tag"], read_tag)[0] != policy["previous_sha"]):
        raise ValueError("baseline is not the pinned immutable release/tag")


def _confirmed_baseline(repository: str, previous_sha: str, frozen: str) -> dict:
    """Сверяет пару SHA и Deployments с независимым чтением доверенного harness."""
    from scripts.deployment_acceptance import AcceptedBaseline, resolve_accepted_baseline
    from scripts.full_ci_controller import GitHub

    payload = json.loads(frozen)
    if not isinstance(payload, dict):
        raise ValueError("invalid accepted baseline")
    expected = AcceptedBaseline(**payload)
    if expected.sha != previous_sha:
        raise ValueError("previous SHA differs from frozen accepted baseline")
    actual = resolve_accepted_baseline(GitHub(repository, os.environ["GITHUB_TOKEN"]))
    if actual != expected:
        raise ValueError("latest accepted baseline changed before compatibility check")
    return asdict(actual)


def main() -> int:
    """Запускает trusted harness без secrets в subprocess и связывает его artifact.

    Returns:
        Ноль только после полного успеха обоих наборов и проверки manifest.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--previous-sha", required=True)
    parser.add_argument("--accepted-baseline", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy_root = Path(__file__).resolve().parents[1]
    candidate = os.environ["GITHUB_SHA"]
    policy_sha = os.environ["COMPATIBILITY_POLICY_SHA"]
    for sha in (candidate, policy_sha):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("exact candidate and external policy SHA are required")
    policy = load_policy()
    if policy["version"] != DYNAMIC_POLICY_VERSION or os.environ["GITHUB_REPOSITORY"] != policy["repository"]:
        raise ValueError("unexpected repository")
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not os.environ[name].isdigit() or int(os.environ[name]) <= 0:
            raise ValueError("missing CI run identity")
    workflow = os.environ["GITHUB_WORKFLOW_REF"]
    allowed = (f"{policy['repository']}/.github/workflows/main-full.yml@refs/heads/main",
               f"{policy['repository']}/.github/workflows/release-full.yml@refs/heads/")
    if workflow != allowed[0] and not (workflow.startswith(allowed[1])
            and re.fullmatch(r"release/\d+\.\d+", workflow[len(allowed[1]):])):
        raise ValueError("unexpected caller workflow")
    with TemporaryDirectory(prefix="compatibility-policy-") as temporary:
        root = Path(temporary)
        trusted = export_commit(policy_root, policy_sha, root / "policy")
        for path in DYNAMIC_HARNESS:
            if hashlib.sha256((policy_root / path).read_bytes()).hexdigest() != trusted[path]:
                raise ValueError(f"policy checkout differs from pinned Git bytes: {path}")
        candidate_manifest = export_commit(args.repository, candidate, root / "candidate")
        for name in WORKFLOWS:
            path = f".github/workflows/{name}"
            if candidate_manifest.get(path) != trusted[path]:
                raise ValueError(f"candidate changed trusted workflow: {name}")
        baseline = _confirmed_baseline(policy["repository"], args.previous_sha, args.accepted_baseline)
        previous_manifest = export_commit(args.repository, args.previous_sha, root / "previous")
        verify_inventory(root / "previous", policy)
        # Candidate requirements/tests не получают API token и CI control file paths.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GITHUB_", "ACTIONS_", "INPUT_"))}
        result = subprocess.run([
            sys.executable, "-B", str(policy_root / "scripts/check_repository_compatibility.py"),
            "--repository", str(args.repository.resolve()), "--previous-sha", args.previous_sha,
            "--candidate-sha", candidate, "--output", str(args.output.resolve()),
        ], cwd=policy_root, env=environment, check=False)
        report = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
        if (result.returncode or report["candidate_sha"] != candidate
                or report["sources"]["previous"] != previous_manifest
                or report["sources"]["candidate"] != candidate_manifest):
            raise ValueError("runner failed or source identity mismatch")
        verify_report(report, policy, previous_sha=args.previous_sha)
        evidence = {
            "version": EVIDENCE_VERSION, "repository": policy["repository"], "candidate_sha": candidate,
            "previous_sha": args.previous_sha, "policy_sha": policy_sha, "baseline": baseline,
            "workflow_ref": workflow, "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "results": {key: report[key]["passed"] for key in (
                "candidate_migrations_data_integrity", "old_repository_behavior",
                "candidate_verified_previous_updates")},
            "files": snapshot(args.output),
            "policy_files": {path: trusted[path] for path in DYNAMIC_HARNESS},
        }
        (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
