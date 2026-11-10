"""Проверяет операторские шаги ротации без GitHub и production VPS."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from scripts import rotate_ci_baseline as rotation
from scripts.full_ci_admission import Admission
from scripts.publish_installation_tag import InstallationReceipt


PREVIOUS_PIN = "1" * 40
POLICY_SHA = "2" * 40
MAIN_SHA = "3" * 40
INSTALLED_SHA = "4" * 40
RECEIPT = InstallationReceipt(1, "v3.4.1", INSTALLED_SHA, "ordinary", 1, 2, 3, "5" * 32, None, "6" * 64)
PLAN = rotation.RotationPlan(RECEIPT, POLICY_SHA, MAIN_SHA)


class RotateCIBaselineTest(unittest.TestCase):
    """Проверяет ограничения для reviewed policy, pin и точной попытки CI."""

    def setUp(self):
        """Готовит стабильные приёмку, policy и клиента без сетевых запросов."""
        self.api = Mock(spec=rotation.RotationGitHub)
        self.api.repository = rotation.EXPECTED_REPOSITORY
        self.api.authenticated_user.return_value = {"login": "karateka-git"}
        self.read_fresh = Mock(return_value=RECEIPT)
        self.old_policy = json.loads(Path(rotation.POLICY_FILE).read_text(encoding="utf-8"))
        self.new_policy = {**self.old_policy, "previous_sha": INSTALLED_SHA,
                           "release_tag": RECEIPT.version, "release_id": RECEIPT.release_id}

    def test_prepare_changes_only_three_fields_after_fresh_receipt_and_inventory(self):
        """Подготовка меняет только поля принятой базы после повторной сверки."""
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(json.dumps(self.old_policy), encoding="utf-8")
            with patch.object(rotation, "POLICY_FILE", str(path)), \
                    patch.object(rotation, "require_accepted_installation") as accepted, \
                    patch.object(rotation, "export_commit") as export, \
                    patch.object(rotation, "verify_inventory") as inventory, \
                    patch.object(rotation, "git", return_value=SimpleNamespace(stdout="")):
                rotation.prepare_policy(self.api, self.read_fresh)
                accepted.assert_called_once_with(self.api, RECEIPT)
                self.assertEqual(export.call_args.args[1], INSTALLED_SHA)
                self.assertEqual(inventory.call_args.args[1], self.new_policy)
                self.assertEqual(self.read_fresh.call_count, 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.new_policy)

    def test_review_accepts_exact_single_file_merge_only(self):
        """Ротация требует отдельный merge и допускает подтверждение исходного run."""
        pull = {"merge_commit_sha": POLICY_SHA}
        self.api.get.return_value = {"sha": POLICY_SHA, "parents": [{"sha": PREVIOUS_PIN}, {"sha": MAIN_SHA}]}
        policy_file = {"filename": rotation.POLICY_FILE, "status": "modified"}
        blobs = {PREVIOUS_PIN: self.old_policy, POLICY_SHA: self.new_policy, MAIN_SHA: self.new_policy}

        def read_blob(api, sha, path):
            self.assertIs(api, self.api)
            self.assertEqual(path, rotation.POLICY_FILE)
            return json.dumps(blobs[sha]).encode("utf-8")

        with patch.object(rotation, "require_accepted_installation"), \
                patch.object(rotation, "_main_sha", return_value=MAIN_SHA), \
                patch.object(rotation, "checked_main_head", return_value=INSTALLED_SHA), \
                patch.object(rotation, "_pull", return_value=pull), patch.object(rotation, "_blob", side_effect=read_blob), \
                patch.object(rotation, "_fast_at_merge", return_value=SimpleNamespace(state="success")):
            self.api.pages.return_value = [policy_file]
            self.assertEqual(rotation.review_rotation(self.api, RECEIPT, pr=42, main_sha=MAIN_SHA), PLAN)
            blobs[INSTALLED_SHA] = self.new_policy
            self.assertEqual(rotation.review_rotation(self.api, RECEIPT, pr=42, main_sha=MAIN_SHA, resume=True), PLAN)
            blobs[INSTALLED_SHA] = self.old_policy
            with self.assertRaisesRegex(rotation.RotationError, "больше не содержит"):
                rotation.review_rotation(self.api, RECEIPT, pr=42, main_sha=MAIN_SHA, resume=True)
            self.api.pages.return_value = [policy_file, {"filename": "README.md", "status": "modified"}]
            with self.assertRaisesRegex(rotation.RotationError, "только файл"):
                rotation.review_rotation(self.api, RECEIPT, pr=42, main_sha=MAIN_SHA)
            self.api.pages.return_value = [policy_file]
            blobs[POLICY_SHA] = {**self.new_policy, "release_id": RECEIPT.release_id + 1}
            with self.assertRaisesRegex(rotation.RotationError, "данные принятого выпуска"):
                rotation.review_rotation(self.api, RECEIPT, pr=42, main_sha=MAIN_SHA)

    def test_pin_lost_response_reads_back_without_second_write(self):
        """Ответ может потеряться после записи GitHub и проверяется чтением."""
        self.api.update_policy_pin.side_effect = rotation.RotationError("timeout")
        with patch.object(rotation, "_main_sha", return_value=MAIN_SHA), \
                patch.object(rotation, "_policy_pin", side_effect=[PREVIOUS_PIN, POLICY_SHA, POLICY_SHA]):
            rotation.pin(self.api, PLAN, expected_old=PREVIOUS_PIN, read_fresh=self.read_fresh)
        self.api.update_policy_pin.assert_called_once_with(POLICY_SHA)

    def test_pin_does_not_write_for_unrelated_pin_changed_receipt_or_non_owner(self):
        """Чужой pin, изменение VPS и чужой token не допускают запись."""
        with patch.object(rotation, "_main_sha", return_value=MAIN_SHA), \
                patch.object(rotation, "_policy_pin", return_value=MAIN_SHA):
            with self.assertRaisesRegex(rotation.RotationError, "прежнего значения"):
                rotation.pin(self.api, PLAN, expected_old=PREVIOUS_PIN, read_fresh=self.read_fresh)
            self.read_fresh.return_value = replace(RECEIPT, runtime_id="7" * 64)
            with self.assertRaisesRegex(rotation.RotationError, "приёмка VPS изменилась"):
                rotation.pin(self.api, PLAN, expected_old=PREVIOUS_PIN, read_fresh=self.read_fresh)
            self.api.authenticated_user.return_value = {"login": "other"}
            with self.assertRaisesRegex(rotation.RotationError, "владелец репозитория"):
                rotation.pin(self.api, PLAN, expected_old=PREVIOUS_PIN, read_fresh=self.read_fresh)
        self.api.update_policy_pin.assert_not_called()

    def test_dispatch_rejects_pending_run_and_reports_ambiguous_response(self):
        """Известный запуск сохраняется, неоднозначный ответ требует сверки."""
        self.api.pages.return_value = [{"head_sha": MAIN_SHA, "head_branch": "main", "status": "queued"}]
        with patch.object(rotation, "_main_sha", return_value=MAIN_SHA), \
                patch.object(rotation, "_policy_pin", return_value=POLICY_SHA):
            with self.assertRaisesRegex(rotation.RotationError, "незавершённый CI"):
                rotation.dispatch(self.api, PLAN, read_fresh=self.read_fresh)
            self.api.pages.return_value = [{"id": 20, "head_sha": MAIN_SHA, "head_branch": "main",
                "status": "completed", "event": "workflow_dispatch", "display_title": rotation.RUN_TITLE_PREFIX + POLICY_SHA}]
            with self.assertRaisesRegex(rotation.RotationError, "уже имеет run=20"):
                rotation.dispatch(self.api, PLAN, read_fresh=self.read_fresh)
            self.api.dispatch_rotation.assert_not_called()
            self.api.pages.return_value = []
            self.api.dispatch_rotation.side_effect = rotation.RotationError("timeout")
            with self.assertRaisesRegex(rotation.RotationError, "Найдите точный run"):
                rotation.dispatch(self.api, PLAN, read_fresh=self.read_fresh)
        self.api.dispatch_rotation.assert_called_once_with(PLAN)

    def test_exact_run_refuses_wrong_attempt_failed_ci_and_pin_drift(self):
        """Успех другого CI или устаревшего pin не считается ротацией."""
        api = Mock(spec=rotation.EvidenceAPI)
        api.repository = rotation.EXPECTED_REPOSITORY
        run = {"id": 20, "run_attempt": 2, "head_sha": MAIN_SHA, "head_branch": "main",
               "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
               "display_title": rotation.RUN_TITLE_PREFIX + POLICY_SHA}
        admission = Admission(api.repository, rotation.MAIN_WORKFLOW, "main", MAIN_SHA, 20, 2)

        def verify():
            return rotation.verify_rotation_run(api, receipt=RECEIPT, policy_sha=POLICY_SHA,
                                                main_sha=MAIN_SHA, run_id=20, attempt=2)

        with patch.object(rotation, "_policy_pin", return_value=POLICY_SHA) as pin, \
                patch.object(rotation, "_blob", return_value=json.dumps(self.new_policy).encode()) as blob, \
                patch.object(rotation, "verify_evidence") as evidence:
            for field, value in [("event", "push"), ("run_attempt", 1), ("conclusion", "failure"),
                                 ("display_title", rotation.RUN_TITLE_PREFIX + PREVIOUS_PIN)]:
                api.get.return_value = {**run, field: value}
                with self.subTest(field=field), self.assertRaises(rotation.RotationError):
                    verify()
            evidence.assert_not_called()
            api.get.return_value = run
            blob.return_value = json.dumps({**self.new_policy, "release_id": RECEIPT.release_id + 1}).encode()
            with self.assertRaisesRegex(rotation.RotationError, "принятый Release"):
                verify()
            blob.return_value = json.dumps(self.new_policy).encode()
            self.assertEqual(verify(), admission)
            evidence.assert_called_once_with(api, admission, POLICY_SHA, INSTALLED_SHA)
            api.get.assert_called_with("/actions/runs/20/attempts/2")
            pin.side_effect = [POLICY_SHA, PREVIOUS_PIN]
            with self.assertRaisesRegex(rotation.RotationError, "pin изменился"):
                verify()


class MergedPolicyPRTest(unittest.TestCase):
    """Проверяет ротацию, когда GitHub очистил связь прогона со слитым PR."""

    def setUp(self):
        """Готовит ответы GitHub для merge, точной попытки и её checkout."""
        self.api = Mock(spec=rotation.RotationGitHub)
        self.api.repository = rotation.EXPECTED_REPOSITORY
        self.head = "7" * 40
        self.test_sha = "8" * 40
        self.tree = {"sha": "9" * 40}
        self.parents = [{"sha": PREVIOUS_PIN}, {"sha": self.head}]
        self.workflow = f"/actions/workflows/{rotation.PR_WORKFLOW}"
        self.run_path = "/actions/runs/20/attempts/1"
        self.completed_at = "2026-11-10T10:26:00Z"
        repo = {"full_name": self.api.repository}
        self.pull = {"number": 42, "merged_at": "2026-11-10T10:35:29Z", "merge_commit_sha": POLICY_SHA,
                     "head": {"sha": self.head, "ref": "policy-branch", "repo": repo},
                     "base": {"sha": PREVIOUS_PIN, "ref": "main"}}
        self.run = {"id": 20, "run_attempt": 1, "workflow_id": 30, "check_suite_id": 40,
                    "path": f".github/workflows/{rotation.PR_WORKFLOW}", "event": "pull_request",
                    "repository": repo, "head_repository": repo, "head_branch": "policy-branch", "head_sha": self.head,
                    "created_at": "2026-11-10T10:25:33Z", "run_started_at": "2026-11-10T10:25:33Z", "pull_requests": []}
        self.job = {"id": 50, "name": rotation.PR_CHECK, "run_id": 20, "run_attempt": 1, "head_sha": self.head,
                    "status": "completed", "conclusion": "success", "started_at": "2026-11-10T10:25:36Z",
                    "completed_at": self.completed_at,
                    "steps": [{"name": "Checkout test merge", "conclusion": "success",
                               "started_at": "2026-11-10T10:25:38Z", "completed_at": "2026-11-10T10:25:39Z"}]}
        self.check = {"id": 50, "name": rotation.PR_CHECK, "status": "completed", "conclusion": "success",
                      "head_sha": self.head, "completed_at": self.completed_at,
                      "app": {"id": 15368, "slug": "github-actions"}, "check_suite": {"id": 40}}
        self.log = ("2026-11-10T10:25:38.5573548Z [command]/usr/bin/git -c protocol.version=2 fetch --prune origin "
                    f"+{self.test_sha}:refs/remotes/pull/42/merge\n"
                    "2026-11-10T10:25:39.0823584Z [command]/usr/bin/git checkout --progress --force refs/remotes/pull/42/merge\n"
                    "2026-11-10T10:25:39.1298545Z [command]/usr/bin/git log -1 --format=%H\n"
                    f"2026-11-10T10:25:39.1333946Z {self.test_sha}\n")
        self.responses = {
            "/git/ref/heads/main": {"ref": "refs/heads/main", "object": {"type": "commit", "sha": MAIN_SHA}},
            "/pulls/42": self.pull,
            f"/compare/{POLICY_SHA}...{MAIN_SHA}": {"status": "ahead", "base_commit": {"sha": POLICY_SHA}},
            f"/git/commits/{POLICY_SHA}": {"sha": POLICY_SHA, "tree": self.tree, "parents": self.parents},
            f"/git/commits/{self.test_sha}": {"sha": self.test_sha, "tree": self.tree, "parents": self.parents},
            self.workflow: {"id": 30, "path": self.run["path"]}, self.run_path: deepcopy(self.run),
            "/check-runs/50": self.check,
        }
        policy_files = [{"filename": rotation.POLICY_FILE, "status": "modified"}]
        self.pages = {f"{self.workflow}/runs": [self.run], f"{self.run_path}/jobs": [self.job],
                      "/pulls/42/files": policy_files, f"/commits/{POLICY_SHA}": policy_files}
        self.api.get.side_effect = lambda path: self.responses[path]
        self.api.pages.side_effect = lambda path, **params: self.pages[path]
        self.api.job_log.side_effect = lambda job_id: self.log
        self.old_policy = json.loads(Path(rotation.POLICY_FILE).read_text(encoding="utf-8"))
        self.new_policy = {**self.old_policy, "previous_sha": INSTALLED_SHA,
                           "release_tag": RECEIPT.version, "release_id": RECEIPT.release_id}

    def _review(self, *, main_sha=MAIN_SHA, resume=False):
        def read_blob(_api, sha, _path):
            return json.dumps(self.old_policy if sha == PREVIOUS_PIN else self.new_policy).encode()

        with patch.object(rotation, "require_accepted_installation"), patch.object(rotation, "_blob", side_effect=read_blob):
            return rotation.review_rotation(self.api, RECEIPT, pr=42, main_sha=main_sha, resume=resume)

    def test_accepts_exact_checkout_of_merged_policy_pr(self):
        """Подтверждает текущий main и повторную проверку после его продвижения."""
        self.assertEqual(self._review(), PLAN)
        self.api.job_log.assert_called_once_with(50)
        self.api.pages.assert_any_call(f"{self.workflow}/runs", key="workflow_runs", event="pull_request", head_sha=self.head)
        new_main = "a" * 40
        self.responses["/git/ref/heads/main"]["object"]["sha"] = new_main
        self.responses[f"/compare/{MAIN_SHA}...{new_main}"] = {"status": "ahead", "base_commit": {"sha": MAIN_SHA}}
        self.responses[f"/compare/{POLICY_SHA}...{new_main}"] = {"status": "ahead", "base_commit": {"sha": POLICY_SHA}}
        self.assertEqual(self._review(resume=True), PLAN)
        self.assertEqual(self._review(main_sha=new_main), replace(PLAN, main_sha=new_main))

    def test_rejects_other_pr_and_different_checkout(self):
        """SHA головы сам по себе не доказывает точный PR или его test merge."""
        log = self.log
        for bad_log in (log.replace("pull/42/merge", "pull/43/merge"),
                        log.replace(f" {self.test_sha}\n", f" {MAIN_SHA}\n"),
                        log.replace("10:25:39.", "10:25:40.")):
            self.log = bad_log
            with self.subTest(log=bad_log), self.assertRaisesRegex(rotation.RotationError, "checkout test merge этого PR"):
                self._review()
        self.log = log
        test_merge = self.responses[f"/git/commits/{self.test_sha}"]
        for field, value in (("parents", [{"sha": MAIN_SHA}, {"sha": self.head}]), ("tree", {"sha": MAIN_SHA})):
            with self.subTest(field=field), patch.dict(test_merge, {field: value}), \
                    self.assertRaisesRegex(rotation.RotationError, "не совпадает с родителями и деревом"):
                self._review()

    def test_rejects_untrusted_run_job_and_incomplete_metadata(self):
        """Требует конкретные workflow, job, попытку, приложение и время."""
        attempt = self.responses[self.run_path]
        for metadata, values in ((self.pull["head"], {"repo": None}),
                                 (attempt, {"workflow_id": 99}), (attempt, {"path": ".github/workflows/other.yml"}),
                                 (attempt, {"head_sha": MAIN_SHA}), (attempt, {"pull_requests": [{"number": 43}]}),
                                 (attempt, {"run_started_at": None}), (self.job, {"run_attempt": 2}),
                                 (self.check, {"app": None}), (self.check, {"check_suite": None}),
                                 (self.check, {"app": {"id": 999, "slug": "github-actions"}}),
                                 (self.check, {"check_suite": {"id": 999}})):
            with self.subTest(values=values), patch.dict(metadata, values), self.assertRaises(rotation.RotationError):
                self._review()
        self.api.job_log.assert_not_called()
        for path in (self.run_path, "/check-runs/50"):
            with self.subTest(path=path), patch.dict(self.responses, {path: None}), self.assertRaises(rotation.RotationError):
                self._review()
        for steps in (None, [{}], [1]):
            with self.subTest(steps=steps), patch.dict(self.job, {"steps": steps}), self.assertRaises(rotation.RotationError):
                self._review()

    def test_uses_last_attempt_before_merge(self):
        """Поздняя ошибка не перекрывается прежним успехом, повтор после merge не учитывается."""
        self.run["run_attempt"] = 2
        second_path = "/actions/runs/20/attempts/2"
        second = {**self.run, "run_started_at": "2026-11-10T10:36:00Z"}
        self.responses[second_path] = second
        self.assertEqual(self._review(), PLAN)
        second["run_started_at"] = "2026-11-10T10:25:34Z"
        second_job = {**self.job, "id": 51, "run_attempt": 2}
        self.pages[f"{second_path}/jobs"] = [second_job]
        self.responses["/check-runs/51"] = {**self.check, "id": 51}
        self.assertEqual(self._review(), PLAN)
        self.api.job_log.assert_called_with(51)
        # API не позволяет установить порядок окончания job и merge в одну секунду.
        for result in ({"conclusion": "failure"}, {"status": "in_progress", "completed_at": None},
                       {"completed_at": self.pull["merged_at"]}):
            with self.subTest(result=result), patch.dict(second_job, result), \
                    self.assertRaisesRegex(rotation.RotationError, "Последняя попытка"):
                self._review()
        second["run_started_at"] = self.responses[self.run_path]["run_started_at"]
        with self.assertRaisesRegex(rotation.RotationError, "последняя попытка"):
            self._review()

    def test_job_log_redirect_preserves_token_only_on_api_request(self):
        """HTTPS redirect хранилища получает запрос без GitHub token."""
        request = Request("https://api.github.com/repos/owner/repo/actions/jobs/50/logs")
        request.add_unredirected_header("Authorization", "Bearer example")
        handler = rotation._JobLogRedirect()
        redirect = handler.redirect_request(request, None, 302, "", {}, "https://logs.example/50")
        self.assertFalse(redirect.has_header("Authorization"))
        with self.assertRaisesRegex(rotation.RotationError, "незащищённую ссылку"):
            handler.redirect_request(request, None, 302, "", {}, "http://logs.example/50")
