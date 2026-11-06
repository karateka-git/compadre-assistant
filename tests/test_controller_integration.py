"""Проверяет совместимость внешнего состояния и обнаружение Docker writers."""

from contextlib import nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod


class ControllerIntegrationTests(unittest.TestCase):
    """Проверяет границы snapshot без обращения к VPS и реальному Docker."""

    def test_existing_state_marker_lock_and_runtime_are_not_migrated_by_read(self):
        """Legacy state, cooldown, marker и inode lock сохраняются побайтно."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            host = prod.Host(root / "checkout", root / "state")
            host.state_dir.mkdir()
            state = {"stage": "failed_preflight", "retry_after": "2099-01-01T00:00:00+00:00",
                     "last_good": {"version": "v3.1.0", "sha": "a" * 40}}
            prep.write_state(host.state_dir, host.checkout, state)
            prod._atomic_text(host.state_dir / "runtime.env", f"ASSISTANT_IMAGE=pin\nCOMPOSE_FILE={host.checkout / 'docker-compose.yml'}:{host.state_dir / 'pinned-compose.yml'}\n")
            for name, value in (("deploy.lock", b""), ("deploy-in-progress", b"critical\n")):
                (host.state_dir / name).write_bytes(value)
            paths = list(host.state_dir.iterdir())
            before = {path.name: (path.read_bytes(), path.stat().st_ino) for path in paths}
            self.assertEqual("pin", host.runtime_tag())
            self.assertEqual("failed_preflight", prep.read_state(host.state_dir, host.checkout)["stage"])
            with self.assertRaises(prod.DeployError):
                host.require_switch_rollout()
            self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_ino) for path in paths})
            self.assertFalse((host.state_dir / "active-db.json").exists())

    def test_foreign_data_mounts_include_ancestors_descendants_and_stopped_containers(self):
        """Writable checkout/data/file bind закрывает снимок даже без polling."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            host = prod.Host(root / "checkout", root / "state")
            for source in (host.checkout, host.checkout / "data", host.checkout / "data/app.db"):
                for running in (True, False):
                    item = {"State": {"Running": running}, "Mounts": [{"Source": str(source), "RW": True}]}
                    with self.subTest(source=source, running=running), patch.object(
                            host, "run", side_effect=["own\nforeign", json.dumps(item)]):
                        with self.assertRaises(prod.DeployError):
                            host.require_no_foreign_database_mounts({"own"})
            for source, writable in ((host.checkout / "data", False), (root / "separate", True)):
                item = {"Mounts": [{"Source": str(source), "RW": writable}]}
                with patch.object(host, "run", side_effect=["own\nforeign", json.dumps(item)]):
                    host.require_no_foreign_database_mounts({"own"})

    def test_foreign_mount_unknown_inventory_fails_closed(self):
        """Отсутствующий mount inventory не означает отсутствие writers."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            host = prod.Host(root / "checkout", root / "state")
            with patch.object(host, "run", side_effect=["foreign", "{}"]):
                with self.assertRaises(prod.DeployError):
                    host.require_no_foreign_database_mounts(set())


if __name__ == "__main__":
    unittest.main()
