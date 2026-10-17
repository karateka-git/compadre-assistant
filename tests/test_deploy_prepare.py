"""Проверяет подготовку выпуска без доступа к Docker, VPS и production SQLite."""

from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_prepare as prepare


SHA = "a" * 40


class DeployPrepareTests(unittest.TestCase):
    """Использует только временные каталоги и искусственные SQLite-БД."""

    def test_state_is_atomic_private_and_outside_checkout(self) -> None:
        """Состояние не попадает в checkout и переживает повторную запись."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            state_dir = root / "state"
            prepare.write_state(state_dir, checkout, {"stage": "building", "old_image_id": "sha256:old"})
            self.assertEqual(prepare.read_state(state_dir, checkout)["old_image_id"], "sha256:old")
            prepare.write_state(state_dir, checkout, {"stage": "backup_verified"})
            self.assertEqual(prepare.read_state(state_dir, checkout)["stage"], "backup_verified")
            self.assertEqual(list(state_dir.glob(".state-*")), [])
            with self.assertRaises(prepare.PreparationError):
                prepare.write_state(checkout / "state", checkout, {})

    @unittest.skipUnless(os.name == "posix", "flock проверяется только на Linux")
    def test_second_deploy_lock_is_rejected_without_removing_lock_file(self) -> None:
        """Второй процесс не получает lock, пока первый держит inode."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            state = root / "state"
            with prepare.deploy_lock(state, checkout):
                with self.assertRaises(prepare.PreparationError):
                    with prepare.deploy_lock(state, checkout):
                        self.fail("Второй запуск получил lock")
            self.assertTrue((state / "deploy.lock").is_file())

    def test_backup_preserves_wal_data_and_validates_copy(self) -> None:
        """SQLite backup API сохраняет подтверждённые строки из WAL."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "live.db"
            destination = root / "release-backups" / "before.db"
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample VALUES ('saved')")
                connection.commit()
                self.assertEqual(prepare.backup_sqlite(source, destination), destination)
            with closing(sqlite3.connect(destination)) as copy:
                self.assertEqual(copy.execute("SELECT value FROM sample").fetchall(), [("saved",)])
                self.assertEqual(copy.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            with self.assertRaises(prepare.PreparationError):
                prepare.backup_sqlite(source, destination)

    def test_schema_validation_requires_exact_migration_list(self) -> None:
        """Неизвестная или неполная схема не принимается после миграции."""
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE schema_migrations (version INTEGER, name TEXT)")
                connection.execute("INSERT INTO schema_migrations VALUES (1, '0001_initial_schema.sql')")
                connection.commit()
            prepare.validate_schema(database, ["0001_initial_schema.sql"])
            with self.assertRaises(prepare.PreparationError):
                prepare.validate_schema(database, ["0001_initial_schema.sql", "0002_extra.sql"])

    def test_build_rejects_wrong_revision_label(self) -> None:
        """Сборка не принимает образ с подменённым OCI revision."""
        image = {"Id": "sha256:" + "b" * 64, "Config": {"Labels": {prepare.OCI_REVISION: "c" * 40}}}
        with patch.object(prepare, "require_clean_checkout"), patch.object(
            prepare, "_run", side_effect=["", json.dumps(image)],
        ):
            with self.assertRaises(prepare.PreparationError):
                prepare.build_pinned_image(Path("/fake"), SHA)

    def test_checkout_does_not_treat_git_error_as_detached(self) -> None:
        """Сбой проверки branch не должен допускать сборку как detached HEAD."""
        branch = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="error")
        with patch.object(prepare, "_run", return_value=""), patch.object(
            prepare.subprocess, "run", return_value=branch,
        ):
            with self.assertRaisesRegex(prepare.PreparationError, "detached HEAD"):
                prepare.require_clean_checkout(Path("/fake"), SHA)

    def test_candidate_check_runs_without_network_or_secrets(self) -> None:
        """Synthetic checks запускаются с изоляцией сети и файловой системы."""
        image_id = "sha256:" + "b" * 64
        image = {"Id": image_id, "Config": {"Labels": {prepare.OCI_REVISION: SHA}}}
        with patch.object(prepare, "_run", side_effect=[json.dumps(image), "", "", ""]) as run:
            prepare.check_candidate_image(image_id, SHA)
        self.assertEqual(run.call_count, 4)
        for call in run.call_args_list[1:]:
            args = call.args
            self.assertIn("none", args)
            self.assertIn("--read-only", args)
            self.assertNotIn("--env-file", args)
            self.assertIn(image_id, args)


if __name__ == "__main__":
    unittest.main()
