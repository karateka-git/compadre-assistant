"""Проверяет пять отказов и повтор в новых guarded процессах без VPS."""

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


HERE = Path(__file__).resolve().parent


def load(name, filename):
    """Загружает проверочный модуль без запуска его CLI."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline = load("stage7_3_tests", "stage7_3_harness_test.py")
harness = load("stage7_4_harness", "stage7_4_harness.py")


class FaultHarnessTests(baseline.HarnessTests):
    """Повторно использует только synthetic setup и мигратор шага 7.3."""

    def test_five_failures_and_fresh_process_retry(self):
        """Отказы не доходят до admission; повтор сохраняет old и один model start."""
        for fault in ("preflight", "backup", "migration", "selector", "prepare"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                config = self._setup(root, wal=True)
                (root / "fault-name").write_text(fault)
                try:
                    first = harness.run_case(root, config, self._migrate)
                    second = harness.run_case(root, config, self._migrate)
                    third = harness.run_case(root, config, self._migrate)
                except Exception as error:
                    self.fail(str(error) + "\n" + (root / "controller.log").read_text(encoding="utf-8", errors="replace"))
                self.assertEqual("fault", first["phase"])
                self.assertEqual(first["lock_inode"], second["lock_inode"])
                self.assertEqual(first["old_sha256"], second["old_sha256"])
                self.assertEqual(second, third)
                self.assertEqual("failed_preflight" if fault == "preflight" else "rolled_back", second["stage"])


if __name__ == "__main__":
    unittest.main()
