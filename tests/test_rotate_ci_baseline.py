"""Проверяет операторские шаги ротации без GitHub и production VPS."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
