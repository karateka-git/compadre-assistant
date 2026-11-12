"""Проверяет удаление только старой временной SQLite после приёмки."""

from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_acceptance as acceptance
from scripts import deploy_cleanup_old as cleanup
from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts.deploy_prod import DeployError
from tests.test_deploy_acceptance import SHA, VERSION
from tests.test_deploy_cleanup_contract import CleanupContractTests


def _can_scan_linux_fds() -> bool:
    """Проверяет доступ к FD всех видимых процессов для real-gate теста."""
    if os.name != "posix" or not Path("/proc/self/fd").is_dir() or os.geteuid() != 0:
        return False
    try:
        with os.scandir("/proc") as processes:
            for process in processes:
                if process.name.isdigit():
                    try:
                        with os.scandir(Path(process.path) / "fd"):
                            pass
                    except FileNotFoundError:
                        continue
    except OSError:
        return False
    return True


class CleanupOldTests(unittest.TestCase):
    """Испытывает адресный успешный маршрут и безопасный повтор."""

    def setUp(self) -> None:
        """Изолирует системный `/proc` gate от остальных проверок fixture."""
        self._fd_gate = patch.object(cleanup, "_require_no_open_old")
        self._fd_gate.start()
        self.addCleanup(self._fd_gate.stop)

    def _fixture(self, root: Path):
        """Создаёт два настоящих поколения SQLite и принятую установку."""
        return CleanupContractTests()._fixture(root)

    def _accepted(self, host) -> str:
        """Получает runtime_id из локальной технической приёмки."""
        return CleanupContractTests()._receipt(host)["runtime_id"]

    def _run(self, host, runtime_id: str) -> dict:
        """Запускает контроллер под модельным lock только на временном хосте."""
        with (patch.object(prep, "deploy_lock", return_value=nullcontext()),
              patch.object(prep, "require_exact_tag")):
            return cleanup.cleanup_old(host, VERSION, SHA, runtime_id)

    def test_removes_only_old_and_keeps_backup_active_and_receipt(self) -> None:
        """Удаление сохраняет текущие записи, snapshot и ту же приёмку."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            before = self._accepted(host)
            old = Path(state["old"]["host_path"])
            new = Path(state["new"]["host_path"])
            snapshot = Path(state["snapshot"]["path"])
            snapshot_before = snapshot.read_bytes()
            selector = host.state_dir / "active-db.json"
            selector_before = selector.read_bytes()
            result = self._run(host, before)
            self.assertEqual(result, switch.old_cleanup_record(state))
            self.assertFalse(old.exists())
            self.assertTrue(new.is_file())
            self.assertTrue(snapshot.is_file())
            self.assertEqual(snapshot.read_bytes(), snapshot_before)
            self.assertEqual(selector.read_bytes(), selector_before)
            with closing(sqlite3.connect(new)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM cleanup_probe").fetchall(),
                                 [("before",)])
            self.assertEqual(self._accepted(host), before)
            self.assertEqual(self._run(host, before), result)

    def test_resume_after_old_unlink_uses_exact_durable_intent(self) -> None:
        """Потеря завершения после unlink не удаляет новое поколение."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            runtime_id = self._accepted(host)
            old = Path(state["old"]["host_path"])
            original_write = prep.write_state
            calls = 0

            def interrupt_after_unlink(directory, checkout, value):
                """Имитирует crash перед durable завершением после удаления."""
                nonlocal calls
                calls += 1
                if calls == 2:
                    self.assertFalse(old.exists())
                    raise OSError("interrupted before completion")
                return original_write(directory, checkout, value)

            with (patch.object(prep, "deploy_lock", return_value=nullcontext()),
                  patch.object(prep, "require_exact_tag"),
                  patch.object(prep, "write_state", side_effect=interrupt_after_unlink)):
                with self.assertRaises(OSError):
                    cleanup.cleanup_old(host, VERSION, SHA, runtime_id)
            self.assertFalse(old.exists())
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertIn("old_generation_cleanup_intent", durable)
            self.assertNotIn("old_generation_cleanup", durable)
            self.assertEqual(self._run(host, runtime_id), switch.old_cleanup_record(state))
            self.assertTrue(Path(state["new"]["host_path"]).is_file())

    def test_wrong_runtime_stops_before_intent_and_unlink(self) -> None:
        """Чужое живое подтверждение не создаёт запись и не удаляет old."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            with self.assertRaises(cleanup.CleanupError):
                self._run(host, "f" * 64)
            self.assertTrue(Path(state["old"]["host_path"]).is_file())
            self.assertNotIn("old_generation_cleanup_intent",
                             prep.read_state(host.state_dir, host.checkout))

    def test_resume_after_sidecar_unlink(self) -> None:
        """Повтор после удаления sidecar завершает очистку по точному intent."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            old = Path(state["old"]["host_path"])
            sidecar = Path(str(old) + "-shm")
            sidecar.write_bytes(b"stale sidecar")
            runtime_id = self._accepted(host)
            original_unlink = Path.unlink

            def interrupt_sidecar(path, *args, **kwargs):
                """Имитирует crash после unlink первого зарегистрированного файла."""
                result = original_unlink(path, *args, **kwargs)
                if path == sidecar:
                    raise OSError("interrupted after sidecar unlink")
                return result

            with patch.object(Path, "unlink", interrupt_sidecar):
                with self.assertRaises(OSError):
                    self._run(host, runtime_id)
            self.assertFalse(sidecar.exists())
            self.assertTrue(old.exists())
            self.assertIn("old_generation_cleanup_intent",
                          prep.read_state(host.state_dir, host.checkout))
            self.assertEqual(self._run(host, runtime_id), switch.old_cleanup_record(state))
            self.assertFalse(old.exists())

    def test_failure_before_intent_never_unlinks_old(self) -> None:
        """Отказ durable-записи намерения оставляет оба поколения на месте."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            runtime_id = self._accepted(host)
            old = Path(state["old"]["host_path"])
            new = Path(state["new"]["host_path"])
            with (patch.object(prep, "deploy_lock", return_value=nullcontext()),
                  patch.object(prep, "require_exact_tag"),
                  patch.object(prep, "write_state", side_effect=OSError("intent not durable"))):
                with self.assertRaises(OSError):
                    cleanup.cleanup_old(host, VERSION, SHA, runtime_id)
            self.assertTrue(old.is_file())
            self.assertTrue(new.is_file())
            self.assertNotIn("old_generation_cleanup_intent",
                             prep.read_state(host.state_dir, host.checkout))

    @unittest.skipUnless(_can_scan_linux_fds(),
                         "Нужен Linux /proc с правами для просмотра чужих FD")
    def test_linux_open_old_fd_refuses_before_intent_then_retries(self) -> None:
        """Открытый old FD запрещает intent и допускает повтор после закрытия."""
        self._fd_gate.stop()
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            runtime_id = self._accepted(host)
            old = Path(state["old"]["host_path"])
            new = Path(state["new"]["host_path"])
            backup = Path(state["snapshot"]["path"])
            new_before, backup_before = new.read_bytes(), backup.read_bytes()
            with old.open("rb"):
                with self.assertRaisesRegex(cleanup.CleanupError, "SQLite ещё открыта"):
                    self._run(host, runtime_id)
                self.assertTrue(old.is_file())
                self.assertNotIn("old_generation_cleanup_intent",
                                 prep.read_state(host.state_dir, host.checkout))
                self.assertEqual(new.read_bytes(), new_before)
                self.assertEqual(backup.read_bytes(), backup_before)
            self.assertEqual(self._run(host, runtime_id), switch.old_cleanup_record(state))
            self.assertFalse(old.exists())
            self.assertEqual(new.read_bytes(), new_before)
            self.assertEqual(backup.read_bytes(), backup_before)

    def test_failure_after_unlink_before_directory_sync_resumes(self) -> None:
        """Повтор после unlink без подтверждённого fsync читает точный intent."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            runtime_id = self._accepted(host)
            old = Path(state["old"]["host_path"])
            with patch.object(cleanup, "_fsync_dir", side_effect=OSError("directory sync failed")):
                with self.assertRaises(OSError):
                    self._run(host, runtime_id)
            self.assertFalse(old.exists())
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertIn("old_generation_cleanup_intent", durable)
            self.assertNotIn("old_generation_cleanup", durable)
            self.assertEqual(self._run(host, runtime_id), switch.old_cleanup_record(state))
            self.assertTrue(Path(state["snapshot"]["path"]).is_file())

    def test_rejects_precleanup_faults_without_deleting_old(self) -> None:
        """Неоднозначное состояние, selector, backup и active DB закрывают unlink."""
        faults = (
            "selector_old", "old_replaced", "snapshot_missing", "snapshot_corrupt",
            "stage_rolled_back", "stage_degraded", "stage_unknown", "marker",
            "recovery_blocked", "admission_false", "new_missing",
        )
        for fault in faults:
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                host, state = self._fixture(Path(temporary).resolve())
                runtime_id = self._accepted(host)
                old = Path(state["old"]["host_path"])
                new = Path(state["new"]["host_path"])
                backup = Path(state["snapshot"]["path"])
                new_before = new.read_bytes()
                if fault == "selector_old":
                    (host.state_dir / "active-db.json").write_text(
                        json.dumps(switch.selector(state, "old")), encoding="utf-8",
                    )
                elif fault == "old_replaced":
                    old_bytes = old.read_bytes()
                    old.rename(old.with_name(old.name + ".original"))
                    old.write_bytes(old_bytes)
                elif fault == "snapshot_missing":
                    backup.chmod(0o600)
                    backup.unlink()
                elif fault == "snapshot_corrupt":
                    backup.chmod(0o600)
                    backup.write_bytes(b"corrupt")
                elif fault.startswith("stage_"):
                    state["stage"] = fault.removeprefix("stage_")
                    prep.write_state(host.state_dir, host.checkout, state)
                elif fault == "marker":
                    (host.state_dir / "deploy-in-progress").write_text("blocked", encoding="utf-8")
                elif fault == "recovery_blocked":
                    (host.state_dir / "recovery-blocked.json").write_text("{}", encoding="utf-8")
                elif fault == "admission_false":
                    state["admission_may_have_started"] = False
                    prep.write_state(host.state_dir, host.checkout, state)
                else:
                    new.unlink()
                with self.assertRaises((DeployError, ValueError, FileNotFoundError,
                                        sqlite3.DatabaseError)):
                    self._run(host, runtime_id)
                self.assertTrue(old.is_file())
                self.assertNotIn("old_generation_cleanup_intent",
                                 prep.read_state(host.state_dir, host.checkout))
                if fault != "new_missing":
                    self.assertEqual(new.read_bytes(), new_before)
                if fault not in ("snapshot_missing", "snapshot_corrupt"):
                    self.assertTrue(backup.is_file())

    def test_intent_rejects_new_or_replaced_sidecar(self) -> None:
        """Повтор не принимает новый sidecar или inode, отличный от intent."""
        for fault in ("new_sidecar", "replaced_sidecar", "replaced_old"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                host, state = self._fixture(Path(temporary).resolve())
                runtime_id = self._accepted(host)
                old = Path(state["old"]["host_path"])
                sidecar = Path(str(old) + "-shm")
                if fault == "replaced_sidecar":
                    sidecar.write_bytes(b"original")
                state["old_generation_cleanup_intent"] = cleanup._intent(host, state, runtime_id)
                prep.write_state(host.state_dir, host.checkout, state)
                if fault == "new_sidecar":
                    sidecar.write_bytes(b"new")
                elif fault == "replaced_sidecar":
                    sidecar.rename(sidecar.with_name(sidecar.name + ".original"))
                    sidecar.write_bytes(b"replacement")
                else:
                    old_bytes = old.read_bytes()
                    old.rename(old.with_name(old.name + ".original"))
                    old.write_bytes(old_bytes)
                with self.assertRaises(cleanup.CleanupError):
                    self._run(host, runtime_id)
                self.assertTrue(old.is_file())
                self.assertTrue(Path(state["new"]["host_path"]).is_file())
                self.assertTrue(Path(state["snapshot"]["path"]).is_file())

    def test_completion_and_intent_together_refuse_read_and_repeat(self) -> None:
        """Противоречивые durable записи не подтверждают завершение очистки."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            runtime_id = self._accepted(host)
            self._run(host, runtime_id)
            durable = prep.read_state(host.state_dir, host.checkout)
            durable["old_generation_cleanup_intent"] = switch.old_cleanup_record(durable) | {
                "runtime_id": runtime_id, "sidecars": {},
            }
            prep.write_state(host.state_dir, host.checkout, durable)
            with self.assertRaises(ValueError):
                switch.read_selected(host, durable)
            with self.assertRaises(ValueError):
                self._run(host, runtime_id)
            self.assertTrue(Path(state["new"]["host_path"]).is_file())
            self.assertTrue(Path(state["snapshot"]["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
