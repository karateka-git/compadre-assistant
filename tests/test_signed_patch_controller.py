"""Проверяет подписанный PATCH через штатный CLI, SSH-парсер и общий журнал VPS."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import deploy_attempt
from tests import test_patch_http_baseline as fixture
from tests.test_signed_patch_runner import SignedPatchAPI


class Controller(fixture._V2ControllerFixture):
    """Соединяет подписанный runner с ограниченным SSH и CLI контроллера на стенде."""

    def execute(self, arguments, **options):
        """Передаёт реальные аргументы и байты через парсер root-dispatcher."""
        parser = Path(__file__).resolve().parents[1] / "deploy" / "attempt-ssh.py"
        frame = " ".join(arguments).encode("ascii") + b"\0" + (options.get("input_text") or "").encode("ascii")
        reply = []

        def run(argv, *, input, env):
            if tuple(argv) != arguments[2:] or env != {"PATH": "/usr/bin:/bin"}:
                raise AssertionError("argv или env SSH-диспетчера отличаются")
            response = super(Controller, self).execute(arguments, input_text=input.decode("ascii"))
            reply.append(response)
            return response

        dispatch = runpy.run_path(str(parser))["dispatch"]
        with patch.dict(dispatch.__globals__, subprocess=SimpleNamespace(run=run)):
            if dispatch(frame) != 0:
                raise AssertionError("Ошибка SSH-диспетчера")
        return reply[0]


class PatchControllerTests(unittest.TestCase):
    """Проверяет переключение и фиксацию принятой базы под контролем VPS."""

    def setUp(self):
        """Готовит отдельные Git, SQLite и одноразовый тестовый ключ."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        graph, self.runner, fingerprint = fixture._fixture(self.root)
        self.api = SignedPatchAPI(graph, fingerprint, fixture.HISTORY_SIZE)
        self.keys = self.api.configure_signer(self.root, fingerprint)
        self.enterContext(patch.dict(os.environ, RELEASE_SIGNER_ATTEMPT="99:1", PRODUCTION_SIGNED_PATCH="true",
                                     PATCH_ALLOWED_SIGNERS_FILE=str(self.keys), COMPATIBILITY_POLICY_SHA=fixture.POLICY_SHA))
        self.enterContext(patch.object(fixture.system, "OLD_SHA", graph.base))
        self.enterContext(patch.object(fixture.system, "NEW_SHA", graph.candidate))
        self.controller = Controller(self.root / "vps", self.api)
        (self.controller.host.state_dir / "patch-allowed-signers").write_bytes(self.keys.read_bytes())

    def seed_acceptance(self):
        """Восстанавливает локальную запись закрытия предыдущего принятого PATCH."""
        base = self.api._baseline()
        receipt = self.controller._verify(self.controller.host, base.version, base.sha)
        previous = self.api.deployments[base.deployment_id]["payload"]
        record = {"protocol": 2, "closed": True, "started": True, "deployment_id": base.deployment_id,
                  "claims": ["create", "in_progress", "finish"], "attempt": previous, "owner": previous["attempt_key"],
                  "snapshot": fixture.SNAPSHOT, "base_receipt": receipt, "result": {"outcome": "success", "receipt": receipt},
                  "patch": {"terminal": base.status_id, "accepted": {"baseline": asdict(base), "receipt": receipt}}}
        self.journal_path.write_text(json.dumps(record), encoding="utf8")

    @property
    def journal_path(self):
        """Возвращает путь временному журналу контроллера."""
        return self.controller.host.state_dir / deploy_attempt.JOURNAL

    def measure(self, *, resume=False, error=None):
        """Запускает Production release с привязкой к тому же тестовому VPS."""
        return fixture._measure(self.root, "signed", self.api, self.runner, installed=resume,
                                controller=self.controller, expected_error=error)

    def test_normal_patch(self):
        """Реальный CLI подтверждает новый runtime, очистку и ID принятого Deployment."""
        self.seed_acceptance()
        self.measure()
        journal = json.loads(self.journal_path.read_bytes())
        self.assertTrue(journal["closed"])
        self.assertEqual(journal["patch"]["accepted"]["baseline"]["status_id"], journal["patch"]["terminal"])
        self.assertEqual(self.api.status_writes, ["in_progress", "success"])
        self.assertEqual((self.controller.switches, self.controller.cleanup_count), (1, 1))
        self.assertEqual(sum(n for (loc, _, _), n in self.api.calls.items() if loc == "vps"), 4)
