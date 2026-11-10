"""Проверяет CI evidence и приватную предварительную SQLite-копию до остановки."""

from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
from tempfile import TemporaryDirectory
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from scripts import deploy_prepare as prep
from scripts.ci_repository_compatibility import ReleaseTagEvidence, verify_baseline
from scripts.full_ci_admission import Admission, COMPATIBILITY_STEPS, select_successful_run
from scripts.full_ci_controller import GitHub
from scripts.repository_compatibility_contract import load_policy, verify_report, verify_tests
from compadre_assistant.core.data.sqlite.runtime_schema import schema_fingerprint


# Лимиты защищают контроллер от чрезмерного artifact и распаковки ZIP.
MAX_ARTIFACT = 64 * 1024 * 1024
HARNESS = ("ci_repository_compatibility.py", "repository_compatibility_contract.py",
           "repository_compatibility_policy.json", "check_repository_compatibility.py",
           "repository_compatibility_worker.py", "repository_compatibility_cases.py")
WORKFLOWS = ("full-suite.yml", "main-full.yml", "release-full.yml")


class PredeployError(ValueError):
    """Содержит только статический SAFE-код отказа без внешнего текста."""

    def __init__(self, code: str) -> None:
        """Принимает код проверки, заданный контроллером, а не внешними данными."""
        self.code = code
        super().__init__(code)


@contextmanager
def check(code: str):
    """Заменяет внешнюю ошибку безопасным кодом, сохраняя точный внутренний код.

    Args:
        code: Статический код проверки без данных БД, token и HTTP-ответов.

    Yields:
        Управление проверке; её отказ закрывает допуск.
    """
    try:
        yield
    except PredeployError:
        raise
    except Exception as error:
        raise PredeployError(code) from error


def private_text(path: Path) -> str:
    """Читает внешний root-owned файл 0600 без symlink либо закрывает допуск."""
    with check("SAFE_CONFIG_READ"):
        info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise PredeployError("SAFE_CONFIG_PERMISSIONS")
    with check("SAFE_CONFIG_READ"):
        value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise PredeployError("SAFE_CONFIG_EMPTY")
    return value


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
                    raise PredeployError("SAFE_ARTIFACT_REDIRECT")
                redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
                redirected.remove_header("Authorization")
                return redirected

        with build_opener(ArtifactRedirect()).open(request, timeout=30) as response:
            result = response.read(MAX_ARTIFACT + 1)
        if len(result) > MAX_ARTIFACT:
            raise PredeployError("SAFE_ARTIFACT_SIZE")
        return result


def _blob(api: GitHub, sha: str, path: str) -> bytes:
    value = api.get(f"/contents/{path}", ref=sha)
    if value.get("type") != "file" or value.get("encoding") != "base64":
        raise PredeployError("SAFE_POLICY_BLOB_MISSING")
    data = base64.b64decode(value["content"])
    if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != value["sha"]:
        raise PredeployError("SAFE_POLICY_BLOB_DIGEST")
    return data


def _local_git_object(checkout: Path, kind: str, ref: str) -> bytes:
    return subprocess.run(("git", "--no-replace-objects", "cat-file", kind, ref),
                          cwd=checkout, capture_output=True, check=True).stdout


def _trusted_policy(data: bytes) -> dict:
    with TemporaryDirectory(prefix="predeploy-policy-") as temporary:
        path = Path(temporary) / "policy.json"
        path.write_bytes(data)
        with check("SAFE_POLICY_CONTENT"):
            return load_policy(path)


def tag_evidence_record(checkout: Path, selected, policy_sha: str, previous_sha: str) -> dict:
    """Сверяет загруженный аннотированный тег и возвращает запись для recovery.

    Args:
        checkout: Git checkout с загруженным объектом тега.
        selected: Обычный immutable Release с точной успешной попыткой CI.
        policy_sha: Внешний reviewed pin установленного контроллера.
        previous_sha: SHA фактически установленного last_good.

    Returns:
        Идентификатор доказательства, pin и отпечаток схемы для выбранной пары.

    Raises:
        PredeployError: Тег, попытка, pin или исходный SHA не совпали.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", policy_sha):
        raise PredeployError("SAFE_POLICY_PIN")
    with check("SAFE_TAG_EVIDENCE"):
        if selected.kind != "ordinary" or not selected.tag_object_sha or not selected.compatibility:
            raise PredeployError("SAFE_TAG_EVIDENCE")
        prep.require_exact_tag(checkout, selected.version, selected.sha, tag_object_sha=selected.tag_object_sha)
        raw = _local_git_object(checkout, "tag", selected.tag_object_sha)
        _, separator, message = raw.partition(b"\n\n")
        if not separator:
            raise PredeployError("SAFE_TAG_EVIDENCE")
        evidence = ReleaseTagEvidence.from_message(message.decode("utf-8"))
        if (evidence != selected.compatibility
                or (evidence.run_id, evidence.run_attempt) != (selected.run_id, selected.run_attempt)):
            raise PredeployError("SAFE_TAG_EVIDENCE")
    if evidence.previous_sha != previous_sha:
        raise PredeployError("SAFE_INSTALLED_PREVIOUS_SHA")
    if evidence.policy_sha != policy_sha:
        raise PredeployError("SAFE_POLICY_PIN")
    return {"tag_object_sha": selected.tag_object_sha, "policy_sha": policy_sha,
            "schema_fingerprint": evidence.schema_fingerprint,
            "verification_id": hashlib.sha256(b"compadre-release-tag-v1\0" + raw).hexdigest()}


def verify_tag_evidence(api: GitHub, checkout: Path, selected, policy_sha: str,
                        previous_sha: str) -> dict:
    """Проверяет аннотацию и независимо сверяет workflow с reviewed policy.

    Селектор уже доказал успешный job точной попытки, которой принадлежит
    аннотация. На VPS используются только GitHub Contents и локальные blobs.

    Args:
        api: Читающий GitHub API выбранного репозитория.
        checkout: Git checkout точного коммита кандидата или его сохранённого объекта.
        selected: Результат селектора опубликованного immutable Release.
        policy_sha: Внешний reviewed pin установленного контроллера.
        previous_sha: SHA фактически установленного last_good.

    Returns:
        Идентификатор доказательства, pin и ожидаемый отпечаток схемы.

    Raises:
        PredeployError: Аннотация, pinned policy или workflow не подтверждены.
    """
    if api.repository != selected.repository:
        raise PredeployError("SAFE_CI_PROVENANCE")
    record = tag_evidence_record(checkout, selected, policy_sha, previous_sha)
    policy = _trusted_policy(_blob(api, policy_sha, "scripts/repository_compatibility_policy.json"))
    if policy["repository"] != selected.repository or policy["previous_sha"] != previous_sha:
        raise PredeployError("SAFE_INSTALLED_PREVIOUS_SHA")
    with check("SAFE_WORKFLOW_BYTES"):
        for name in WORKFLOWS:
            path = f".github/workflows/{name}"
            if _blob(api, policy_sha, path) != _local_git_object(checkout, "blob", f"{selected.sha}:{path}"):
                raise PredeployError("SAFE_WORKFLOW_BYTES")
    return record


def _files(payload: bytes) -> dict[str, bytes]:
    result = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_ARTIFACT:
            raise PredeployError("SAFE_ARTIFACT_EXPANDED_SIZE")
        for item in archive.infolist():
            if item.is_dir():
                continue
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                    or path.as_posix() != item.filename or item.filename in result):
                raise PredeployError("SAFE_ARTIFACT_MEMBER")
            result[item.filename] = archive.read(item)
    return result


def verify_evidence(api: EvidenceAPI, selected, policy_sha: str, previous_sha: str) -> dict:
    """Проверяет происхождение, bytes policy/workflows и полноту результатов CI.

    Args:
        api: Независимый GitHub API с actions/contents read.
        selected: Закреплённый Release или допуск точной попытки обычного CI.
        policy_sha: Внешний reviewed pin контроллера, не поле artifact.
        previous_sha: SHA фактически установленного last_good.

    Returns:
        Безопасные идентификаторы artifact и fingerprint candidate-схемы.

    Raises:
        ValueError: Evidence неполно, изменено или относится к другой паре.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", policy_sha):
        raise PredeployError("SAFE_POLICY_PIN")
    branch = "main" if selected.workflow == "main-full.yml" else selected.branch
    if not isinstance(selected, Admission) and selected.kind == "emergency":
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
            raise PredeployError("SAFE_CI_PROVENANCE")
        caller = selected.workflow.removesuffix(".yml")
        jobs = [job for job in api.pages(
            f"/actions/runs/{selected.run_id}/attempts/{selected.run_attempt}/jobs", key="jobs")
            if job.get("name") == f"{caller} / repository-compatibility"]
        if len(jobs) != 1:
            raise PredeployError("SAFE_CI_JOB_MISSING")
        job = jobs[0]
        steps = job.get("steps", [])
        if (job.get("head_sha") != selected.sha or job.get("status") != "completed"
                or job.get("conclusion") != "success"
                or len({step.get("name") for step in steps}) != len(steps)
                or not COMPATIBILITY_STEPS <= {step.get("name") for step in steps}
                or any(step.get("status") != "completed" or step.get("conclusion") != "success"
                       for step in steps)):
            raise PredeployError("SAFE_CI_JOB_FAILED")
    elif select_successful_run(api, selected.repository, selected.workflow, selected.sha,
                               branch, selected.run_id, selected.run_attempt) is None:
        raise PredeployError("SAFE_CI_ATTEMPT")
    trusted = {name: _blob(api, policy_sha, f"scripts/{name}") for name in HARNESS}
    policy = _trusted_policy(trusted["repository_compatibility_policy.json"])
    if policy["repository"] != selected.repository or policy["previous_sha"] != previous_sha:
        raise PredeployError("SAFE_INSTALLED_PREVIOUS_SHA")
    with check("SAFE_BASELINE_RELEASE"):
        verify_baseline(policy, api.get(f"/releases/tags/{policy['release_tag']}"),
                        api.get(f"/git/ref/tags/{policy['release_tag']}"), previous_sha,
                        lambda sha: api.get(f"/git/tags/{sha}"))
    for name in WORKFLOWS:
        path = f".github/workflows/{name}"
        if _blob(api, policy_sha, path) != _blob(api, selected.sha, path):
            raise PredeployError("SAFE_WORKFLOW_BYTES")
    name = f"repository-compatibility-{selected.run_id}-{selected.run_attempt}"
    artifacts = [item for item in api.pages(f"/actions/runs/{selected.run_id}/artifacts", key="artifacts")
                 if item.get("name") == name]
    if len(artifacts) != 1:
        raise PredeployError("SAFE_ARTIFACT_MISSING")
    artifact = artifacts[0]
    identity = artifact.get("workflow_run", {})
    digest = artifact.get("digest", "")
    if (type(artifact.get("id")) is not int or artifact["id"] <= 0
            or artifact.get("expired") is not False
            or datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)
            or identity.get("id") != selected.run_id or identity.get("head_sha") != selected.sha
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise PredeployError("SAFE_ARTIFACT_METADATA")
    payload = api.download(artifact["id"])
    if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise PredeployError("SAFE_ARTIFACT_DIGEST")
    with check("SAFE_ARTIFACT_FORMAT"):
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
        raise PredeployError("SAFE_EVIDENCE_IDENTITY")
    with check("SAFE_REPORT_CONTRACT"):
        report = json.loads(files["report.json"])
        with check("SAFE_TEST_MANIFEST"):
            verify_tests(report["old_repository_behavior"]["old_tests"], policy["test_ids"])
        verify_report(report, policy)
    if report["candidate_sha"] != selected.sha:
        raise PredeployError("SAFE_CANDIDATE_SHA")
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
    with check("SAFE_COPY_BACKUP"):
        original = prep.backup_sqlite(source, directory / "original.db")
    with check("SAFE_COPY_SOURCE_SCHEMA"):
        prep.validate_schema(original, before)
    work = directory / "candidate"
    work.mkdir(mode=0o700)
    with check("SAFE_COPY_BACKUP"):
        candidate = prep.backup_sqlite(original, work / "app.db")
    with check("SAFE_COPY_MIGRATION"):
        migrate(work)
    verify_migrated_copy(original, candidate, expected, fingerprint)


def open_copy_readonly(path: Path) -> sqlite3.Connection:
    """Открывает существующую SQLite-копию в режиме чтения."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def require_copy_foreign_keys(connection: sqlite3.Connection) -> None:
    """Отклоняет SQLite-копию с нарушенными внешними ключами."""
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise PredeployError("SAFE_COPY_FOREIGN_KEYS")


def verify_migrated_copy(original: Path, candidate: Path, expected: list[str],
                         fingerprint: str) -> None:
    """Сравнивает отдельную SQLite-копию с исходным снимком и CI evidence.

    Args:
        original: Проверенный снимок старого поколения.
        candidate: Копия после завершённой изолированной миграции.
        expected: Точный ожидаемый журнал candidate.
        fingerprint: Ожидаемый fingerprint схемы из CI evidence.

    Raises:
        PredeployError: Журнал, integrity, связи, схема или данные не совпали.
    """
    with check("SAFE_COPY_CANDIDATE_SCHEMA"):
        prep.validate_schema(candidate, expected)
    with closing(open_copy_readonly(original)) as old, closing(open_copy_readonly(candidate)) as new:
        require_copy_foreign_keys(old)
        require_copy_foreign_keys(new)
        if schema_fingerprint(new) != fingerprint:
            raise PredeployError("SAFE_COPY_FINGERPRINT")
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
                raise PredeployError("SAFE_COPY_TABLE_LOSS")
            columns = ",".join('"' + column.replace('"', '""') + '"' for column in common)
            query = f"SELECT {columns} FROM {quoted}"
            if Counter(old.execute(query)) != Counter(new.execute(query)):
                raise PredeployError("SAFE_COPY_DATA_LOSS")
