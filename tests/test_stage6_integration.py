"""Связывает switch/recovery/finalization/boot на SQLite с fake transport."""

from contextlib import closing, nullcontext
import copy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deploy_runtime as runtime
from tests import test_deploy_database_recovery as recovery
from tests.test_deploy_database_switch import IMAGE, SHA
from tests.deploy_fixture_paths import FixtureStatePath


class IntegrationHost(recovery.RecoveryHost):
    """Соединяет реальные state machines с изолированным Docker/systemd."""

    def require_switch_rollout(self):
        """Открывает только тестовый адаптер; production gate не изменяется."""

    def activate_unit(self):
        """Выполняет настоящий adoption под смоделированным занятым lock."""
        with patch.object(prep, "deploy_lock", side_effect=prep.PreparationError("locked")):
            runtime.start(self)

    def timer_restore(self, snapshot):
        """Сохраняет состояние timer в модели systemd."""
        self.restored_timer = copy.deepcopy(snapshot)


class Stage6IntegrationTests(unittest.TestCase):
    """Проверяет совместное поведение компонентов и пределы fake polling."""

    def setUp(self):
        """Оставляет чтение файлов, подменяя только POSIX permissions check."""
        mocked = patch.object(prod.compatibility, "private_text", side_effect=lambda path: path.read_text().strip())
        mocked.start()
        self.addCleanup(mocked.stop)

    def _fixture(self, root, stage):
        with patch.object(recovery, "RecoveryHost", IntegrationHost):
            host, state, record = recovery.DatabaseRecoveryTests()._setup(root, stage)
        state.update(timer_before={"enabled": True, "active": True}, last_good={
            "version": "v1.2.2", "sha": recovery.OLD_SHA,
            "image_id": recovery.OLD_IMAGE, "tag": prep.pinned_tag(recovery.OLD_SHA)})
        record(state["stage"])
        return host, state

    def _reload(self, host):
        # Конструктор fixture создаёт БД; после restart нужен только новый Host.
        restored = object.__new__(IntegrationHost)
        prod.Host.__init__(restored, host.checkout, host.state_dir)
        restored.state_dir = FixtureStatePath(restored.state_dir)
        for name in ("found", "commands", "selected", "serial", "starts", "bad_image"):
            setattr(restored, name, getattr(host, name))
        state = prep.read_state(restored.state_dir, restored.checkout)

        def record(stage, **changes):
            state.update(changes, stage=stage)
            prep.write_state(restored.state_dir, restored.checkout, state)

        return restored, state, record

    def _assert_terminal(self, host, state, side, outcome):
        expected_sha, expected_image = (SHA, IMAGE) if outcome == "success" else (recovery.OLD_SHA, recovery.OLD_IMAGE)
        self.assertEqual(outcome, state["stage"])
        self.assertEqual(side, switch.read_selected(host, state))
        self.assertEqual(expected_sha, state["boot"]["sha"])
        self.assertEqual(expected_image, state["boot"]["image_id"])
        self.assertEqual("c" * 40, state["controller_sha"])
        self.assertEqual({name: item["Id"] for name, item in host.found.items()}, state["boot"]["containers"])
        self.assertEqual({"enabled": True, "active": True}, host.restored_timer)
        self.assertFalse((host.state_dir / "deploy-in-progress").exists())
        runtime.verify(host, state, require_running=True)
        before = list(host.commands)
        with patch.object(prep, "deploy_lock", return_value=nullcontext()):
            runtime.start(host)
            runtime.start(host)
        self.assertEqual(before, host.commands)

    def test_success_and_controller_reload_keep_exact_pair_and_rows(self):
        """Успех заканчивается новым pin; повтор boot не создаёт и не стартует пару."""
        with TemporaryDirectory() as temporary:
            host, _ = self._fixture(Path(temporary).resolve(), "candidate_started")
            host, state, record = self._reload(host)
            old_bytes = Path(state["old"]["host_path"]).read_bytes()
            runtime.finalize(host, state, record, "success")
            self._assert_terminal(host, state, "new", "success")
            self.assertEqual(1, host.starts)
            self.assertEqual(old_bytes, Path(state["old"]["host_path"]).read_bytes())
            with closing(sqlite3.connect(state["new"]["host_path"])) as db:
                self.assertEqual([("original",)], db.execute("SELECT text FROM messages").fetchall())

    def test_before_and_after_admission_recovery_finalize_and_repeat(self):
        """До true возвращается old; после true new/WAL переживает recovery и boot."""
        for stage in ("database_selected", "admission_recorded", "candidate_started"):
            with self.subTest(stage=stage), TemporaryDirectory() as temporary:
                host, state = self._fixture(Path(temporary).resolve(), stage)
                admitted = state["admission_may_have_started"]
                old_bytes = Path(state["old"]["host_path"]).read_bytes()
                with closing(sqlite3.connect(state["new"]["host_path"])) as db:
                    if stage == "candidate_started":
                        db.execute("PRAGMA journal_mode=WAL")
                        db.execute("PRAGMA wal_autocheckpoint=0")
                        db.execute("INSERT INTO messages VALUES(2, 'accepted')")
                        db.commit()
                    host, state, record = self._reload(host)
                    with patch.object(prod.compatibility, "verify_evidence", return_value=state["compatibility_evidence"]) as evidence:
                        prod._recover(host, state)
                        runtime.finalize(host, state, record, "rolled_back")
                        self._assert_terminal(host, state, "new" if admitted else "old", "rolled_back")
                    self.assertEqual(4 if admitted else 0, evidence.call_count)
                    for call in evidence.call_args_list:
                        self.assertEqual((host.selected, "c" * 40, recovery.OLD_SHA), call.args[1:])
                    self.assertEqual(admitted, state["admission_may_have_started"])
                    self.assertEqual(old_bytes, Path(state["old"]["host_path"]).read_bytes())
                    expected = [("original",), ("accepted",)] if stage == "candidate_started" else [("original",)]
                    self.assertEqual(expected, db.execute("SELECT text FROM messages ORDER BY id").fetchall())
                    self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])
                    self.assertEqual([], db.execute("PRAGMA foreign_key_check").fetchall())

    def test_restart_during_finalization_resumes_durable_intent(self):
        """Отдельный Python-процесс завершает intent только по сохранённым файлам."""
        with TemporaryDirectory() as temporary:
            host, _ = self._fixture(Path(temporary).resolve(), "candidate_started")
            host, state, record = self._reload(host)
            with patch.object(host, "activate_unit", side_effect=recovery.Crash()):
                with self.assertRaises(recovery.Crash):
                    runtime.finalize(host, state, record, "success")
            saved_boot = copy.deepcopy(state["boot"])
            commands = list(host.commands)
            transport = Path(temporary) / "transport.json"
            transport.write_text(json.dumps({"found": host.found, "selected": asdict(host.selected),
                "serial": host.serial, "starts": host.starts, "bad_image": host.bad_image}), encoding="utf-8")
            # Новый процесс получает модель внешнего Docker, но не объект state/record.
            code = """
import json, sys
from pathlib import Path
from scripts import deploy_prod as prod, deploy_prepare as prep, deploy_runtime as runtime
from scripts.select_deploy_release import SelectedRelease
from tests.test_stage6_integration import IntegrationHost
from tests.deploy_fixture_paths import FixtureStatePath
root = Path(sys.argv[1])
values = json.loads((root / 'transport.json').read_text(encoding='utf-8'))
host = object.__new__(IntegrationHost)
prod.Host.__init__(host, root / 'checkout', root / 'state')
host.state_dir = FixtureStatePath(host.state_dir)
host.__dict__.update(values)
host.selected = SelectedRelease(**values['selected'])
host.commands = []
state = prep.read_state(host.state_dir, host.checkout)
prod._recover(host, state)
runtime.verify(host, state, require_running=True)
assert not host.commands, 'restart created or started containers'
assert host.restored_timer == {'enabled': True, 'active': True}
assert state['stage'] == 'success'
"""
            result = subprocess.run([sys.executable, "-B", "-c", code, str(Path(temporary).resolve())],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            state = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual("success", state["stage"])
            self.assertFalse((host.state_dir / "deploy-in-progress").exists())
            self.assertEqual(saved_boot, state["boot"])
            self.assertEqual(commands, host.commands)


if __name__ == "__main__":
    unittest.main()
