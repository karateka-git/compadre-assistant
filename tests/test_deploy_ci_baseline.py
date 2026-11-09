"""Проверяет активацию CI baseline без GitHub и рабочего VPS."""

from contextlib import contextmanager, ExitStack
from dataclasses import asdict
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_ci_baseline as baseline
from scripts import deploy_prod as prod
from scripts import predeploy_compatibility as gate
from scripts import rotate_ci_baseline as rotation
from scripts.publish_installation_tag import InstallationReceipt
from tests.test_predeploy_compatibility import ArtifactAPI, CANDIDATE_SHA, POLICY_SHA


OLD_PIN = "d" * 40


class RotationAPI(ArtifactAPI):
    """Моделирует текущий GitHub pin, exact run и подлинные bytes evidence."""

    def __init__(self):
        """Связывает существующее evidence fixture с приёмкой и run ротации."""
        super().__init__()
        self.pin = POLICY_SHA
        self.run["event"] = "workflow_dispatch"
        self.run["display_title"] = rotation.RUN_TITLE_PREFIX + POLICY_SHA
        self.receipt = InstallationReceipt(1, self.policy["release_tag"], self.policy["previous_sha"],
                                           "ordinary", self.policy["release_id"], 5, 1,
                                           "1" * 32, "0004_schema.sql", "2" * 64)

    def get(self, path, **parameters):
        """Читает переменную, остальные ответы наследует из evidence fixture."""
        if path == f"/actions/variables/{rotation.PIN_NAME}":
            return {"name": rotation.PIN_NAME, "value": self.pin}
        return super().get(path, **parameters)


class DeployCIBaselineTest(unittest.TestCase):
    """Проверяет durable запись pin только после новой успешной проверки."""

    def setUp(self):
        """Размещает приватный pin и симулирует сериализацию с deploy."""
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.host = prod.Host(directory / "checkout", directory / "state")
        self.host.state_dir.mkdir()
        self.pin = self.host.state_dir / "compatibility-policy.sha"
        self.pin.write_text(OLD_PIN + "\n", encoding="utf-8")
        self.api = RotationAPI()
        self.locked = False

        @contextmanager
        def lock(state_dir, checkout):
            self.assertEqual((state_dir, checkout), (self.host.state_dir, self.host.checkout))
            self.assertFalse(self.locked)
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

        def read(path):
            self.assertTrue(self.locked)
            return self.pin.read_text(encoding="utf-8").strip() if path == self.pin else "read-token"

        def write(path, value):
            self.assertTrue(self.locked)
            prod._atomic_text(path, value)

        self.enterContext(patch.object(baseline.prep, "deploy_lock", side_effect=lock))
        self.enterContext(patch.object(gate, "private_text", side_effect=read))
        self.enterContext(patch.object(gate, "EvidenceAPI", return_value=self.api))
        self.write = self.enterContext(patch.object(baseline, "_atomic_text", side_effect=write))
        self.accepted = self.enterContext(patch.object(baseline, "require_accepted_installation"))
        self.receipt = self.enterContext(patch.object(baseline.deploy_acceptance, "_verify_locked",
                                                     return_value=asdict(self.api.receipt)))

    def activate(self):
        """Передаёт подтверждённые оператором реквизиты целевой активации."""
        baseline.activate(self.host, self.api.receipt.version, self.api.receipt.sha,
                          expected_old=OLD_PIN, policy_sha=POLICY_SHA, main_sha=CANDIDATE_SHA,
                          run_id=1, attempt=1, runtime_id=self.api.receipt.runtime_id)

    def test_activation_writes_pin_once_after_verified_ci_and_can_be_replayed(self):
        """Повторная активация заново проверяется и сохраняет тот же pin."""
        self.activate()
        self.activate()
        self.assertEqual(self.pin.read_text(encoding="utf-8").strip(), POLICY_SHA)
        self.write.assert_called_once_with(self.pin, POLICY_SHA + "\n")
        self.assertEqual(self.receipt.call_count, 4)
        self.accepted.assert_called_with(self.api, self.api.receipt)

    def test_failed_ci_old_green_and_api_error_keep_old_pin(self):
        """Успешный artifact старого pin и сбои CI оставляют запись прежней."""
        self.api.pin = OLD_PIN
        with self.assertRaisesRegex(rotation.RotationError, "GitHub pin"):
            self.activate()
        self.api.pin = POLICY_SHA
        self.api.run["conclusion"] = "failure"
        with self.assertRaisesRegex(rotation.RotationError, "успешная попытка"):
            self.activate()
        self.api.run["conclusion"] = "success"
        self.api.evidence["policy_sha"] = OLD_PIN
        self.api.pack()
        with self.assertRaisesRegex(gate.PredeployError, "SAFE_EVIDENCE_IDENTITY"):
            self.activate()
        with patch.object(self.api, "get", side_effect=OSError("GitHub недоступен")), self.assertRaises(OSError):
            self.activate()
        self.write.assert_not_called()
        self.assertEqual(self.pin.read_text(encoding="utf-8").strip(), OLD_PIN)

    def test_runtime_drift_under_lock_keeps_old_pin(self):
        """Изменение процессов во время проверки закрывает активацию."""
        before = asdict(self.api.receipt)
        self.receipt.side_effect = [before, {**before, "runtime_id": "3" * 64}]
        with self.assertRaisesRegex(prod.DeployError, "изменились в ходе проверки"):
            self.activate()
        self.write.assert_not_called()

    def test_cli_selects_rotation_without_installing_or_stopping(self):
        """Аргументы активации идут в отдельный режим команды VPS."""
        argv = ["deploy-prod", self.api.receipt.version, "--activate-ci-baseline", self.api.receipt.sha, OLD_PIN,
                POLICY_SHA, CANDIDATE_SHA, "1", "1", self.api.receipt.runtime_id]
        with ExitStack() as stack:
            for patcher in (patch.object(sys, "argv", argv), patch.object(prod.os, "name", "posix"),
                            patch.object(prod.os, "geteuid", return_value=0, create=True),
                            patch.object(prod, "Host", return_value=self.host), patch("builtins.print")):
                stack.enter_context(patcher)
            activate = stack.enter_context(patch.object(baseline, "activate"))
            deploy = stack.enter_context(patch.object(prod, "deploy"))
            prod.main()
        deploy.assert_not_called()
        activate.assert_called_once_with(self.host, self.api.receipt.version, self.api.receipt.sha,
                                         expected_old=OLD_PIN, policy_sha=POLICY_SHA, main_sha=CANDIDATE_SHA,
                                         run_id=1, attempt=1, runtime_id=self.api.receipt.runtime_id)
