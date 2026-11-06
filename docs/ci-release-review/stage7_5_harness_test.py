"""Проверяет process exit/WAL и закрытый отказ recovery без VPS."""

from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest


HERE = Path(__file__).resolve().parent


def load(name, filename):
    """Импортирует только локальный проверочный модуль."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline = load("stage7_3_tests", "stage7_3_harness_test.py")
harness = load("stage7_5_harness", "stage7_5_harness.py")


class PostAdmissionTests(unittest.TestCase):
    """Использует настоящую SQLite и новые child процессы с exact synthetic pair."""

    _setup = baseline.HarnessTests._setup
    _migrate = baseline.HarnessTests._migrate

    def test_crash_and_wal_and_negative_evidence(self):
        """Admission сохраняет new, WAL commit переживает exit, corruption закрывает start."""
        for fault in ("admission", "wal", "bad_evidence", "bad_state"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                config = self._setup(root, wal=True)
                source = root / "fixtures/source.db"
                with closing(sqlite3.connect(source)) as db:
                    baseline.migration_runner.apply_migrations(db)
                config["schema_before"] = config["schema_after"]
                config["source_digest"] = harness.digest(source)
                (root / "fault-name").write_text(fault)
                try:
                    first = harness.run_case(root, config, self._migrate)
                    second = harness.run_case(root, config, self._migrate)
                    third = harness.run_case(root, config, self._migrate)
                except Exception as error:
                    self.fail(str(error) + "\n" + (root / "controller.log").read_text(encoding="utf-8", errors="replace"))
                self.assertEqual(77, first["exit_code"])
                self.assertEqual(first["selector_sha256"], second["selector_sha256"])
                self.assertEqual(first["new_inode"], second["new_inode"])
                self.assertEqual(second["test_row_count"], third["test_row_count"])
                if fault.startswith("bad_"):
                    self.assertTrue(third["fail_closed"])
                    self.assertEqual(0, third["model_starts"])
                    self.assertEqual("degraded", third["stage"])
                else:
                    self.assertEqual("rolled_back", second["stage"])
                    self.assertEqual(second, third)
                    self.assertEqual(1 if fault == "wal" else 0, third["test_row_count"])


if __name__ == "__main__":
    unittest.main()
