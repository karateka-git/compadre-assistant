"""Проверяет точные sources, SQL-only подстановку и границы imports runner 5.2."""

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import check_repository_compatibility as runner

from scripts.check_repository_compatibility import (
    MIGRATIONS, attach_git_history, export_commit, overlay_sql, run_worker, snapshot, verify_git_history,
)
from scripts.repository_compatibility_worker import CheckoutImports, run_tests
from tests.compatibility_test_environment import worker_python


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

    def test_guard_rejects_foreign_discovered_test_package(self) -> None:
        """Каталог тестов с __init__ тоже импортируется только из принятой версии."""
        for checkout in (self.previous, self.candidate):
            package = checkout / "tests" / "compat_test_group"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
        guard = CheckoutImports(self.previous)
        with patch.object(sys, "path", [str(self.candidate / "tests"), *sys.path]):
            with self.assertRaisesRegex(ImportError, "leakage"):
                guard.find_spec("compat_test_group")

    def test_exact_export_gets_private_git_history_without_source_changes(self) -> None:
        """Вложенный экспорт читает старый SHA, а чужой HEAD/index/link отклоняются."""
        repository = self.root / "source"
        repository.mkdir()

        def git(cwd, *args):
            return subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgsign=false", *args],
                cwd=cwd, check=True, capture_output=True, text=True,
            ).stdout.strip()

        git(repository, "init", "--quiet")
        test_module = repository / "test_fixture.py"
        test_module.write_bytes(b"# historical\n")
        git(repository, "add", "test_fixture.py")
        git(repository, "commit", "--quiet", "-m", "Historical fixture")
        historical = git(repository, "rev-parse", "HEAD")
        test_module.write_bytes(b"# accepted\n")
        git(repository, "add", "test_fixture.py")
        git(repository, "commit", "--quiet", "-m", "Accepted fixture")
        accepted = git(repository, "rev-parse", "HEAD")
        checkout = self.root / "exact"
        original = export_commit(repository, accepted, checkout)
        metadata = self.root / "private-git"

        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "wrong-git"),
                                     "GIT_INDEX_FILE": str(self.root / "wrong-index")}):
            marker = attach_git_history(repository, checkout, accepted, metadata)
        self.assertEqual(snapshot(checkout, git_marker=marker), original)
        self.assertEqual(git(checkout, "status", "--porcelain"), "")
        self.assertEqual(git(checkout, "remote"), "")
        proof = verify_git_history(checkout, accepted, metadata)
        self.assertEqual(proof, {"head": accepted, "index_tree": git(repository, "rev-parse", "HEAD^{tree}")})
        nested = self.root / "nested-export"
        export_commit(checkout, historical, nested)
        self.assertEqual((nested / "test_fixture.py").read_bytes(), b"# historical\n")

        git(checkout, "update-ref", "--no-deref", "HEAD", historical)
        with self.assertRaisesRegex(ValueError, "HEAD differs"):
            verify_git_history(checkout, accepted, metadata)
        git(checkout, "update-ref", "--no-deref", "HEAD", accepted)
        git(checkout, "read-tree", historical)
        with self.assertRaisesRegex(ValueError, "index differs"):
            verify_git_history(checkout, accepted, metadata)
        (checkout / ".git").unlink()
        (checkout / ".git").write_bytes(b"gitdir: elsewhere\n")
        with self.assertRaisesRegex(ValueError, "metadata link changed"):
            snapshot(checkout, git_marker=marker)
        self.assertEqual(git(repository, "rev-parse", "HEAD"), accepted)

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
        result = run_worker(worker_python(), self.previous, "behavior", output,
                            ["--test-modules", "tests.test_missing"], 30)
        self.assertFalse(result["passed"])
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("missing checkout module", result["error"])
        self.assertTrue((output / "behavior.log").is_file())
        self.assertFalse(json.loads((output / "behavior.json").read_text())["passed"])

    def _write_discovery_module(self, suffix: str, source: str) -> None:
        name = f"test_compat_discovery_{suffix}"
        (self.previous / "tests" / f"{name}.py").write_text(
            "import unittest\n" + source, encoding="utf-8")
        self.addCleanup(sys.modules.pop, name, None)

    def _discover(self) -> dict:
        with patch.object(sys, "path", sys.path.copy()), redirect_stderr(io.StringIO()):
            return run_tests([], discover_dir=self.previous / "tests")

    def test_discovery_runs_new_module_and_reports_declared_skips(self) -> None:
        """Новый файл проходит discovery без правки списка и виден в отчёте."""
        self._write_discovery_module("common", """
class OrdinaryTest(unittest.TestCase):
    @unittest.skip('platform')
    def test_skip(self):
        self.fail('skip must prevent this assertion')

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.fail('expected')
""")
        self._write_discovery_module("new", """
class AddedTest(unittest.TestCase):
    def test_new_case(self):
        self.assertEqual(2 + 2, 4)
""")
        result = self._discover()
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["test_ids"], result["started_test_ids"])
        self.assertIn("test_compat_discovery_new.AddedTest.test_new_case", result["started_test_ids"])
        self.assertEqual(result["skips"], [("test_compat_discovery_common.OrdinaryTest.test_skip", "platform")])
        self.assertEqual(result["expected_failures"], ["test_compat_discovery_common.OrdinaryTest.test_expected_failure"])

    def test_discovery_fails_if_cases_are_not_started(self) -> None:
        """Пропуск через setup модуля и вызов без startTest делают прогон неполным."""
        self._write_discovery_module("setup_skip", """
def setUpModule():
    raise unittest.SkipTest('module fixture')

class SkippedBySetup(unittest.TestCase):
    def test_case(self):
        self.fail('module fixture must prevent this assertion')
""")
        self._write_discovery_module("dropped", """
class DroppedTest(unittest.TestCase):
    def run(self, result=None):
        return result

    def test_case(self):
        self.fail('overridden run must prevent this assertion')
""")
        result = self._discover()
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["test_ids"]), 2)
        self.assertEqual(result["started_test_ids"], [])
        self.assertEqual(result["skips"][0][1], "module fixture")

    def test_discovery_fails_if_loading_a_module_errors_or_skips(self) -> None:
        """Ошибка загрузки или пропуск на импорте видны в результате discovery."""
        self._write_discovery_module("import_error", "raise RuntimeError('import failed')\n")
        self._write_discovery_module("import_skip", "raise unittest.SkipTest('module import')\n")
        result = self._discover()
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["loader_errors"]), 2)
        self.assertIn("import failed", result["loader_errors"][0])
        self.assertIn("skipped during import", result["loader_errors"][1])

    def test_discovery_rejects_duplicate_ids(self) -> None:
        """Два запуска одного ID не могут подтвердить полноту набора."""
        self._write_discovery_module("duplicate", """
class DuplicateTest(unittest.TestCase):
    def test_case(self):
        pass

def load_tests(loader, tests, pattern):
    tests.addTest(DuplicateTest('test_case'))
    return tests
""")
        result = self._discover()
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["started_test_ids"]), 2)
        self.assertEqual(len(set(result["started_test_ids"])), 1)

    def test_external_suite_rejects_declared_skip(self) -> None:
        """Для доверенных дополнительных проверок отказ действует и на обычные skips."""
        external = self.root / "external.py"
        external.write_text("""import unittest

class AdditionalTest(unittest.TestCase):
    @unittest.skip('disabled')
    def test_case(self):
        pass
""", encoding="utf-8")
        with patch.dict(sys.modules), redirect_stderr(io.StringIO()):
            result = run_tests([], external)
        self.assertFalse(result["passed"])
        self.assertEqual(result["test_ids"], result["started_test_ids"])
        self.assertEqual(len(result["skips"]), 1)

    def test_full_discovery_applies_overlay_to_child_and_rejects_incompatible_sql(self) -> None:
        """Новый тест автоматически проверяет схему в основном процессе и CLI."""
        source = Path(__file__).resolve().parents[1]
        checkout = self.root / "discover-runtime"
        for package in ("compadre_assistant", "document_chunker"):
            shutil.copytree(source / package, checkout / package, ignore=shutil.ignore_patterns("__pycache__"))
        tests = checkout / "tests"
        tests.mkdir()
        (tests / "test_added_migration.py").write_text("""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations

class MigrationTest(unittest.TestCase):
    def test_current_schema(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with connect_database(root / "direct.db") as connection:
                apply_migrations(connection)
                connection.execute("SELECT display_name FROM app_users")
            subprocess.run(
                [sys.executable, "-m", "compadre_assistant", "--migrate", "--database-path", str(root / "child.db")],
                cwd=root, env=os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                capture_output=True, check=True, timeout=30)
            with connect_database(root / "child.db") as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(app_users)")}
            self.assertIn("discovered_fixture_column", columns)
""", encoding="utf-8")
        external = self.root / "additional.py"
        external.write_text("""import unittest

class AdditionalTest(unittest.TestCase):
    def test_external(self):
        pass
""", encoding="utf-8")
        original = snapshot(checkout)
        candidate = self.root / "sql-source"
        shutil.copytree(checkout / MIGRATIONS, candidate / MIGRATIONS)
        version = len(list((candidate / MIGRATIONS).glob("*.sql"))) + 1
        added = candidate / MIGRATIONS / f"{version:04d}_discovery_fixture.sql"
        added.write_text("ALTER TABLE app_users ADD COLUMN discovered_fixture_column TEXT;\n", encoding="utf-8")
        expected = overlay_sql(checkout, candidate, original)
        test_id = "test_added_migration.MigrationTest.test_current_schema"
        args = ["--full-discovery", "--compatibility-tests", str(external)]
        compatible = self.root / "compatible"
        compatible.mkdir()
        result = run_worker(worker_python(), checkout, "behavior", compatible, args, 60)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["old_tests"]["started_test_ids"], [test_id])
        self.assertTrue(result["additional_tests"]["passed"])
        self.assertEqual(result["candidate_schema_observations"], 1)
        self.assertIn(added.name, result["migration_names"])
        self.assertEqual(snapshot(checkout), expected)

        added.write_text("ALTER TABLE app_users RENAME COLUMN display_name TO hidden_name;\n", encoding="utf-8")
        overlay_sql(checkout, candidate, expected)
        incompatible = self.root / "incompatible"
        incompatible.mkdir()
        result = run_worker(worker_python(), checkout, "behavior", incompatible, args, 60)
        self.assertFalse(result["passed"])
        self.assertEqual(result["old_tests"]["errors"], [test_id])
        self.assertTrue(result["additional_tests"]["passed"])
        self.assertEqual(result["candidate_schema_observations"], 1)

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
        result = run_worker(worker_python(), checkout, "behavior", output,
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
        result = run_worker(worker_python(), checkout, "behavior", output,
                            ["--test-modules", "tests.test_user_identity_repository"], 60)
        self.assertFalse(result["passed"])
        self.assertTrue(result["old_tests"]["errors"])


if __name__ == "__main__":
    unittest.main()
