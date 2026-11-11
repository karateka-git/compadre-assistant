"""Проверяет чтение завершённой очистки на временной SQLite с миграциями."""

from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations
from scripts import deploy_acceptance as acceptance
from scripts import deploy_backup as backup
from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deploy_runtime as runtime
from tests.test_deploy_acceptance import AcceptanceHost, SHA, TAG_OBJECT, VERSION
from tests.test_deploy_database_switch import IMAGE


MANIFEST = [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]


class CurrentSchemaHost(AcceptanceHost):
    """Проверяет окончательную копию по таблице тестовых записей."""

    def migrate_copy(self, image_id: str, directory: Path, *, basename: str = "app.db") -> None:
        """Имитирует неизменную миграцию на реальной временной SQLite."""
        self.commands.append(("migrate", str(directory / basename)))
        with closing(sqlite3.connect(directory / basename)) as connection:
            self.asserted_copy_rows = connection.execute("SELECT value FROM cleanup_probe").fetchall()


class CleanupContractTests(unittest.TestCase):
    """Проверяет, когда чтение может перестать требовать прежний файл."""

    def _fixture(self, root: Path) -> tuple[AcceptanceHost, dict]:
        """Строит успешный переход на БД с актуальными миграциями."""
        host = CurrentSchemaHost(root)
        initial = host.checkout / "data/app.db"
        initial.unlink()
        with closing(sqlite3.connect(initial)) as connection:
            apply_migrations(connection)
            connection.execute("CREATE TABLE cleanup_probe(id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO cleanup_probe VALUES(1, 'before')")
            connection.commit()
        old = switch.identity(initial, "legacy", MANIFEST)
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST, "controller_sha": "c" * 40,
                 "old_sha": "a" * 40, "old_image_id": "sha256:" + "1" * 64, "old_tag": "old-pin",
                 "compatibility_evidence": {"schema_fingerprint": old["fingerprint"]},
                 "timer_before": {"enabled": False, "active": False},
                 "candidate": {"image_id": IMAGE, "sha": SHA, "tag": prep.pinned_tag(SHA),
                               "version": VERSION, "release_id": 41, "run_id": 107, "run_attempt": 2,
                               "tag_object_sha": TAG_OBJECT, "kind": "ordinary"}}

        def record(stage: str, **changes: object) -> None:
            """Сохраняет очередную durable стадию тестового перехода."""
            state.update(changes, stage=stage)
            prep.write_state(host.state_dir, host.checkout, state)

        switch.register_initial(host, state, record, prod._atomic_text)
        switch.transition(host, state, record, prod._atomic_text)
        for item in host.found.values():
            item["State"].update(StartedAt="2026-11-09T12:00:00Z", Health={"Status": "healthy"})
        boot = runtime.boot_record(state, "success")
        host.marker(False)
        record("success", boot=boot, last_good={
            "version": VERSION, "sha": SHA, "image_id": IMAGE, "tag": prep.pinned_tag(SHA),
            "backup_id": state["snapshot"]["path"], "ci_run_id": 107,
            "ci_run_attempt": 2, "kind": "ordinary",
        })
        return host, state

    def _receipt(self, host: AcceptanceHost) -> dict:
        """Читает техническую приёмку без обращения к GitHub."""
        with (patch.object(acceptance, "_read_lock", return_value=nullcontext()),
              patch.object(prep, "require_exact_tag")):
            return acceptance.verify_installed(host, VERSION, SHA)

    def _record_cleanup(self, host: AcceptanceHost, state: dict) -> None:
        """Моделирует уже завершённое удаление только временного old."""
        Path(state["old"]["host_path"]).unlink()
        state["old_generation_cleanup"] = switch.old_cleanup_record(state)
        prep.write_state(host.state_dir, host.checkout, state)

    def test_completed_cleanup_keeps_acceptance_backup_and_next_deploy_readable(self) -> None:
        """После очистки читаются приёмка, ночная копия и следующий baseline."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            host, state = self._fixture(root)
            before = self._receipt(host)
            self._record_cleanup(host, state)
            self.assertEqual(switch.read_selected(host, state), "new")
            self.assertEqual(runtime.verify(host, state, require_running=True), state["boot"])
            self.assertEqual(self._receipt(host), before)
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                copied = backup.backup(host, root / "nightly")
            with closing(sqlite3.connect(copied)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM cleanup_probe").fetchall(),
                                 [("before",)])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            prod._recover(host, state)
            self.assertEqual(switch.read_selected(host, state), "new")
            self.assertTrue(Path(state["new"]["host_path"]).is_file())
            self.assertTrue(Path(state["snapshot"]["path"]).is_file())

    def test_missing_old_requires_exact_completed_record(self) -> None:
        """Без полной записи отсутствие old закрывает проверку."""
        for fault in ("missing", "partial", "bool_version", "wrong_transition",
                      "wrong_generation", "rolled_back"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                host, state = self._fixture(Path(temporary).resolve())
                self._record_cleanup(host, state)
                if fault == "missing":
                    del state["old_generation_cleanup"]
                elif fault == "partial":
                    del state["old_generation_cleanup"]["old_inode"]
                elif fault == "bool_version":
                    state["old_generation_cleanup"]["version"] = True
                elif fault == "wrong_transition":
                    state["old_generation_cleanup"]["transition_id"] = "f" * 32
                elif fault == "wrong_generation":
                    state["old_generation_cleanup"]["new_generation_id"] = "f" * 32
                else:
                    state["stage"] = "rolled_back"
                prep.write_state(host.state_dir, host.checkout, state)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    switch.read_selected(host, state)

    def test_record_never_hides_old_or_active_database_drift(self) -> None:
        """Преждевременный marker и потеря new закрывают допуск."""
        for fault in ("old_present", "old_sidecar", "selector_old", "new_missing"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                host, state = self._fixture(Path(temporary).resolve())
                self._record_cleanup(host, state)
                if fault == "old_present":
                    Path(state["old"]["host_path"]).write_bytes(b"unexpected")
                elif fault == "old_sidecar":
                    Path(state["old"]["host_path"] + "-wal").write_bytes(b"unexpected")
                elif fault == "selector_old":
                    prod._atomic_text(host.state_dir / "active-db.json", json.dumps(switch.selector(state, "old")))
                else:
                    Path(state["new"]["host_path"]).unlink()
                with self.assertRaises((ValueError, FileNotFoundError)):
                    switch.read_selected(host, state)


if __name__ == "__main__":
    unittest.main()
