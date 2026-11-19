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
        self.cmd("in_progress", number="11")
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

    def test_reconcile_needs_completed_owner_and_matching_unapplied_post(self):
        """Оператор снимает только последнее отклонённое намерение завершённого owner."""
        self.cmd("reserve")
        self.cmd("create")
        def reconcile(action):
            controller.reconcile_post(self.host, VERSION, SHA, KEY, SNAPSHOT, action)
        with self.assertRaises(DeployError):
            reconcile("create")
        self.states[KEY] = "completed"
        with self.assertRaises(DeployError):
            reconcile("cancel")
        reconcile("create")
        reconcile("cancel")
        self.assertTrue(controller._read(self.host)["closed"])

    def test_runner_resumes_lost_cleanup_response_with_new_workflow_id(self):
        """Оба протокола проходят два сеанса с одной установкой и одной очисткой."""
        self._runner_crash("complete")

    def test_resume_after_creation_without_status(self):
        """Потеря runner после POST создания не порождает второй D18."""
        self._runner_crash("create")

    def test_resume_after_in_progress_before_install(self):
        """При отсутствии серверного старта новый owner начинает установку один раз."""
        self._runner_crash("in_progress")

    def test_resume_after_install(self):
        """Уже установленный runtime проверяется и очищается без нового install."""
        self._runner_crash("install")

    def test_resume_after_success_before_close(self):
        """Потеря ответа success оставляет тот же числовой статус."""
        self._runner_crash("success")

    def test_resume_after_terminal_journal_write(self):
        """Повтор закрытого журнала требует свежей приёмки того же runtime."""
        self._runner_crash("close")

    def _runner_crash(self, interruption):
        from dataclasses import replace
        import json
        from scripts import release_production as runner

        lost = True
        def interrupt(point):
            nonlocal lost
            if point == interruption and lost:
                lost = False
                raise InterruptedError("Остановка runner")

        selected = self.host.select(VERSION)
        receipt = replace(RECEIPT, version=VERSION, sha=SHA, release_id=42, run_id=108, run_attempt=1,
                          generation_id="9" * 32, runtime_id="8" * 64)
        self.enterContext(patch.object(controller.acceptance, "_verify_locked",
                                      side_effect=lambda host, ver, sha: asdict(receipt if ver == VERSION else RECEIPT)))
        self.enterContext(patch.object(controller.cleanup, "_snapshot"))
        self.enterContext(patch.object(runner, "_select_or_publish", return_value=selected))
        old_post = self.api.post
        def post(path, body):
            result = old_post(path, body)
            if path == "/deployments":
                self.api.deployments[-1].update(sha=SHA, ref=SHA)
                interrupt("create")
            else:
                interrupt(body["state"])
            return result
        self.api.post = post
        original_get = self.api.get
        self.api.get = lambda path, **kwargs: original_get(path, **({"ref": BASE.sha} if
            path.startswith("/contents/") and kwargs.get("ref") == SHA else kwargs))
        def install(host, version):
            attempt, number = host.locked_attempt
            self.assertEqual(self.api.records[number]["state"], "in_progress")
            controller.prep.write_state(host.state_dir, host.checkout, {
                "stage": "success", "last_good": {"version": version, "sha": SHA},
                "deployment_attempt": [attempt.attempt_key, number], "candidate": {"sha": SHA},
                "transition_id": "a" * 32, "old": {"device": 1, "inode": 10, "host_path": "/data/app-old.db",
                "generation_id": RECEIPT.generation_id}, "new": {"generation_id": receipt.generation_id}})
            return SHA
        def clean(host, version, sha, runtime):
            state = controller.prep.read_state(host.state_dir, host.checkout)
            state["old_generation_cleanup"] = controller.prod.database_switch.old_cleanup_record(state)
            controller.prep.write_state(host.state_dir, host.checkout, state)
        installs = self.enterContext(patch.object(controller.prod, "deploy", side_effect=install))
        cleans = self.enterContext(patch.object(controller.cleanup, "cleanup_old", side_effect=clean))
        def ssh(argv, **kwargs):
            _, _, _, version, _, action, *arguments = argv
            result = controller.execute(self.host, version, action, *arguments)
            if action in ("complete", "install", "close"):
                interrupt(action)
            return SimpleNamespace(stdout=json.dumps(result), stderr="")
        self.enterContext(patch.object(runner, "run_vps_command", side_effect=ssh))
        settings = {"COMPADRE_CONTROLLER_SHA": SNAPSHOT, "COMPADRE_DEPLOYMENT_WRITER": "v2",
                    "GITHUB_RUN_ID": KEY.split(":")[0], "GITHUB_RUN_ATTEMPT": "1", "RELEASE_SOURCE_ATTEMPT": ""}
        with patch.dict(os.environ, settings):
            with self.assertRaisesRegex(InterruptedError, "runner"):
                runner._run_release(self.api, VERSION, SHA, change="patch", notes="patch",
                                    ssh_host="vps", known_hosts=Path("hosts"), identity=None)
        self.states[KEY] = "completed"
        settings.update(GITHUB_RUN_ID=RESUME.split(":")[0], RELEASE_SOURCE_ATTEMPT=KEY,
                        COMPADRE_DEPLOYMENT_WRITER="resume-only")
        with patch.dict(os.environ, settings):
            for _ in range(2):
                result = runner._run_release(self.api, VERSION, SHA, change=None, notes=None,
                                             ssh_host="vps", known_hosts=Path("hosts"), identity=None)
                self.assertEqual(result.state.value, "success")
        self.assertEqual(self.api.posts, ["/deployments", "/deployments/11/statuses", "/deployments/11/statuses"])
        installs.assert_called_once()
        cleans.assert_called_once()
        self.assertTrue(controller._read(self.host)["closed"])

    def test_refuses_unknown_state_and_closes_only_proven_return(self):
        """Прерванная установка завершается failure только с подтверждённой базой."""
        attempt = self.cmd("reserve")["attempt"]
        self.cmd("create")
        self.api.deployments.append(self.api.latest | {"sha": SHA, "ref": SHA, "payload": attempt})
        self.cmd("in_progress", number="11")
        self.api.records[11] = self.api.status | {"state": "in_progress", "description": f"v1 attempt={KEY}"}
        with patch.object(controller.prod, "deploy", side_effect=ConnectionError), self.assertRaises(ConnectionError):
            self.cmd("install", number="11")
        with self.assertRaises(DeployError):
            self.cmd("complete", number="11")
        controller.prep.write_state(self.host.state_dir, self.host.checkout, {"stage": "success",
                                    "last_good": {"version": BASE.version, "sha": BASE.sha}})
        with patch.object(controller.acceptance, "_verify_locked", return_value=asdict(RECEIPT) | {"generation_id": "8"*32}):
            with self.assertRaises(DeployError):
                self.cmd("complete", number="11")
        self.assertEqual(self.cmd("complete", number="11")["outcome"], "restored")
        self.assertTrue(self.cmd("finish", number="11")["granted"])
        self.api.records[11].update(state="failure", description="v1 restored=7/9")
        with self.assertRaises(DeployError):
            self.cmd("close", number="11")
        self.api.records[11]["description"] = "v1 restored=7/8"
        self.assertEqual(self.cmd("close", number="11")["outcome"], "restored")

    def test_wrong_resume_arguments_stop_before_publication(self):
        """Запрет новых попыток и чужой ключ проверяются ещё до подготовки тега."""
        from scripts import release_production as runner
        cases = (("resume-only", "patch", ""), ("v2", None, ""), ("v2", "patch", KEY),
                 ("v2", None, "0001:02"), ("v1", None, KEY))
        for mode, change, source in cases:
            with self.subTest(mode=mode, change=change, source=source), patch.dict(
                    os.environ, COMPADRE_DEPLOYMENT_WRITER=mode, RELEASE_SOURCE_ATTEMPT=source,
                    COMPADRE_CONTROLLER_SHA=SNAPSHOT, GITHUB_RUN_ID="13002", GITHUB_RUN_ATTEMPT="1"):
                with patch.object(runner, "_select_or_publish") as publish, self.assertRaises(runner.ProductionReleaseError):
                    runner._run_release(self.api, VERSION, SHA, change=change, notes=None,
                                        ssh_host="vps", known_hosts=Path("hosts"), identity=None)
                publish.assert_not_called()

    def test_old_image_on_new_generation_cannot_prove_restoration(self):
        """Даже штатный boot прежнего образа не доказывает возврат после допуска на новую БД."""
        from dataclasses import replace

        state = {"stage": "rolled_back", "last_good": {"image_id": "old-image", "sha": BASE.sha,
                 "version": BASE.version, "ci_run_id": RECEIPT.run_id, "ci_run_attempt": RECEIPT.run_attempt},
                 "admission_may_have_started": False, "old": {"manifest": [RECEIPT.migration]},
                 "timer_before": {"enabled": True, "active": True}}
        controller.prep.write_state(self.host.state_dir, self.host.checkout, state)
        state = controller.prep.read_state(self.host.state_dir, self.host.checkout)
        self.host.select = lambda version: SimpleNamespace(**asdict(RECEIPT), tag_object_sha="3" * 40)
        self.host.timer_snapshot = lambda: state["timer_before"]
        self.host.unit_active = lambda unit: True
        self.enterContext(patch.object(controller.prep, "require_clean_checkout"))
        self.enterContext(patch.object(controller.prep, "require_exact_tag"))
        self.enterContext(patch.object(controller.acceptance, "_container_identity", return_value={"tg": ["id", "ts"]}))
        boot = {"outcome": "rolled_back", "sha": BASE.sha, "image_id": "old-image",
                "generation_id": RECEIPT.generation_id}
        self.enterContext(patch.object(controller.runtime, "verify", return_value=boot))
        actual = controller._rolled_back(self.host, state, RECEIPT)
        self.assertEqual(replace(actual, runtime_id=RECEIPT.runtime_id), RECEIPT)
        for change in ({"generation_id": "a" * 32}, {"outcome": "success"}):
            with patch.dict(boot, change), self.assertRaises(DeployError):
                controller._rolled_back(self.host, state, RECEIPT)
        with patch.dict(state, admission_may_have_started=True), self.assertRaises(DeployError):
            controller._rolled_back(self.host, state, RECEIPT)


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "проверка требует изолированного Linux с root-owned состоянием")
class LinuxOwnershipTests(unittest.TestCase):
    """Проверяет реальный flock и root-only журнал в двух Python-процессах."""

    def test_process_exit_preserves_reservation(self):
        """После выхода владельца flock свободен, но другой deploy запрещён журналом."""
        import subprocess
        import sys
        from tests.test_deployment_writer import ATTEMPT

        with TemporaryDirectory() as directory:
            root = Path(directory)
            host = SimpleNamespace(checkout=root / "checkout", state_dir=root / "state")
            code = (
                "from pathlib import Path\n"
                "from types import SimpleNamespace\n"
                "from scripts.deploy_attempt import mutation_lock\n"
                "from scripts.deploy_prepare import PreparationError\n"
                "from scripts.deploy_prod import DeployError\n"
                "import sys\n"
                "root = Path(sys.argv[1])\n"
                "host = SimpleNamespace(checkout=root/'checkout', state_dir=root/'state')\n"
                "try:\n"
                "    with mutation_lock(host): print('доступ')\n"
                "except (PreparationError, DeployError) as error: print(str(error))\n"
            )
            def probe():
                return subprocess.run((sys.executable, "-B", "-c", code, directory),
                                      capture_output=True, encoding="utf-8", check=True).stdout
            with controller.prep.deploy_lock(host.state_dir, host.checkout):
                self.assertIn("Уже выполняется", probe())
                controller._save(host, {"protocol": 2, "attempt": asdict(ATTEMPT), "owner": KEY,
                    "snapshot": SNAPSHOT, "base_receipt": asdict(RECEIPT), "claims": [], "deployment_id": None,
                    "started": False, "closed": False})
            self.assertEqual((host.state_dir / controller.JOURNAL).stat().st_mode & 0o777, 0o600)
            self.assertIn("владеет открытая попытка", probe())
