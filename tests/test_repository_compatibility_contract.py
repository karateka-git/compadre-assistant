"""Проверяет полноту policy и реальные отказы старых записей на новом SQL."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.check_repository_compatibility import (
    BEHAVIOR_MODULES, COMPATIBILITY_TESTS, MIGRATIONS, export_commit, overlay_sql, run_worker, snapshot,
)
from scripts.ci_repository_compatibility import verify_baseline
from scripts import ci_repository_compatibility as ci
from scripts.repository_compatibility_contract import load_policy, verify_inventory, verify_report, verify_tests
from tests.compatibility_test_environment import worker_python


class CompatibilityContractTests(unittest.TestCase):
    """Проверяет доверенную базу и manifest без GitHub/VPS."""

    def test_baseline_requires_pinned_immutable_release_and_direct_tag(self) -> None:
        """Произвольные input, tag и Release не заменяют закреплённую базу."""
        policy = load_policy()
        release = {"id": policy["release_id"], "tag_name": policy["release_tag"],
                   "immutable": True, "draft": False, "prerelease": False, "published_at": "2026-11-02"}
        reference = {"object": {"type": "commit", "sha": policy["previous_sha"]}}
        verify_baseline(policy, release, reference, policy["previous_sha"])
        for key, value in (("id", 1), ("tag_name", "v0.0.1"), ("immutable", False),
                           ("draft", True), ("prerelease", True), ("published_at", None)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_baseline(policy, dict(release, **{key: value}), reference, policy["previous_sha"])
        for obj in ({"type": "tag", "sha": policy["previous_sha"]}, {"type": "commit", "sha": "a" * 40}):
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                verify_baseline(policy, release, {"object": obj}, policy["previous_sha"])
        with self.assertRaises(ValueError):
            verify_baseline(policy, release, reference, "a" * 40)

    def test_manifest_rejects_empty_skips_duplicates_and_missing_test(self) -> None:
        """Даже passed=true не заменяет полный выполненный manifest."""
        ids = load_policy()["test_ids"]
        result = {"passed": True, "test_ids": ids, "tests_run": len(ids),
                  "skips": [], "failures": [], "errors": []}
        verify_tests(result, ids)
        changes = [{"test_ids": []}, {"skips": [[ids[0], "skip"]]},
                   {"test_ids": ids[:-1], "tests_run": len(ids) - 1},
                   {"test_ids": ids[:-1] + [ids[0]]}, {"tests_run": 0},
                   {"expected_failures": [ids[0]]}, {"errors": [ids[0]]}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_tests(dict(result, **change), ids)

    def test_policy_rejects_missing_duplicate_or_unmapped_operation(self) -> None:
        """Удалённый метод и неизвестный test ID не проходят в новой policy."""
        policy = load_policy()
        for kind in ("missing", "duplicate", "unknown"):
            altered = deepcopy(policy)
            if kind == "missing":
                altered["matrix"].pop()
            elif kind == "duplicate":
                altered["matrix"][0] = altered["matrix"][1]
            else:
                altered["matrix"][0]["test_id"] = "missing.test"
            with TemporaryDirectory() as temporary:
                path = Path(temporary) / "policy.json"
                path.write_text(json.dumps(altered), encoding="utf-8")
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    load_policy(path)

    def test_report_rejects_sql_import_baseline_and_result_substitution(self) -> None:
        """Подмена evidence не скрывается за зелёным агрегированным passed."""
        policy = load_policy()
        checkout = Path.cwd().resolve() / "fixture"
        source = "compadre_assistant/fixture.py"
        old = {source: "old", f"{MIGRATIONS}/0001_initial.sql": "sql1"}
        new = dict(old, **{f"{MIGRATIONS}/0002_added.sql": "sql2"})
        worker = {"passed": True, "imports": {"compadre_assistant.fixture": str(checkout / source)},
                  "environment": {"sys_path": [str(checkout)]}}
        tests = {"passed": True, "test_ids": policy["test_ids"], "tests_run": 173,
                 "skips": [], "failures": [], "errors": []}
        candidate = dict(worker, candidate_tests=tests, fresh_fingerprint="schema", upgraded_fingerprint="schema")
        behavior = dict(worker, old_tests=tests, schema_fingerprints=["schema"],
                        candidate_schema_observations=1, schema_errors=[])
        report = {"passed": True, "previous_sha": policy["previous_sha"],
                  "source_verification": "passed", "source_changes": {"previous": [], "candidate": []},
                  "sources": {"previous": old, "candidate": new}, "previous_with_candidate_sql": new,
                  "previous_seed": worker, "candidate_migrations_data_integrity": candidate,
                  "old_repository_behavior": behavior,
                  "candidate_verified_previous_updates": dict(worker, schema_fingerprint="schema")}
        verify_report(report, policy)
        for kind in ("baseline", "sql", "import", "source", "candidate", "fingerprint", "manifest"):
            changed = deepcopy(report)
            if kind == "baseline":
                changed["previous_sha"] = "a" * 40
            elif kind == "sql":
                changed["previous_with_candidate_sql"][f"{MIGRATIONS}/0002_added.sql"] = "substituted"
                # Отдельный объект source нужен, чтобы не подменять оба свидетельства сразу.
                changed["sources"]["candidate"] = dict(new)
            elif kind == "import":
                changed["old_repository_behavior"]["imports"] = {"compadre_assistant.fixture": str(Path.cwd() / "candidate.py")}
            elif kind == "source":
                changed["source_changes"]["previous"] = [source]
            elif kind == "candidate":
                changed["candidate_migrations_data_integrity"]["passed"] = False
            elif kind == "fingerprint":
                changed["old_repository_behavior"]["schema_fingerprints"] = ["different"]
            else:
                changed["old_repository_behavior"]["old_tests"]["test_ids"] = policy["test_ids"][:-1]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                verify_report(changed, policy)

    def test_ci_evidence_binds_pair_policy_attempt_and_exact_report_bytes(self) -> None:
        """Orchestrator скрывает token от worker и хеширует результаты одной попытки."""
        policy = load_policy()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_root = root / "policy"
            (policy_root / "scripts").mkdir(parents=True)
            names = ("ci_repository_compatibility.py", "repository_compatibility_contract.py",
                     "repository_compatibility_policy.json", "check_repository_compatibility.py",
                     "repository_compatibility_worker.py", "repository_compatibility_cases.py")
            trusted = {}
            for name in names:
                content = name.encode()
                (policy_root / "scripts" / name).write_bytes(content)
                trusted[f"scripts/{name}"] = hashlib.sha256(content).hexdigest()
            workflows = {f".github/workflows/{name}.yml": "workflow" for name in
                         ("full-suite", "main-full", "release-full")}
            trusted.update(workflows)
            output = root / "output"
            output.mkdir()
            report = {"candidate_sha": "a" * 40, "sources": {"previous": {"old": "hash"}, "candidate": workflows},
                      **{name: {"passed": True} for name in (
                          "candidate_migrations_data_integrity", "old_repository_behavior",
                          "candidate_verified_previous_updates")}}
            (output / "report.json").write_text(json.dumps(report), encoding="utf-8")
            environment = {"GITHUB_SHA": "a" * 40, "COMPATIBILITY_POLICY_SHA": "b" * 40,
                           "GITHUB_REPOSITORY": policy["repository"], "GITHUB_RUN_ID": "12",
                           "GITHUB_RUN_ATTEMPT": "3", "GITHUB_TOKEN": "must-not-leak",
                           "GITHUB_OUTPUT": "must-not-write", "ACTIONS_RUNTIME_TOKEN": "must-not-leak",
                           "GITHUB_WORKFLOW_REF": f"{policy['repository']}/.github/workflows/main-full.yml@refs/heads/main"}
            release = {"id": policy["release_id"], "tag_name": policy["release_tag"],
                       "immutable": True, "draft": False, "prerelease": False, "published_at": "date"}
            reference = {"object": {"type": "commit", "sha": policy["previous_sha"]}}
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(ci, "__file__", str(policy_root / "scripts/ci_repository_compatibility.py")), \
                    patch.object(sys, "argv", ["ci", "--repository", str(root), "--previous-sha",
                                              policy["previous_sha"], "--output", str(output)]), \
                    patch.object(ci, "_get", side_effect=[release, reference]) as get, \
                    patch.object(ci, "export_commit", side_effect=[trusted, workflows, {"old": "hash"}]) as export, \
                    patch.object(ci, "verify_inventory"), patch.object(ci, "verify_report"), \
                    patch.object(ci.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(ci.main(), 0)
                self.assertFalse(any(key.startswith(("GITHUB_", "ACTIONS_")) for key in run.call_args.kwargs["env"]))
                for kind in ("matrix", "workflow"):
                    get.side_effect = [release, reference]
                    export.side_effect = [trusted, workflows, {"old": "hash"}]
                    run.reset_mock()
                    if kind == "matrix":
                        (policy_root / "scripts/repository_compatibility_policy.json").write_bytes(b"substituted matrix")
                    else:
                        (policy_root / "scripts/repository_compatibility_policy.json").write_bytes(b"repository_compatibility_policy.json")
                        export.side_effect = [trusted, dict(workflows, **{".github/workflows/full-suite.yml": "substituted"})]
                    with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "policy checkout|trusted workflow"):
                        ci.main()
                    run.assert_not_called()
            evidence = json.loads((output / "evidence.json").read_text())
            self.assertEqual((evidence["candidate_sha"], evidence["previous_sha"], evidence["policy_sha"]),
                             ("a" * 40, policy["previous_sha"], "b" * 40))
            self.assertEqual((evidence["run_id"], evidence["run_attempt"]), (12, 3))
            self.assertEqual(evidence["files"]["report.json"], hashlib.sha256((output / "report.json").read_bytes()).hexdigest())
            (output / "report.json").write_text("{}", encoding="utf-8")
            self.assertNotEqual(evidence["files"]["report.json"], hashlib.sha256((output / "report.json").read_bytes()).hexdigest())


class CompatibilitySqlMutationTests(unittest.TestCase):
    """Исполняет неизменный previous API из точного Git export на временных БД."""

    @classmethod
    def setUpClass(cls) -> None:
        """Получает v3.1.0 один раз без checkout рабочего дерева."""
        cls.temporary = TemporaryDirectory(prefix="compatibility-mutations-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.policy = load_policy()
        cls.repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "status", "--short"], cwd=cls.repository, check=True, capture_output=True)
        cls.checkout = cls.root / "previous"
        cls.original = export_commit(cls.repository, cls.policy["previous_sha"], cls.checkout)

    def _run(self, name: str, sql: str, *, full: bool = False) -> dict:
        source = self.root / name / "source"
        (source / MIGRATIONS).mkdir(parents=True)
        for path in (self.checkout / MIGRATIONS).glob("*.sql"):
            if path.name.startswith("0005_"):
                continue
            (source / MIGRATIONS / path.name).write_bytes(path.read_bytes())
        (source / MIGRATIONS / "0005_mutation.sql").write_text(sql, encoding="utf-8")
        expected = overlay_sql(self.checkout, source, snapshot(self.checkout))
        output = self.root / name / "result"
        output.mkdir()
        modules = [f"tests.test_{name}" for name in BEHAVIOR_MODULES] if full else ["tests.test_user_identity_repository"]
        extra = ["--compatibility-tests", str(COMPATIBILITY_TESTS.resolve())] if full else []
        result = run_worker(worker_python(), self.checkout, "behavior", output,
                            [*extra, "--test-modules", *modules], 180)
        self.assertEqual(snapshot(self.checkout), expected)
        self.assertEqual(
            {path: digest for path, digest in expected.items() if not path.endswith('.sql')},
            {path: digest for path, digest in self.original.items() if not path.endswith('.sql')},
        )
        failed_ids = (result.get("old_tests", {}).get("errors", [])
                      + result.get("old_tests", {}).get("failures", []))
        if failed_ids:
            log = (output / "behavior.log").read_text(encoding="utf-8", errors="replace")
            self.assertIn("Traceback (most recent call last)", log)
            for test_id in failed_ids:
                self.assertIn(test_id.rsplit(".", 1)[1], log)
            saved = json.loads((output / "behavior.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["old_tests"]["errors"], result["old_tests"]["errors"])
        return result

    def test_compatible_addition_and_unused_column_removal_pass_full_matrix(self) -> None:
        """Новый столбец и удаление неиспользуемого fixture-поля проходят 173 tests."""
        verify_inventory(self.checkout, self.policy)
        changed = deepcopy(self.policy)
        changed["matrix"][0]["method"] = "nonexistent"
        with self.assertRaisesRegex(ValueError, "public API"):
            verify_inventory(self.checkout, changed)
        # Дополнительное поле — отдельная synthetic fixture, исходные SQL не меняются.
        for name, sql in (
            ("add", "ALTER TABLE app_users ADD COLUMN compatibility_unused TEXT;"),
            ("drop_unused", "ALTER TABLE app_users ADD COLUMN compatibility_unused TEXT;\n"
                            "ALTER TABLE app_users DROP COLUMN compatibility_unused;"),
        ):
            with self.subTest(name=name):
                result = self._run(name, sql, full=True)
                self.assertTrue(result["passed"], result.get("error", result.get("old_tests")))
                verify_tests(result["old_tests"], self.policy["test_ids"])

    def test_changed_read_value_fails_old_assertions(self) -> None:
        """Успешный INSERT не оправдывает изменённое значение при прежнем чтении."""
        result = self._run("read_value", "CREATE TRIGGER corrupt_read AFTER INSERT ON app_users "
                           "BEGIN UPDATE app_users SET display_name='changed' WHERE id=NEW.id; END;")
        self.assertFalse(result["passed"])
        self.assertTrue(result["old_tests"]["failures"])
        self.assertGreater(result["candidate_schema_observations"], 0)

    def test_breaking_writes_fail_without_schema_diff(self) -> None:
        """DROP/RENAME, CHECK, trigger и NOT NULL ломают настоящие прежние записи."""
        mutations = {
            "drop_used": "ALTER TABLE app_users DROP COLUMN display_name;",
            "rename_used": "ALTER TABLE app_users RENAME COLUMN display_name TO renamed;",
            "check": "ALTER TABLE app_users ADD COLUMN gate INTEGER CHECK(display_name IS NULL);",
            "trigger": "CREATE TRIGGER deny_old BEFORE INSERT ON app_users BEGIN SELECT RAISE(ABORT, 'denied'); END;",
            "not_null": "ALTER TABLE app_users ADD COLUMN required_value TEXT NOT NULL;",
        }
        for name, sql in mutations.items():
            with self.subTest(name=name):
                result = self._run(name, sql)
                self.assertFalse(result["passed"])
                self.assertTrue(result["old_tests"]["errors"])
                self.assertGreater(result["candidate_schema_observations"], 0)


if __name__ == "__main__":
    unittest.main()
