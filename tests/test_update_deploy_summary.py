"""Проверяет повторную доставку, восстановление и публикацию сводки в одном PR."""

from contextlib import chdir
from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import yaml

from scripts import update_deploy_summary as writer
from scripts.deploy_summary import AttemptSummary
from scripts.deploy_summary_table import END, START, attempts, update_table, validated

REPO = writer.REPOSITORY
EMPTY = f"Введение\n{START}\n| дата |\n| --- |\n{END}\nМетодика\n"
OLD = Path(writer.DOCUMENT).read_text(encoding="utf-8")
FIRST = AttemptSummary("111:1", "111:1", "v4.4.2", "a" * 40, "patch", "2026-11-21T12:00:00Z",
                       "2026-11-21T12:00:10Z", "2026-11-21T12:02:00Z", 123, 1034, 18, "success")
NEXT = replace(FIRST, attempt="112:1", source="112:1", version="v4.4.3", sha="b" * 40, deployment=124)


class SummaryTests(unittest.TestCase):
    """Сверяет подтверждения, входные данные и идемпотентность."""

    def test_workflow_runs_only_protected_code(self):
        """Входные номера остаются данными, исполняется только фиксированная команда из main."""
        config = yaml.safe_load(Path(".github/workflows/deploy-summary.yml").read_text(encoding="utf-8"))
        triggers = config[True]
        self.assertEqual(triggers["workflow_run"], {"workflows": ["Production release"],
                                                   "branches": ["main"], "types": ["completed"]})
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]), {"run_id", "run_attempt"})
        job = config["jobs"]["summary"]
        self.assertIn("github.ref == 'refs/heads/main'", job["if"])
        self.assertEqual(job["permissions"], {"actions": "read", "contents": "write", "pull-requests": "write"})
        checkout, update = job["steps"]
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(update["run"], "python3 -m scripts.update_deploy_summary")
        self.assertEqual(set(update["env"]), {"GITHUB_TOKEN", "RELEASE_RUN", "RELEASE_ATTEMPT"})

    def test_out_of_order_resume_and_replay(self):
        """Поздний исходный отчёт и повторная доставка сохраняют одну измеренную строку."""
        first = replace(FIRST, outcome="unconfirmed")
        resume = replace(FIRST, attempt="333:1", scenario="resume", installed_from=None,
                         downtime_ms=None, api=4, started_at="2026-11-21T13:00:00Z",
                         ended_at="2026-11-21T13:01:00Z")
        interim = update_table(EMPTY, REPO, resume)
        self.assertIn("| нет данных | нет данных | нет данных | Успех", interim)
        finished = update_table(interim, REPO, first)
        self.assertIn("| 3650 | 1,034 | 22 | Успех", finished)
        self.assertEqual([r.attempt for r in attempts(finished)], ["111:1", "333:1"])
        self.assertEqual(update_table(finished, REPO, resume), finished)
        self.assertEqual(update_table(update_table(EMPTY, REPO, first), REPO, resume), finished)
        legacy = replace(resume, source=resume.attempt)
        self.assertIn("| 3650 | 1,034 | 22 |", update_table(update_table(EMPTY, REPO, first), REPO, legacy))

    def test_failure_restore_and_incomplete_counts(self):
        """Отличает подтверждённый возврат и отказ до выделения Deployment."""
        first = replace(FIRST, outcome="restored", api=None)
        self.assertIn("| 110 | 1,034 | нет данных | Возврат подтверждён", update_table(EMPTY, REPO, first))
        early = replace(FIRST, deployment=None, installed_from=None, downtime_ms=None, outcome="unconfirmed")
        self.assertIn("<!-- run:111:1 -->", update_table(EMPTY, REPO, early))
        rejected = replace(early, attempt="333:1", scenario="resume", version="v9.9.9", api=0)
        document = update_table(update_table(EMPTY, REPO, FIRST), REPO, rejected)
        self.assertIn("<!-- run:333:1 -->", document)
        self.assertIn("| 0 | Результат не подтверждён", document)
        self.assertIn("| 18 | Успех", document)

    def test_historical_replay_preserves_verified_values(self):
        """Проверяет реальную историческую строку при отсутствии нового артефакта."""
        key = "44907760823:1"
        fallback = AttemptSummary(key, key, None, None, "unknown", FIRST.started_at)
        self.assertEqual(update_table(OLD, REPO, fallback), OLD)

    def test_rejects_non_technical_values(self):
        """Отвергает строки и числа, выходящие за формат технического отчёта."""
        for key, bad in (("version", "malicious -->"), ("scenario", "injected | cell"),
                         ("api", True), ("sha", "not SHA"), ("started_at", "yesterday"),
                         ("deployment", -1), ("source", "-c")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validated(dict(asdict(FIRST), **{key: bad}))

    def test_trusted_run_and_authenticated_artifact(self):
        """Сверяет workflow, репозиторий, попытку и digest архива перед чтением JSON."""
        api = Mock(repository=REPO, token="dummy")
        run = dict(id=111, run_attempt=1, status="completed", path=writer.WORKFLOW, event="workflow_dispatch",
                   head_branch="main", repository={"full_name": REPO}, head_repository={"full_name": REPO},
                   head_sha="c" * 40, run_started_at=FIRST.started_at)
        api.get.return_value = run
        api.pages.return_value = []
        self.assertEqual(writer.release_attempt(api, FIRST.attempt).scenario, "unknown")
        out = BytesIO()
        with ZipFile(out, "w") as archive:
            archive.writestr("deployment-summary.json", json.dumps(asdict(FIRST)))
        data = out.getvalue()
        art = dict(name="deployment-summary-111-1", expired=False, id=567,
                   workflow_run={"id": 111, "head_sha": run["head_sha"]}, digest="sha256:" + sha256(data).hexdigest())
        api.pages.return_value = [art]
        with patch.object(writer.EvidenceAPI, "download", return_value=data):
            self.assertEqual(writer.release_attempt(api, FIRST.attempt), FIRST)
            art["digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "digest"):
                writer.release_attempt(api, FIRST.attempt)
        run["head_repository"] = {"full_name": "foreign/fork"}
        with self.assertRaisesRegex(ValueError, "main"):
            writer.release_attempt(api, FIRST.attempt)

    def test_two_releases_and_pr_race(self):
        """Воспроизводит конкурирующую запись в локальный bare remote и гонку создания PR."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--bare", "-q", str(root / "remote")], check=True)
            checkout = root / "checkout"
            checkout.mkdir()
            with chdir(checkout):
                git = writer._git
                git("init", "-q", "-b", "main")
                git("remote", "add", "origin", str(root / "remote"))
                doc = Path(writer.DOCUMENT)
                doc.parent.mkdir()
                doc.write_text(EMPTY, encoding="utf-8")
                git("add", writer.DOCUMENT)
                git("-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "main")
                git("push", "-q", "origin", "main")
                original_push = True

                def race(*args, **kwargs):
                    nonlocal original_push
                    if args[0] == "push" and original_push:
                        original_push = False
                        # Другая задача уже обновила ветку после чтения.
                        self.assertTrue(writer.update_branch(REPO, NEXT))
                    return git(*args, **kwargs)

                with patch.object(writer, "_git", side_effect=race):
                    self.assertTrue(writer.update_branch(REPO, FIRST))
                ref = "refs/heads/" + writer.BRANCH
                git("fetch", "origin", ref)
                saved = git("show", f"FETCH_HEAD:{writer.DOCUMENT}")
                self.assertEqual({r.attempt for r in attempts(saved)}, {"111:1", "112:1"})
                self.assertEqual(git("diff", "--name-only", "main", "FETCH_HEAD").splitlines(), [writer.DOCUMENT])
                old_head = git("rev-parse", "FETCH_HEAD")
                self.assertTrue(writer.update_branch(REPO, FIRST))
                self.assertEqual(git("rev-parse", "FETCH_HEAD"), old_head)
                git("push", "origin", "FETCH_HEAD:refs/heads/main")
                self.assertFalse(writer.update_branch(REPO, FIRST))
        api = Mock(repository=REPO)
        api.get.side_effect = [[], [{"number": 5}], [{"number": 5}]]
        api.post.side_effect = writer.PublishingError("одновременное создание")
        writer.ensure_pr(api)
        writer.ensure_pr(api)
        api.post.assert_called_once()
