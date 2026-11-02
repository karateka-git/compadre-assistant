"""Проверяет точные sources, SQL-only подстановку и границы imports runner 5.2."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import check_repository_compatibility as runner

from scripts.check_repository_compatibility import (
    MIGRATIONS, export_commit, overlay_sql, run_worker, snapshot,
)
from scripts.repository_compatibility_worker import CheckoutImports


class RepositoryCompatibilityRunnerTest(unittest.TestCase):
    """Проверяет механизм на временных деревьях без изменения выпущенных tests."""

    def setUp(self) -> None:
        """Подготавливает две независимые одноразовые копии."""
        self.temporary = TemporaryDirectory(prefix="compatibility-runner-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.previous = self.root / "previous"
        self.candidate = self.root / "candidate"
        for checkout in (self.previous, self.candidate):
            (checkout / MIGRATIONS).mkdir(parents=True)
            (checkout / MIGRATIONS / "0001_initial.sql").write_bytes(b"CREATE TABLE fixture(id INTEGER);\n")
            (checkout / "tests").mkdir()
            (checkout / "tests/helper.py").write_bytes(b"VALUE = 'previous'\r\n")

    def test_overlay_preserves_python_and_exact_candidate_sql(self) -> None:
        """Новая SQL заменяет источник, сохраняя даже CRLF старого helper."""
        original = snapshot(self.previous)
        sql = b"ALTER TABLE fixture ADD COLUMN name TEXT;\n"
        (self.candidate / MIGRATIONS / "0002_extra.sql").write_bytes(sql)
        (self.candidate / "tests/helper.py").write_text("raise AssertionError('candidate leaked')\n")
        expected = overlay_sql(self.previous, self.candidate, original)
        self.assertEqual(snapshot(self.previous), expected)
        self.assertEqual((self.previous / "tests/helper.py").read_bytes(), b"VALUE = 'previous'\r\n")
        self.assertEqual((self.previous / MIGRATIONS / "0002_extra.sql").read_bytes(), sql)

    def test_overlay_preserves_previous_migrations_package_init(self) -> None:
        """Tracked __init__.py является старым Python и не копируется у кандидата."""
        (self.previous / MIGRATIONS / "__init__.py").write_bytes(b"# old\n")
        (self.candidate / MIGRATIONS / "__init__.py").write_bytes(b"# candidate\n")
        original = snapshot(self.previous)
        overlay_sql(self.previous, self.candidate, original)
        self.assertEqual((self.previous / MIGRATIONS / "__init__.py").read_bytes(), b"# old\n")

    def test_overlay_refuses_changed_previous_python(self) -> None:
        """Изменённые tests нельзя выдать за bytes релиза."""
        original = snapshot(self.previous)
        (self.previous / "tests/helper.py").write_text("VALUE = 'changed'\n")
        with self.assertRaisesRegex(ValueError, "changed before"):
            overlay_sql(self.previous, self.candidate, original)

    def test_overlay_refuses_non_sql_without_mutating_previous(self) -> None:
        """Посторонний Python в source миграций останавливает подстановку."""
        original = snapshot(self.previous)
        (self.candidate / MIGRATIONS / "helper.py").write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "only SQL"):
            overlay_sql(self.previous, self.candidate, original)
        self.assertEqual(snapshot(self.previous), original)

    def test_guard_rejects_candidate_and_installed_project_before_import(self) -> None:
        """Проверяет origins package, test и helper без исполнения чужого кода."""
        guard = CheckoutImports(self.previous)
        for name in ("compadre_assistant.core", "tests.test_example", "helper"):
            with self.subTest(name=name), self.assertRaisesRegex(ImportError, "leakage"):
                guard.check(name, str(self.candidate / "tests/helper.py"))
        guard.check("helper", str(self.previous / "tests/helper.py"))
        with patch.object(sys, "path", [str(self.candidate / "tests"), *sys.path]):
            with self.assertRaisesRegex(ImportError, "leakage"):
                guard.find_spec("helper")

    def test_candidate_failure_does_not_suppress_old_behavior_result(self) -> None:
        """Отказ candidate набора сохраняется отдельно и не отменяет old worker."""
        modes = []

        def export(repository, sha, destination):
            (destination / MIGRATIONS).mkdir(parents=True)
            (destination / MIGRATIONS / "0001_initial.sql").write_text("SELECT 1;\n")
            return snapshot(destination)

        def worker(python, checkout, mode, output, extra, timeout):
            modes.append(mode)
            return {"passed": mode != "candidate"}

        output = self.root / "separate-results"
        with patch.object(sys, "argv", ["runner", "--previous-sha", "a" * 40,
                                       "--candidate-sha", "b" * 40, "--output", str(output)]), \
                patch.object(runner, "export_commit", side_effect=export), \
                patch.object(runner, "create_environment", return_value=Path(sys.executable)), \
                patch.object(runner, "run_worker", side_effect=worker):
            self.assertEqual(runner.main(), 1)
        self.assertEqual(modes, ["seed", "candidate", "behavior"])
        report = json.loads((output / "report.json").read_text())
        self.assertFalse(report["candidate_migrations_data_integrity"]["passed"])
        self.assertTrue(report["old_repository_behavior"]["passed"])
        self.assertFalse(report["passed"])

    def test_default_migrations_path_is_candidate_before_old_import(self) -> None:
        """Настоящий import default argument видит SQL, подставленный заранее."""
        module = self.previous / "old_fixture.py"
        module.write_text(
            "from pathlib import Path\n"
            f"SOURCE = Path(__file__).parent / {MIGRATIONS!r}\n"
            "def fixture(source=SOURCE):\n"
            "    return sorted(p.name for p in source.glob('*.sql'))\n",
            encoding="utf-8",
        )
        original = snapshot(self.previous)
        (self.candidate / MIGRATIONS / "0002_extra.sql").write_text("SELECT 1;\n")
        overlay_sql(self.previous, self.candidate, original)
        spec = importlib.util.spec_from_file_location("old_fixture", module)
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        self.assertEqual(loaded.fixture(), ["0001_initial.sql", "0002_extra.sql"])

    def test_export_matches_raw_git_blob_and_rejects_moving_ref(self) -> None:
        """Точный export совпадает с Git bytes независимо от Windows checkout."""
        repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "status", "--short"], cwd=repository, check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
        destination = self.root / "export"
        manifest = export_commit(repository, sha, destination)
        content = subprocess.run(["git", "show", f"{sha}:requirements.txt"], cwd=repository, check=True, capture_output=True).stdout
        self.assertEqual((destination / "requirements.txt").read_bytes(), content)
        self.assertEqual(manifest["requirements.txt"], hashlib.sha256(content).hexdigest())
        with self.assertRaisesRegex(ValueError, "full lowercase"):
            export_commit(repository, "HEAD", self.root / "invalid")

    def test_worker_import_error_is_failed_result_with_log(self) -> None:
        """Ошибочный source не превращается в успешный пустой результат."""
        output = self.root / "output"
        output.mkdir()
        result = run_worker(Path(sys.executable), self.previous, "behavior", output,
                            ["--test-modules", "tests.test_missing"], 30)
        self.assertFalse(result["passed"])
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("missing checkout module", result["error"])
        self.assertTrue((output / "behavior.log").is_file())
        self.assertFalse(json.loads((output / "behavior.json").read_text())["passed"])

    def test_real_old_tests_observe_added_sql_and_report_sql_failure(self) -> None:
        """Старые repository tests используют новую SQL до импорта и видят её ошибку."""
        repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "status", "--short"], cwd=repository, check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
        checkout = self.root / "real-old"
        original = export_commit(repository, sha, checkout)
        sql_source = self.root / "sql-source"
        (sql_source / MIGRATIONS).mkdir(parents=True)
        paths = list((checkout / MIGRATIONS).glob("*.sql"))
        for path in paths:
            (sql_source / MIGRATIONS / path.name).write_bytes(path.read_bytes())
        added = f"{len(paths) + 1:04d}_runner_fixture.sql"
        (sql_source / MIGRATIONS / added).write_text(
            "ALTER TABLE app_users ADD COLUMN runner_fixture TEXT;\n", encoding="utf-8")
        expected = overlay_sql(checkout, sql_source, original)
        output = self.root / "valid-output"
        output.mkdir()
        result = run_worker(Path(sys.executable), checkout, "behavior", output,
                            ["--test-modules", "tests.test_user_identity_repository",
                             "tests.test_vault_lease_fencing"], 60)
        self.assertTrue(result["passed"], result)
        self.assertGreater(result["candidate_schema_observations"], 0)
        self.assertIn(added, result["migration_names"])
        self.assertTrue(all(Path(origin).is_relative_to(checkout) for origin in result["imports"].values()))
        self.assertEqual(snapshot(checkout), expected)
        (sql_source / MIGRATIONS / added).write_text("INVALID SQL;\n", encoding="utf-8")
        overlay_sql(checkout, sql_source, expected)
        output = self.root / "invalid-output"
        output.mkdir()
        result = run_worker(Path(sys.executable), checkout, "behavior", output,
                            ["--test-modules", "tests.test_user_identity_repository"], 60)
        self.assertFalse(result["passed"])
        self.assertTrue(result["old_tests"]["errors"])


if __name__ == "__main__":
    unittest.main()
