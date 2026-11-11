"""Проверяет удаление только старой временной SQLite после приёмки."""

from contextlib import closing, nullcontext
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_acceptance as acceptance
from scripts import deploy_cleanup_old as cleanup
from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from tests.test_deploy_acceptance import SHA, VERSION
from tests.test_deploy_cleanup_contract import CleanupContractTests


class CleanupOldTests(unittest.TestCase):
    """Испытывает адресный успешный маршрут и безопасный повтор."""

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
            selector = host.state_dir / "active-db.json"
            selector_before = selector.read_bytes()
            result = self._run(host, before)
            self.assertEqual(result, switch.old_cleanup_record(state))
            self.assertFalse(old.exists())
            self.assertTrue(new.is_file())
            self.assertTrue(snapshot.is_file())
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


if __name__ == "__main__":
    unittest.main()
