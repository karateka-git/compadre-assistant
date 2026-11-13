"""Проверяет окончательный снимок на временной SQLite с миграциями проекта."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations
from compadre_assistant.core.data.sqlite.runtime_schema import schema_fingerprint
from scripts import deploy_final_snapshot as final
from scripts import deploy_prod as prod
from scripts.predeploy_compatibility import PredeployError


class FinalSnapshotTests(unittest.TestCase):
    """Проверяет изоляцию, сохранение WAL и отказ до выбора поколения."""

    def test_prepare_with_real_migrations_and_failures(self):
        """Настоящий runner мигрирует копию, а selector и исходные байты сохраняются."""
        scripts = sorted(MIGRATIONS_DIR.glob("*.sql"))
        before = [path.name for path in scripts[:2]]
        expected = [path.name for path in scripts]
        run_cli = subprocess.run
        for mode in ("success", "writer", "snapshot_state", "migration", "foreign",
                     "journal", "fingerprint", "data", "integrity", "late_writer", "collision",
                     "directory_sync"):
            with self.subTest(mode=mode), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                data = root / "data"
                state = root / "state"
                migrations = root / "old-migrations"
                for directory in (data, state, migrations):
                    directory.mkdir()
                for script in scripts[:2]:
                    (migrations / script.name).write_bytes(script.read_bytes())
                source = data / "app.db"
                selector = state / "active-db.json"
                selector.write_text(json.dumps({"basename": source.name}), encoding="utf-8")
                recorded = []
                checks = []
                generation = uuid4().hex
                candidate = data / f"app-{generation}.db"
                if mode == "collision":
                    candidate.write_bytes(b"existing")
                with closing(sqlite3.connect(":memory:")) as target:
                    apply_migrations(target)
                    fingerprint = schema_fingerprint(target)
                with closing(sqlite3.connect(source)) as writer:
                    writer.execute("PRAGMA journal_mode=WAL")
                    writer.execute("PRAGMA wal_autocheckpoint=0")
                    apply_migrations(writer, migrations)
                    writer.execute("INSERT INTO app_users(display_name) VALUES ('WAL row')")
                    writer.commit()
                    self.assertGreater((data / "app.db-wal").stat().st_size, 0)
                    old = {path: path.read_bytes() for path in (
                        source, data / "app.db-wal", selector)}

                    def quiescence():
                        checks.append(True)
                        if mode == "writer" or (mode == "late_writer" and len(checks) == 2):
                            raise RuntimeError("writer not stopped")

                    def record(snapshot):
                        recorded.append(snapshot)
                        self.assertEqual(hashlib.sha256(snapshot.path.read_bytes()).hexdigest(),
                                         snapshot.sha256)
                        self.assertFalse(stat.S_IMODE(snapshot.path.stat().st_mode) & stat.S_IWUSR)
                        if mode == "snapshot_state":
                            raise OSError("state write failed")

                    def docker(command, **kwargs):
                        if command[1] == "rm":
                            return SimpleNamespace(returncode=0)
                        self.assertIn("--read-only", command)
                        self.assertIn("none", command)
                        self.assertNotIn("--env-file", command)
                        volume = command[command.index("-v") + 1]
                        staging = Path(volume.removesuffix(":/app/data:rw"))
                        self.assertEqual(list(staging.iterdir()), [staging / candidate.name])
                        self.assertEqual(command[-4:], ["compadre_assistant", "--migrate",
                                                         "--database-path", f"/app/data/{candidate.name}"])
                        self.assertIn("sha256:" + "a" * 64, command)
                        cli = run_cli([sys.executable, "-m", *command[-4:-1], str(staging / candidate.name)],
                                      capture_output=True, check=False, timeout=30)
                        self.assertEqual(cli.returncode, 0, cli.stderr.decode(errors="replace"))
                        with closing(sqlite3.connect(staging / candidate.name)) as copy:
                            if mode == "data":
                                copy.execute("UPDATE app_users SET display_name='changed'")
                            if mode == "journal":
                                copy.execute("DELETE FROM schema_migrations WHERE version = "
                                             "(SELECT MAX(version) FROM schema_migrations)")
                            if mode == "foreign":
                                copy.execute("INSERT INTO user_capability_preferences "
                                             "(app_user_id, capability_id, enabled) VALUES (999, 'ask', 1)")
                            copy.commit()
                        if mode == "integrity":
                            (staging / candidate.name).write_bytes(b"invalid SQLite")
                        return SimpleNamespace(returncode=int(mode == "migration"))

                    def migrate(path):
                        host = prod.Host()
                        with patch.object(prod.subprocess, "run", side_effect=docker):
                            host.migrate_copy("sha256:" + "a" * 64, path.parent, basename=path.name)

                    def sync_directory(directory):
                        if mode == "directory_sync" and directory == data:
                            raise OSError("directory fsync failed")

                    arguments = dict(require_quiescence=quiescence, migrate=migrate, record_snapshot=record)
                    target_fingerprint = "0" * 64 if mode == "fingerprint" else fingerprint
                    if mode == "success":
                        self.assertEqual(final.prepare(source, state, generation, before, expected,
                                                       target_fingerprint, **arguments), candidate)
                        with closing(sqlite3.connect(candidate)) as result:
                            self.assertEqual(result.execute("SELECT display_name FROM app_users").fetchall(),
                                             [("WAL row",)])
                            self.assertEqual(schema_fingerprint(result), fingerprint)
                    else:
                        codes = {"writer": "SAFE_FINAL_PREPARATION",
                                 "snapshot_state": "SAFE_FINAL_PREPARATION",
                                 "migration": "SAFE_FINAL_MIGRATION", "foreign": "SAFE_COPY_FOREIGN_KEYS",
                                 "journal": "SAFE_COPY_CANDIDATE_SCHEMA", "fingerprint": "SAFE_COPY_FINGERPRINT",
                                 "data": "SAFE_COPY_DATA_LOSS", "integrity": "SAFE_FINAL_PREPARATION",
                                 "late_writer": "SAFE_FINAL_PREPARATION",
                                 "collision": "SAFE_FINAL_NAME_COLLISION", "directory_sync": "SAFE_FINAL_PREPARATION"}
                        with (patch.object(final, "_sync_directory", side_effect=sync_directory),
                              self.assertRaisesRegex(PredeployError, codes[mode])):
                            final.prepare(source, state, generation, before, expected,
                                          target_fingerprint, **arguments)
                        if mode == "collision":
                            self.assertEqual(candidate.read_bytes(), b"existing")
                        elif mode == "directory_sync":
                            self.assertTrue(candidate.is_file())
                        else:
                            self.assertFalse(candidate.exists())
                    self.assertEqual(old, {path: path.read_bytes() for path in old})
                    self.assertEqual(list(data.glob(".candidate-*")), [])
                    self.assertEqual(list(state.glob(".final-*")), [])
                    self.assertEqual(len(recorded), int(mode != "writer"))
                    for snapshot in recorded:
                        with closing(sqlite3.connect(f"{snapshot.path.as_uri()}?mode=ro", uri=True)) as saved:
                            self.assertEqual(saved.execute("SELECT display_name FROM app_users").fetchall(),
                                             [("WAL row",)])
                            self.assertEqual(saved.execute("SELECT name FROM schema_migrations ORDER BY version")
                                             .fetchall(), [(name,) for name in before])
                        os.chmod(snapshot.path, 0o600)


    def test_host_requires_both_stopped_and_backup_inactive(self):
        """Host отклоняет активный writer, restart policy, unit, timer и отсутствие marker."""
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            marker = state / "deploy-in-progress"
            host = prod.Host(state_dir=state)
            for refusal in (None, "marker", "running", "restart", "timer_enabled",
                            prod.SERVICE, prod.TIMER, prod.BACKUP_SERVICE):
                with self.subTest(refusal=refusal):
                    marker.write_text("unexpected" if refusal == "marker" else "critical\n",
                                      encoding="utf-8")
                    containers = {
                        "tg": {"Id": "tg-id", "State": {"Running": False},
                               "HostConfig": {"RestartPolicy": {"Name": "no"}}},
                        "vk": {"Id": "vk-id", "State": {"Running": refusal == "running"},
                               "HostConfig": {"RestartPolicy": {
                                   "Name": "unless-stopped" if refusal == "restart" else "no"}}},
                    }
                    with (patch.object(host, "unit_state", side_effect=lambda unit:
                                       "active" if unit == refusal else "inactive"),
                          patch.object(host, "unit_enabled", return_value=refusal == "timer_enabled"),
                          patch.object(host, "require_no_foreign_database_mounts"),
                          patch.object(host, "containers", return_value=containers)):
                        if refusal is None:
                            host.require_snapshot_quiescence()
                        else:
                            with self.assertRaises(prod.DeployError):
                                host.require_snapshot_quiescence()

    def test_publication_refuses_a_name_created_after_precheck(self):
        """Атомарная публикация сохраняет файл, появившийся после проверки имени."""
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            staged = directory / "stage.db"
            target = directory / "candidate.db"
            staged.write_bytes(b"candidate")
            primitive = "link" if os.name == "posix" else "rename"
            publish = getattr(os, primitive)

            def competing_publish(source, destination):
                destination.write_bytes(b"pre-existing")
                return publish(source, destination)

            with (patch.object(os, primitive, side_effect=competing_publish),
                  self.assertRaisesRegex(PredeployError, "SAFE_FINAL_NAME_COLLISION")):
                final._publish(staged, target)
            self.assertEqual(target.read_bytes(), b"pre-existing")
            self.assertEqual(staged.read_bytes(), b"candidate")
