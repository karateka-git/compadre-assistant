"""Проверяет публикацию подписанного PATCH на HTTP-транспорте штатного runner."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from dataclasses import asdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import AbstractHTTPHandler
from zipfile import ZipFile

from scripts import release_production
from scripts.deployment_acceptance import DeploymentAttempt
from scripts.patch_admission import PRINCIPAL, PatchAdmission, sign_admission
from scripts.publish_installation_tag import InstallationReceipt
from tests import test_patch_http_baseline as fixture


class SignedPatchAPI(fixture._V2RestFixture):
    """Добавляет один подписанный артефакт к стенду двух пар PR."""

    def configure_signer(self, root: Path, fingerprint: str) -> Path:
        """Создаёт разовый ключ, архив подписанта и возвращает путь доверенных ключей."""
        key = root / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        allowed = root / "signers"
        allowed.write_text(f"{PRINCIPAL} {key.with_suffix('.pub').read_text()}", encoding="ascii")
        self.proof = PatchAdmission(fixture.REPOSITORY, fixture.BRANCH, fixture.VERSION, self.graph.candidate,
                                    fixture.PREVIOUS_VERSION, self.graph.base, self._baseline(), fixture.RELEASE_WORKFLOW,
                                    1, 1, fixture.POLICY_SHA, fixture.MIGRATION, fingerprint, "e" * 64)
        self.signed_data = sign_admission(self.proof, key)
        target = io.BytesIO()
        with ZipFile(target, "w") as archive:
            archive.writestr("patch-admission.json", self.signed_data)
        self.artifact.payload = target.getvalue()
        self.signer_digest = "sha256:" + sha256(self.artifact.payload).hexdigest()
        self.latest = fixture.PREVIOUS_VERSION
        self.draft = None
        return allowed

    def _read(self, path, query):
        if path == "/actions/runs/99/attempts/1":
            repo = {"full_name": fixture.REPOSITORY}
            return {"id": 99, "run_attempt": 1, "event": "workflow_run", "path": ".github/workflows/patch-admission.yml",
                    "head_branch": "main", "status": "completed", "conclusion": "success", "head_sha": self.graph.main,
                    "repository": repo, "head_repository": repo}
        if path == "/actions/runs/99/artifacts":
            return {"artifacts": [{"id": 22, "name": "patch-admission-99-1", "expired": False,
                                   "expires_at": "2099-01-01T00:00:00Z", "digest": self.signer_digest,
                                   "workflow_run": {"id": 99, "head_sha": self.graph.main}}]}
        return super()._read(path, query)

    def _graphql(self, location, body):
        if "query PatchPublication(" in body["query"]:
            args = body["variables"]
            assert args == {"owner": fixture.REPOSITORY.split("/")[0], "name": fixture.REPOSITORY.split("/")[1],
                            "version": fixture.VERSION, "ref": f"refs/tags/{fixture.VERSION}"}
            return {"repository": {"nameWithOwner": fixture.REPOSITORY, "release": self.draft,
                                   "ref": self.refs.get(fixture.VERSION), "latestRelease": {"tagName": self.latest}}}
        return super()._graphql(location, body)


class _Remote:
    """Возвращает ожидаемые квитанции ограниченного SSH без измерения сервера."""

    def __init__(self, api):
        self.api = api
        self.actions = []
        self.result = {"outcome": "success", "receipt": asdict(api.receipt)}

    def __call__(self, arguments, **options):
        _, _, _, version, flag, action, sha, key, owner, snapshot, deployment, status = arguments
        self.actions.append(action)
        assert flag == "--patch-attempt"
        assert snapshot == fixture.SNAPSHOT
        base = self.api._baseline()
        if action in ("reserve", "resume"):
            data = json.loads(options["input_text"])
            assert data["admission"].encode() == self.api.signed_data
            assert data["tag_object_sha"] == self.api.refs[version]["object"]["sha"]
            attempt = DeploymentAttempt(2, key, version, sha, "ordinary", data["release_id"], 1, 1, base)
            response = {"attempt": asdict(attempt), "deployment_id": None, "started": False}
            if action == "resume":
                assert owner != key and self.api.source_finished
                response.update(deployment_id=self.api.history_size + 1, started=True)
        elif action == "create":
            response = {"granted": True}
        elif action == "in_progress":
            accepted = InstallationReceipt(1, base.version, base.sha, "ordinary", base.release_id, 2, 1,
                                           fixture.BASE_GENERATION, fixture.MIGRATION, fixture.RUNTIME)
            response = {"granted": True, "receipt": asdict(accepted)}
        elif action == "finish":
            response = {"granted": True, "receipt": self.result["receipt"]}
        elif action == "install":
            assert int(status) > 0 and int(deployment) > 0
            response = {"installed": sha}
        else:
            assert action in ("complete", "close")
            response = self.result
        return subprocess.CompletedProcess([], 0, json.dumps(response), "")


class SignedPatchRunnerTests(unittest.TestCase):
    """Вызывает штатный CLI из shallow checkout main и считает фактические HTTP."""

    def setUp(self) -> None:
        """Подготавливает независимые main, кандидат и базу v2."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        graph, self.runner, fingerprint = fixture._fixture(self.root)
        self.api = SignedPatchAPI(graph, fingerprint, fixture.HISTORY_SIZE)
        keys = self.api.configure_signer(self.root, fingerprint)
        self.remote = _Remote(self.api)
        self.notes = self.root / "notes.md"
        self.notes.write_text("Исправления PATCH", encoding="utf8")
        self.enterContext(chdir(self.runner))
        self.enterContext(patch.object(AbstractHTTPHandler, "do_open", side_effect=self.api.transport))
        self.enterContext(patch.object(release_production, "run_vps_command", side_effect=self.remote))
        self.enterContext(patch.dict(os.environ, GITHUB_TOKEN=fixture.RUNNER_TOKEN, COMPADRE_DEPLOYMENT_READER="v2",
                                     COMPADRE_DEPLOYMENT_WRITER="v2", COMPADRE_CONTROLLER_SHA=fixture.SNAPSHOT,
                                     GITHUB_RUN_ID=str(fixture.SOURCE_RUN), GITHUB_RUN_ATTEMPT="1",
                                     RELEASE_SOURCE_ATTEMPT="", RELEASE_SIGNER_ATTEMPT="99:1",
                                     PRODUCTION_SIGNED_PATCH="true", PATCH_ALLOWED_SIGNERS_FILE=str(keys),
                                     COMPATIBILITY_POLICY_SHA=fixture.POLICY_SHA))

    def _run(self, *, sha=None, resume=False):
        flags = ["--resume"] if resume else ["--release", "--change", "patch", "--notes-file", str(self.notes)]
        args = ["release_production", fixture.VERSION, sha or self.api.graph.candidate,
                "--ssh-host", "vps.invalid", "--known-hosts", str(self.notes), *flags]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", args), redirect_stdout(out), redirect_stderr(err):
            try:
                release_production.main()
            except SystemExit:
                self.last_error = err.getvalue()
                raise
        reports = [json.loads(line.removeprefix("deploy-http-metrics "))
                   for line in err.getvalue().splitlines() if line.startswith("deploy-http-metrics ")]
        return json.loads(out.getvalue().splitlines()[-1]), reports

    def test_patch_after_branch_advanced(self) -> None:
        """Публикует выбранный SHA после продвижения ветки, сохраняя latest старшей линии."""
        self.api.release_tip = self.api.graph.main
        self.api.latest = "v5.0.0"
        result, _ = self._run()
        self.assertEqual(result["state"], "success")
        release = self.api.releases[fixture.VERSION]
        self.assertEqual(release["target_commitish"], self.api.graph.candidate)
        self.assertEqual(release["make_latest"], "false")
        self.assertEqual(len(self.api.posts), 6)

    def test_wrong_sha_and_damaged_zip_stop_before_publication(self) -> None:
        """Чужой SHA и другой digest останавливают выпуск до первой записи."""
        with self.assertRaises(SystemExit):
            self._run(sha=self.api.graph.main)
        self.api.signer_digest = "sha256:" + "0" * 64
        with self.assertRaises(SystemExit):
            self._run()
        self.assertEqual((self.api.posts, self.remote.actions), ([], []))

    def test_resume_ready_release_after_lost_post(self) -> None:
        """Сверяет потерянный success и завершает прежний Deployment без повторной установки."""
        self.api.lose_success_response = True
        self._run()
        self.api.source_finished = True
        self.remote.actions.clear()
        with patch.dict(os.environ, GITHUB_RUN_ID=str(fixture.RESUME_RUN),
                        RELEASE_SOURCE_ATTEMPT=f"{fixture.SOURCE_RUN}:1"):
            self.assertEqual(self._run(resume=True)[0]["state"], "success")
        self.assertEqual(self.api.status_writes, ["in_progress", "success"])
        self.assertEqual(self.remote.actions, ["resume", "complete", "close"])

    def test_conflict_and_draft_before_writes(self) -> None:
        """Черновик с pending tag и отдельный существующий ref блокируют первую запись."""
        self.api.draft = {"isDraft": True, "isPrerelease": False}
        with self.assertRaises(SystemExit):
            self._run()
        self.api.draft = None
        self.api.refs[fixture.VERSION] = {"id": "occupied"}
        with self.assertRaises(SystemExit):
            self._run()
        self.assertEqual((self.api.posts, self.remote.actions), ([], []))
