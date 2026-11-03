"""Проверяет CI evidence и приватную предварительную SQLite-копию до остановки."""

from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from scripts import deploy_prepare as prep
from scripts.ci_repository_compatibility import verify_baseline
from scripts.full_ci_admission import COMPATIBILITY_STEPS, select_successful_run
from scripts.full_ci_controller import GitHub
from scripts.repository_compatibility_contract import load_policy, verify_report
from compadre_assistant.core.data.sqlite.runtime_schema import schema_fingerprint


# Лимиты защищают контроллер от чрезмерного artifact и распаковки ZIP.
MAX_ARTIFACT = 64 * 1024 * 1024
HARNESS = ("ci_repository_compatibility.py", "repository_compatibility_contract.py",
           "repository_compatibility_policy.json", "check_repository_compatibility.py",
           "repository_compatibility_worker.py", "repository_compatibility_cases.py")


def private_text(path: Path) -> str:
    """Читает внешний root-owned файл 0600 без symlink либо закрывает допуск."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("predeploy configuration must be root-owned 0600")
    return path.read_text(encoding="utf-8").strip()


class EvidenceAPI(GitHub):
    """Читает artifact по GitHub ID; URL и digest не берутся из evidence."""

    def download(self, artifact_id: int) -> bytes:
        """Получает ZIP через доверенный endpoint с ограничением размера."""
        request = Request(f"{self.root}/actions/artifacts/{artifact_id}/zip", headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        })
        class ArtifactRedirect(HTTPRedirectHandler):
            """Не передаёт GitHub credential в подписанный storage URL."""

            def redirect_request(self, req, fp, code, msg, headers, newurl):
                """Разрешает только HTTPS redirect и удаляет Authorization."""
                if not newurl.startswith("https://"):
                    raise ValueError("artifact redirect must use HTTPS")
                redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
                redirected.remove_header("Authorization")
                return redirected

        with build_opener(ArtifactRedirect()).open(request, timeout=30) as response:
            result = response.read(MAX_ARTIFACT + 1)
        if len(result) > MAX_ARTIFACT:
            raise ValueError("compatibility artifact exceeds size limit")
        return result


def _blob(api: GitHub, sha: str, path: str) -> bytes:
    value = api.get(f"/contents/{path}", ref=sha)
    if value.get("type") != "file" or value.get("encoding") != "base64":
        raise ValueError("missing trusted Git blob")
    data = base64.b64decode(value["content"])
    if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != value["sha"]:
        raise ValueError("Git blob digest mismatch")
    return data


def _files(payload: bytes) -> dict[str, bytes]:
    result = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_ARTIFACT:
            raise ValueError("expanded artifact exceeds size limit")
        for item in archive.infolist():
            if item.is_dir():
                continue
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                    or path.as_posix() != item.filename or item.filename in result):
                raise ValueError("unsafe or duplicate artifact member")
            result[item.filename] = archive.read(item)
    return result


def verify_evidence(api: EvidenceAPI, selected, policy_sha: str, previous_sha: str) -> dict:
    """Проверяет происхождение, bytes policy/workflows и полноту результатов CI.

    Args:
        api: Независимый GitHub API с actions/contents read.
        selected: Закреплённый Release с точной попыткой полного CI.
        policy_sha: Внешний reviewed pin контроллера, не поле artifact.
        previous_sha: SHA фактически установленного last_good.

    Returns:
        Безопасные идентификаторы artifact и fingerprint candidate-схемы.

    Raises:
        ValueError: Evidence неполно, изменено или относится к другой паре.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", policy_sha):
        raise ValueError("external compatibility policy pin is required")
    branch = "main" if selected.workflow == "main-full.yml" else selected.branch
    if selected.kind == "emergency":
        run = api.get(f"/actions/runs/{selected.run_id}/attempts/{selected.run_attempt}")
        workflow = api.get(f"/actions/workflows/{selected.workflow}")
        path = f".github/workflows/{selected.workflow}"
        if (selected.workflow not in ("main-full.yml", "release-full.yml")
                or run.get("id") != selected.run_id or run.get("run_attempt") != selected.run_attempt
                or run.get("head_sha") != selected.sha or run.get("head_branch") != branch
                or run.get("repository", {}).get("full_name") != selected.repository
                or run.get("head_repository", {}).get("full_name") != selected.repository
                or run.get("workflow_id") != workflow.get("id") or workflow.get("path") != path
                or run.get("path") not in (path, f"{path}@refs/heads/{branch}", f"{path}@{branch}")
                or run.get("event") not in ("push", "workflow_dispatch")
                or run.get("status") != "completed"):
            raise ValueError("emergency compatibility run provenance mismatch")
        caller = selected.workflow.removesuffix(".yml")
        jobs = [job for job in api.pages(
            f"/actions/runs/{selected.run_id}/attempts/{selected.run_attempt}/jobs", key="jobs")
            if job.get("name") == f"{caller} / repository-compatibility"]
        if len(jobs) != 1:
            raise ValueError("emergency compatibility job is missing")
        job = jobs[0]
        steps = job.get("steps", [])
        if (job.get("head_sha") != selected.sha or job.get("status") != "completed"
                or job.get("conclusion") != "success"
                or len({step.get("name") for step in steps}) != len(steps)
                or not COMPATIBILITY_STEPS <= {step.get("name") for step in steps}
                or any(step.get("status") != "completed" or step.get("conclusion") != "success"
                       for step in steps)):
            raise ValueError("emergency compatibility job did not succeed completely")
    elif select_successful_run(api, selected.repository, selected.workflow, selected.sha,
                               branch, selected.run_id, selected.run_attempt) is None:
        raise ValueError("exact successful CI attempt is required")
    trusted = {name: _blob(api, policy_sha, f"scripts/{name}") for name in HARNESS}
    with TemporaryDirectory(prefix="predeploy-policy-") as temporary:
        path = Path(temporary) / "policy.json"
        path.write_bytes(trusted["repository_compatibility_policy.json"])
        policy = load_policy(path)
    if policy["repository"] != selected.repository or policy["previous_sha"] != previous_sha:
        raise ValueError("installed release differs from compatibility baseline")
    verify_baseline(policy, api.get(f"/releases/tags/{policy['release_tag']}"),
                    api.get(f"/git/ref/tags/{policy['release_tag']}"), previous_sha)
    for name in ("full-suite.yml", "main-full.yml", "release-full.yml"):
        path = f".github/workflows/{name}"
        if _blob(api, policy_sha, path) != _blob(api, selected.sha, path):
            raise ValueError("candidate workflow differs from externally pinned policy")
    name = f"repository-compatibility-{selected.run_id}-{selected.run_attempt}"
    artifacts = [item for item in api.pages(f"/actions/runs/{selected.run_id}/artifacts", key="artifacts")
                 if item.get("name") == name]
    if len(artifacts) != 1:
        raise ValueError("missing or ambiguous compatibility artifact")
    artifact = artifacts[0]
    identity = artifact.get("workflow_run", {})
    digest = artifact.get("digest", "")
    if (type(artifact.get("id")) is not int or artifact["id"] <= 0
            or artifact.get("expired") is not False
            or datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)
            or identity.get("id") != selected.run_id or identity.get("head_sha") != selected.sha
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise ValueError("artifact provenance or lifetime mismatch")
    payload = api.download(artifact["id"])
    if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("artifact digest mismatch")
    files = _files(payload)
    evidence = json.loads(files.pop("evidence.json"))
    expected = {"version": 1, "repository": selected.repository, "candidate_sha": selected.sha,
                "previous_sha": previous_sha, "policy_sha": policy_sha,
                "run_id": selected.run_id, "run_attempt": selected.run_attempt,
                "workflow_ref": f"{selected.repository}/.github/workflows/{selected.workflow}@refs/heads/{branch}",
                "baseline": {"release_id": policy["release_id"], "tag": policy["release_tag"], "immutable": True},
                "results": {key: True for key in ("candidate_migrations_data_integrity",
                            "old_repository_behavior", "candidate_verified_previous_updates")},
                "policy_files": {name: hashlib.sha256(data).hexdigest() for name, data in trusted.items()},
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    if evidence != expected:
        raise ValueError("evidence identity, hashes or results mismatch")
    report = json.loads(files["report.json"])
    verify_report(report, policy)
    if report["candidate_sha"] != selected.sha:
        raise ValueError("runner candidate SHA mismatch")
    return {"artifact_id": artifact["id"], "artifact_digest": digest, "policy_sha": policy_sha,
            "schema_fingerprint": report["candidate_migrations_data_integrity"]["fresh_fingerprint"]}


def verify_copy(source: Path, directory: Path, before: list[str], expected: list[str],
                fingerprint: str, migrate) -> None:
    """Мигрирует online backup и сверяет целостность и сохранность общих полей.

    Args:
        source: Рабочая БД; доступна только SQLite online backup API.
        directory: Приватный временный каталог вне checkout.
        before: Точный исходный журнал.
        expected: Точный журнал candidate.
        fingerprint: Fingerprint candidate-схемы из проверенного CI.
        migrate: Изолированный запуск candidate с единственной writable копией.

    Raises:
        ValueError: Потеря таблицы, строк, общих полей, связей либо другая схема.
    """
    original = prep.backup_sqlite(source, directory / "original.db")
    prep.validate_schema(original, before)
    work = directory / "candidate"
    work.mkdir(mode=0o700)
    candidate = prep.backup_sqlite(original, work / "app.db")
    migrate(work)
    prep.validate_schema(candidate, expected)
    with closing(sqlite3.connect(original)) as old, closing(sqlite3.connect(candidate)) as new:
        if new.execute("PRAGMA foreign_key_check").fetchall() or old.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("copy foreign key violation")
        if schema_fingerprint(new) != fingerprint:
            raise ValueError("production copy differs from CI candidate schema")
        # Общие поля сравниваются как мультимножество: порядок строк не имеет смысла,
        # а multiplicity сохраняет доказательство отсутствия удаления дубликатов.
        tables = old.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall()
        for (name,) in tables:
            if name.startswith("sqlite_") or name == "schema_migrations":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            left = {row[1] for row in old.execute(f"PRAGMA table_info({quoted})")}
            right = {row[1] for row in new.execute(f"PRAGMA table_info({quoted})")}
            common = sorted(left & right)
            if not common:
                raise ValueError("copy lost a table or all of its columns")
            columns = ",".join('"' + column.replace('"', '""') + '"' for column in common)
            query = f"SELECT {columns} FROM {quoted}"
            if Counter(old.execute(query)) != Counter(new.execute(query)):
                raise ValueError("copy did not preserve existing data")
