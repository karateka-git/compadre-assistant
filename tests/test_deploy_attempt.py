"""Проверяет долговечную резервацию контроллера на временном состоянии VPS."""

from contextlib import contextmanager
from dataclasses import asdict
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import deploy_attempt as controller
from scripts.deploy_prod import DeployError
from tests.test_deployment_acceptance import RECEIPT
from tests.test_deployment_writer import BASE, WriterGitHub

VERSION = "v3.4.2"
SHA = "2" * 40
SNAPSHOT = "3" * 40
KEY = "12001:1"
RESUME = "13002:1"


class ControllerTests(unittest.TestCase):
    """Проверяет отказ другим владельцам между отдельными вызовами команды."""

    def setUp(self):
        """Моделирует Linux lock и root-владельца приватных файлов на Windows."""
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.api = WriterGitHub()
        selection = SimpleNamespace(sha=SHA, version=VERSION, release_id=42, run_id=108, run_attempt=1,
                                    compatibility=SimpleNamespace(baseline=BASE), kind="ordinary")
        self.host = SimpleNamespace(state_dir=directory, checkout=directory / "checkout",
                                    release_api=lambda: self.api, controller_sha=lambda: SNAPSHOT,
                                    select=lambda version: selection)
        self.locked = False
        self.states = {KEY: "in_progress", RESUME: "in_progress"}
        self.enterContext(patch.dict(os.environ, COMPADRE_DEPLOYMENT_READER="v2"))
        self.enterContext(patch.object(controller.compatibility, "private_text", lambda path: path.read_text()))
        self.enterContext(patch.object(controller.prep, "deploy_lock", self.lock))
        self.enterContext(patch.object(controller.records, "_check_attempt_release"))
        self.compare = self.enterContext(patch.object(controller, "compare_readers", return_value=BASE))
        self.enterContext(patch.object(controller.acceptance, "_verify_locked", return_value=asdict(RECEIPT)))
        self.old_get = self.api.get
        self.api.get = self.get

    def get(self, path, **kwargs):
        """Связывает статус запуска с конкретными run и attempt."""
        if path.startswith("/actions/runs/"):
            _, _, _, run, _, retry = path.split("/")
            repo = {"full_name": self.api.repository}
            return {"repository": repo, "head_repository": repo, "id": int(run), "run_attempt": int(retry),
                    "path": ".github/workflows/production-release.yml", "event": "workflow_dispatch",
                    "head_branch": "main", "status": self.states[f"{run}:{retry}"]}
        return self.old_get(path, **kwargs)

    @contextmanager
    def lock(self, *args):
        """Отклоняет вложенный и параллельный вход."""
        if self.locked:
            raise DeployError("занято")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def cmd(self, action, owner=KEY, key=KEY, number="0", snapshot=SNAPSHOT):
        """Обращается к обычному серверному входу."""
        return controller.execute(self.host, VERSION, action, SHA, key, owner, snapshot, number)

    def test_fence_survives_disconnect_and_owner_takeover(self):
        """Переоткрытие журнала после SSH не выдаёт старому runner право повторно."""
        first = self.cmd("reserve")
        self.assertEqual(first, self.cmd("reserve"))
        self.compare.assert_called_once()
        self.assertTrue(self.cmd("create")["granted"])
        with self.assertRaises(DeployError), controller.mutation_lock(self.host):
            self.fail()
        with self.assertRaises(DeployError):
            self.cmd("reserve", owner=RESUME, key=RESUME)
        with self.assertRaises(DeployError):
            self.cmd("resume", owner=RESUME)
        self.states[KEY] = "completed"
        self.assertEqual(first, self.cmd("resume", owner=RESUME))
        self.assertFalse(self.cmd("create", owner=RESUME)["granted"])
        with self.assertRaises(DeployError):
            self.cmd("create")

    def test_snapshot_and_live_base_checked_before_reservation(self):
        """Иной snapshot, разъезд читателей или миграции оставляет журнал пустым."""
        with self.assertRaises(DeployError):
            self.cmd("reserve", snapshot="4" * 40)
        with patch.object(controller, "compare_readers", return_value=None), self.assertRaises(DeployError):
            self.cmd("reserve")
        with patch.object(controller.acceptance, "_verify_locked", return_value=asdict(RECEIPT) | {"generation_id": "5"*32}):
            with self.assertRaises(DeployError):
                self.cmd("reserve")
        self.assertFalse((self.host.state_dir / controller.JOURNAL).exists())

    def test_install_intent_precedes_call_and_prevents_second_switch(self):
        """Обрыв установки оставляет started и право только на классификацию recovery."""
        attempt = self.cmd("reserve")["attempt"]
        self.cmd("create")
        self.api.deployments.append(self.api.latest | {"sha": SHA, "ref": SHA, "payload": attempt})
        self.api.records[11] = self.api.status | {"state": "in_progress", "description": f"v1 attempt={KEY}"}
        def lose_connection(host, version):
            self.assertTrue(self.locked)
            self.assertTrue(controller._read(host)["started"])
            with controller.mutation_lock(host):
                controller.require_current(host)
            raise ConnectionError
        with patch.object(controller.prod, "deploy", side_effect=lose_connection) as deploy:
            with self.assertRaises(ConnectionError):
                self.cmd("install", number="11")
            with self.assertRaises(DeployError):
                self.cmd("install", number="11")
            deploy.assert_called_once()
        self.api.records[11]["state"] = "failure"
        self.host.locked_attempt = (controller.records._attempt(self.api.deployments[-1]), 11)
        with self.assertRaises(DeployError):
            controller.require_current(self.host)

    def test_forced_command_accepts_only_protocol_argv(self):
        """Отвергает произвольный shell, лишние аргументы и сокращённые SHA."""
        path = Path(__file__).resolve().parents[1] / "deploy" / "attempt-ssh.py"
        command = runpy.run_path(str(path))["command"]
        safe = f"sudo -n /usr/local/sbin/deploy-prod {VERSION} --attempt reserve {SHA} {KEY} {KEY} {SNAPSHOT} 0"
        self.assertEqual(command(safe), safe.split())
        for other in (safe + " env", safe + "\n", safe.replace("reserve", "shell"), safe.replace(SHA, "$(pwd)"),
                      safe.replace("sudo -n", "sudo  -n"), "sudo -n /bin/sh"):
            with self.subTest(other=other), self.assertRaises(ValueError):
                command(other)
