"""Проверяет ночной backup legacy/v2 на отдельных настоящих SQLite."""

from contextlib import closing, nullcontext
import copy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_backup as backup
from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from tests.test_deploy_database_switch import SwitchHost, SHA, IMAGE, MANIFEST


class DeployBackupTests(unittest.TestCase):
    """Проверяет выбор поколения, общую блокировку и отказ без fallback."""

    def _fixture(self, root, v2=False):
        host = SwitchHost(root)
        (host.state_dir / "deploy-in-progress").unlink()
        good = {"sha": SHA, "image_id": IMAGE, "tag": prep.pinned_tag(SHA)}
        state = {"stage": "success", "last_good": good}
        host.runtime_tag = lambda: good["tag"]
        if v2:
            old = switch.identity(host.checkout / "data/app.db", "legacy", MANIFEST)
            generation = "d" * 32
            new_path = host.checkout / "data" / f"app-{generation}.db"
            prep.backup_sqlite(Path(old["host_path"]), new_path)
            with closing(sqlite3.connect(new_path)) as connection:
                connection.execute("INSERT INTO messages VALUES(2, 'new-generation')")
                connection.commit()
            state.update(protocol_version=2, transition_id="e" * 32, old=old,
                         new=switch.identity(new_path, generation, MANIFEST),
                         candidate=good, admission_may_have_started=True,
                         switch_intent={"from": "legacy", "to": generation},
                         admission={"transition_id": "e" * 32, "generation_id": generation,
                                    "image_id": IMAGE, "sha": SHA})
            prod._atomic_text(host.state_dir / "active-db.json", json.dumps(switch.selector(state, "new")))
        prep.write_state(host.state_dir, host.checkout, state)
        return host, state

    def test_backup_legacy_and_selected_v2_preserves_rows_and_source(self):
        """Backup v2 содержит новую запись; legacy app.db остаётся прежней."""
        for v2 in (False, True):
            with self.subTest(v2=v2), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                host, state = self._fixture(root, v2)
                before = (host.checkout / "data/app.db").read_bytes()
                with patch.object(prep, "deploy_lock", return_value=nullcontext()) as lock:
                    destination = backup.backup(host, root / "nightly")
                lock.assert_called_once_with(host.state_dir, host.checkout)
                with closing(sqlite3.connect(destination)) as connection:
                    self.assertEqual(2 if v2 else 1, connection.execute("SELECT count(*) FROM messages").fetchone()[0])
                    self.assertEqual([("ok",)], connection.execute("PRAGMA integrity_check").fetchall())
                    self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual(before, (host.checkout / "data/app.db").read_bytes())
                self.assertEqual(state, {key: value for key, value in prep.read_state(host.state_dir, host.checkout).items() if key != "updated_at"})

    def test_marker_stage_selector_and_pin_fail_without_backup(self):
        """Прямой CLI не обходит marker, неизвестную stage, selector или pin."""
        for fault in ("marker", "stage", "selector", "missing", "pin", "legacy-v2-files"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                host, state = self._fixture(root, True)
                if fault == "marker":
                    host.marker(True)
                elif fault == "stage":
                    state["stage"] = "recovery_verified"
                elif fault == "selector":
                    (host.state_dir / "active-db.json").write_text("{}", encoding="utf-8")
                elif fault == "missing":
                    (host.state_dir / "active-db.json").unlink()
                elif fault == "pin":
                    host.runtime_tag = lambda: "wrong-pin"
                else:
                    del state["protocol_version"]
                prep.write_state(host.state_dir, host.checkout, state)
                with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                    with self.assertRaises((prod.DeployError, ValueError, FileNotFoundError)):
                        backup.backup(host, root / "nightly")
                self.assertFalse((root / "nightly").exists())

    def test_held_lock_prevents_any_selection(self):
        """Занятый deploy lock закрывает backup до чтения выбранной SQLite."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            host, _ = self._fixture(root)
            with patch.object(prep, "deploy_lock", side_effect=prep.PreparationError("locked")):
                with self.assertRaises(prep.PreparationError):
                    backup.backup(host, root / "nightly")
            self.assertFalse((root / "nightly").exists())


if __name__ == "__main__":
    unittest.main()
