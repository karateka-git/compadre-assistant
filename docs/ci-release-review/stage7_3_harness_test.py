"""Локальная synthetic проверка IPC harness без Docker, VPS или production SQLite."""

from contextlib import closing
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import uuid


HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path.insert(0, str(REPOSITORY))
spec = importlib.util.spec_from_file_location("stage7_harness", HERE / "stage7_3_harness.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
from compadre_assistant.core.data.sqlite import migration_runner, runtime_schema


class HarnessTests(unittest.TestCase):
    """Применяет настоящие SQL на synthetic SQLite, моделируя только broker transport."""

    def _setup(self, root, *, wal=False):
        (root / "fixtures").mkdir(mode=0o700)
        sql = root / "previous-sql"
        sql.mkdir()
        migrations = migration_runner._discover_migrations(migration_runner.MIGRATIONS_DIR)
        shutil.copyfile(migrations[0].path, sql / migrations[0].name)
        source = root / "fixtures/source.db"
        with closing(sqlite3.connect(source)) as connection:
            if wal:
                self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            migration_runner.apply_migrations(connection, sql)
            connection.execute("INSERT INTO app_users(display_name) VALUES('synthetic seed')")
            connection.commit()
        current = root / "current.db"
        shutil.copyfile(source, current)
        with closing(sqlite3.connect(current)) as connection:
            migration_runner.apply_migrations(connection)
            fingerprint = runtime_schema.schema_fingerprint(connection)
        config = {"candidate_sha": harness.CANDIDATE, "previous_sha": harness.PREVIOUS, "run_id": harness.CI_RUN,
            "run_attempt": 1, "full_ci_success": True, "repository": "karateka-git/compadre-assistant", "synthetic": True,
            "source_digest": harness.digest(source), "image_id": "sha256:" + "a" * 64,
            "previous_image_id": "sha256:" + "b" * 64, "schema_fingerprint": fingerprint,
            "snapshot_digest": "c" * 64, "policy_sha": "d" * 40, "artifact_digest": "sha256:" + "e" * 64,
            "artifact_id": 1, "nonce": uuid.uuid4().hex, "control": str(REPOSITORY),
            "schema_before": [migrations[0].name], "schema_after": [item.name for item in migrations]}
        return config

    def _migrate(self, root, database, image, sha):
        self.assertEqual(sha, harness.CANDIDATE)
        self.assertEqual(image, "sha256:" + "a" * 64)
        with closing(sqlite3.connect(database)) as connection:
            migration_runner.apply_migrations(connection)

    def test_real_guarded_child_and_two_rpc_migrations_complete(self):
        """Настоящие preflight/final snapshot/switch/write/boot работают с pipe broker."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = self._setup(root)
            try:
                result = harness.run_case(root, config, self._migrate)
            except ValueError as error:
                self.fail(str(error) + "\n" + (root / "controller.log").read_text(encoding="utf-8"))
            self.assertTrue(result["synthetic"])
            self.assertTrue(result["row_committed"])
            self.assertEqual(4, result["guard_checks"])
            self.assertEqual(2, result["migrations"])
            self.assertEqual(1, result["model_starts"])
            self.assertEqual(config["source_digest"], harness.digest(root / "fixtures/source.db"))

    def test_admission_and_every_request_identity_fail_before_broker(self):
        """Нет fallback для неверных SHA/image/nonce/sequence/path/inode/hash/op."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = self._setup(root)
            database = root / "scenarios/happy/preflight/candidate/app.db"
            database.parent.mkdir(parents=True)
            shutil.copyfile(root / "fixtures/source.db", database)
            request = {"operation": "migrate", "receipt": harness.receipt_id(config), "sequence": 1,
                "path": str(database), "device": database.stat().st_dev, "inode": database.stat().st_ino,
                "before_digest": harness.digest(database), "image_id": config["image_id"], "candidate_sha": harness.CANDIDATE}
            for key, bad in (("operation", "docker start"), ("receipt", "wrong"), ("sequence", 2),
                ("path", str(root / "fixtures/source.db")), ("image_id", "sha256:" + "f" * 64),
                ("candidate_sha", "f" * 40), ("inode", database.stat().st_ino + 1), ("before_digest", "0" * 64)):
                with self.subTest(key=key), patch.object(self, "_migrate") as broker:
                    changed = request | {key: bad}
                    with self.assertRaises(ValueError):
                        harness.serve_request(root, config, changed, 1, broker)
                    broker.assert_not_called()
            for key, bad in (("full_ci_success", False), ("candidate_sha", "f" * 40), ("artifact_digest", "")):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    harness.validate(config | {key: bad}, root)

    def test_wal_source_stays_closed_through_full_controller_case(self):
        """WAL-mode fixture без sidecars не открывается SQLite даже при setup."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = self._setup(root, wal=True)
            source = root / "fixtures/source.db"
            self.assertEqual(source.read_bytes()[18:20], b"\x02\x02")
            before = (source.stat().st_size, source.stat().st_mtime_ns, harness.digest(source))
            self.assertFalse(any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")))
            try:
                result = harness.run_case(root, config, self._migrate)
            except ValueError as error:
                self.fail(str(error) + "\n" + (root / "controller.log").read_text(encoding="utf-8"))
            self.assertTrue(result["row_committed"])
            self.assertEqual(result["migrations"], 2)
            self.assertEqual(result["model_starts"], 1)
            self.assertEqual(before, (source.stat().st_size, source.stat().st_mtime_ns, harness.digest(source)))
            self.assertFalse(any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")))
            harness.validate(config, root)

    def test_stale_response_and_failed_migration_never_complete(self):
        """Отказ parent и подмена результата закрывают child до selector/admission."""
        for failure in ("response", "migration"):
            with self.subTest(failure=failure), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                config = self._setup(root)
                serve = harness.serve_request

                def altered(*args):
                    value = serve(*args)
                    return value | {"after_digest": "0" * 64}

                with patch.object(harness, "serve_request", side_effect=altered if failure == "response" else ValueError("failed")):
                    with self.assertRaises(ValueError):
                        harness.run_case(root, config, self._migrate)
                self.assertFalse((root / "result.json").exists())
                self.assertFalse((root / "scenarios/happy/state/active-db.json").exists())
                self.assertEqual(config["source_digest"], harness.digest(root / "fixtures/source.db"))

    def test_real_receipt_requires_reviewed_bytes_and_binding(self):
        """Самозаявленный full_ci_success не заменяет reviewed native receipt."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = self._setup(root)
            with self.assertRaisesRegex(ValueError, "NATIVE_RECEIPT_BYTES"):
                harness.validate_native_receipts(root, config)
            ci = {key: config[key] for key in ("candidate_sha", "previous_sha", "run_id", "run_attempt", "full_ci_success",
                    "schema_fingerprint", "snapshot_digest", "policy_sha", "artifact_digest", "artifact_id")}
            ci["linux_native_verifier"] = True
            image = {"candidate_sha": config["candidate_sha"], "oci_revision": harness.CANDIDATE, "image_id": config["image_id"]}
            synthetic = {"stage": "success", "real_flock": True, "real_polling": False}
            config["previous_image_id"] = "sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8"
            for name, value in zip(harness.NATIVE_RECEIPTS, (ci, image, synthetic)):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            pins = {name: harness.digest(root / name) for name in harness.NATIVE_RECEIPTS}
            with patch.object(harness, "NATIVE_RECEIPTS", pins):
                harness.validate_native_receipts(root, config)
                for key, value in (("artifact_id", config["artifact_id"] + 1), ("schema_fingerprint", "0" * 64),
                                   ("image_id", "sha256:" + "0" * 64), ("full_ci_success", False)):
                    with self.subTest(key=key), self.assertRaisesRegex(ValueError, "NATIVE_RECEIPT_BINDING"):
                        harness.validate_native_receipts(root, config | {key: value})
                (root / "ci-receipt.json").write_text(json.dumps(ci | {"full_ci_success": False}))
                with self.assertRaisesRegex(ValueError, "NATIVE_RECEIPT_BYTES"):
                    harness.validate_native_receipts(root, config)


if __name__ == "__main__":
    unittest.main()
