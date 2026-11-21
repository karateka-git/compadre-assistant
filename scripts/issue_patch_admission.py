"""Выпускает подписанный допуск по завершённой попытке полного release CI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deployment_acceptance import AcceptedBaseline
from scripts.full_ci_admission import (Admission, COMPATIBILITY_CHECK, FULL_CALLERS, FULL_SUITE_CHECK, RELEASE_BRANCH_PATTERN,
                                       RELEASE_WORKFLOW, SHA_PATTERN, _names_and_steps)
from scripts.full_ci_controller import HttpLocation, collect_http_metrics, immutable_git_reads, tag_commit
from scripts.patch_admission import PatchAdmission, sign_admission
from scripts.predeploy_compatibility import EvidenceAPI, verify_evidence
from scripts.publish_release import _published_versions
from scripts.release_sqlite_summary import MIGRATIONS_PREFIX, expected_migration_from_manifest


def _source(api: EvidenceAPI, event: dict) -> tuple[Admission, int, str]:
    run = event["workflow_run"]
    path = f".github/workflows/{RELEASE_WORKFLOW}"
    if (event["action"] != "completed" or event["repository"]["full_name"] != EXPECTED_REPOSITORY
            or api.repository != EXPECTED_REPOSITORY or run["repository"]["full_name"] != api.repository
            or run["head_repository"]["full_name"] != api.repository or run["event"] not in ("push", "workflow_dispatch")
            or run["path"] != path or run["status"] != "completed" or run["conclusion"] != "success"
            or not SHA_PATTERN.fullmatch(run["head_sha"]) or not RELEASE_BRANCH_PATTERN.fullmatch(run["head_branch"])
            or any(type(run[key]) is not int or run[key] <= 0 for key in ("id", "run_attempt", "workflow_id"))):
        raise ValueError("Событие не подтверждает успешную попытку release CI")
    source = Admission(api.repository, RELEASE_WORKFLOW, run["head_branch"], run["head_sha"], run["id"], run["run_attempt"])
    expected_workflow = api.get(f"/actions/workflows/{RELEASE_WORKFLOW}")
    if (expected_workflow.get("id"), expected_workflow.get("path")) != (run["workflow_id"], path):
        raise ValueError("Workflow выпуска расходится с доверенным источником")
    prefix = f"/actions/runs/{source.run_id}/attempts/{source.run_attempt}"
    actual = api.get(prefix)
    if any(actual.get(name) != run[name] for name in
           ("workflow_id", "id", "run_attempt", "head_branch", "head_sha", "event", "status", "conclusion")):
        raise ValueError("GitHub не подтвердил точную успешную попытку")
    jobs = api.pages(prefix + "/jobs", key="jobs")
    if not _names_and_steps(jobs, source.workflow, source.sha):
        raise ValueError("Обязательные проверки полного CI не завершились успешно")
    compatibility = next(job for job in jobs if job["name"] == f"{FULL_CALLERS[RELEASE_WORKFLOW]} / {COMPATIBILITY_CHECK}")
    if "Build isolated compatibility worker" not in {step["name"] for step in compatibility["steps"]}:
        raise ValueError("Исходная job CI не подтвердила изолированный worker")
    suite = next(job for job in jobs if job["name"] == f"{FULL_CALLERS[RELEASE_WORKFLOW]} / {FULL_SUITE_CHECK}")
    receipts = [step["name"] for step in suite["steps"] if step["name"].startswith("Compatibility artifact ")]
    if len(receipts) != 1:
        raise ValueError("Отсутствует точная квитанция загрузки из job совместимости")
    match = re.fullmatch(r"Compatibility artifact ([1-9][0-9]*) ([0-9a-f]{64})", receipts[0])
    if not match:
        raise ValueError("Некорректная квитанция загрузки из job совместимости")
    return source, int(match[1]), match[2]


def _git(repository: Path, *args: str) -> list[str]:
    # Читаем только объекты. В рабочее дерево кандидат не загружается.
    return subprocess.check_output(["git", "-C", str(repository), *args], text=True, encoding="utf8").splitlines()


def prepare_admission(api: EvidenceAPI, event: dict, repository: Path, policy_sha: str) -> PatchAdmission:
    """Проверяет попытку, evidence, границы PATCH и возвращает данные для подписи.

    Args:
        api: Читающий клиент доверенного репозитория.
        event: Событие workflow_run из файла GitHub.
        repository: Полная история Git в доверенном checkout подписанта.
        policy_sha: Внешний pin проверенной политики.

    Returns:
        Подтверждённые реквизиты нового PATCH.

    Raises:
        ValueError: Попытка, квитанция, проверенные байты или диапазон расходятся.
        subprocess.CalledProcessError: Требуемый локальный Git-объект отсутствует.
    """
    source, artifact_id, digest = _source(api, event)
    record = verify_evidence(api, source, policy_sha, None)
    if (record["artifact_id"], record["artifact_digest"]) != (artifact_id, "sha256:" + digest):
        raise ValueError("Артефакт отличается от загрузки подтверждённой job совместимости")
    history = _published_versions(api)
    line = source.branch.removeprefix("release/")
    major, minor = map(int, line.split("."))
    previous = max((v for v in history if v[:2] == (major, minor)), default=None)
    if previous is None or history[previous].get("immutable") is not True:
        raise ValueError("Отсутствует предыдущий immutable Release линии PATCH")
    previous_tag = history[previous]["tag_name"]
    base = tag_commit(api, previous_tag)
    # Точка начала должна лежать на линии первых родителей; пустой диапазон не выпускается.
    if base not in _git(repository, "rev-list", "--first-parent", source.sha)[1:]:
        raise ValueError("Предыдущий тег не начинает непустой диапазон PATCH")
    paths = _git(repository, "ls-tree", "-r", "--name-only", source.sha, "--", MIGRATIONS_PREFIX)
    return PatchAdmission(source.repository, source.branch, f"v{line}.{previous[2] + 1}", source.sha,
                          previous_tag, base, AcceptedBaseline(**record["baseline"]), source.workflow,
                          source.run_id, source.run_attempt, policy_sha, expected_migration_from_manifest(paths),
                          record["schema_fingerprint"], digest)


def main() -> None:
    """Читает защищённое событие, подписывает проверенный допуск и выводит метрики."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    api = EvidenceAPI(EXPECTED_REPOSITORY, os.environ["GITHUB_TOKEN"])
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    with collect_http_metrics(HttpLocation.CI) as metrics, immutable_git_reads():
        try:
            proof = prepare_admission(api, event, Path.cwd(), os.environ["COMPATIBILITY_POLICY_SHA"])
            args.output.write_bytes(sign_admission(proof, args.key))
        finally:
            print("patch-admission-http " + json.dumps(metrics.rows()))
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf8") as summary:
        summary.write(f"PATCH `{proof.version}`, SHA `{proof.candidate_sha}`, "
                      f"полный CI `{proof.run_id}:{proof.run_attempt}`, "
                      f"подписант `{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}`\n")


if __name__ == "__main__":
    main()
