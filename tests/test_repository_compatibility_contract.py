"""Проверяет полноту policy и реальные отказы старых записей на новом SQL."""

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
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
from scripts.deployment_acceptance import AcceptedBaseline
from scripts.repository_compatibility_contract import load_policy, verify_inventory, verify_report, verify_tests
from tests.compatibility_test_environment import worker_python


BASELINE = AcceptedBaseline("22c01936c58d0cd7c99df334465f4562f9e3ca4e", "v3.4.0",
                            442821807, 7736620585, 22029591001)


class CompatibilityContractTests(unittest.TestCase):
    """Проверяет доверенную базу и manifest без GitHub/VPS."""

    def test_baseline_requires_pinned_immutable_release_and_commit(self) -> None:
        """Произвольные input, tag и Release не заменяют закреплённую базу."""
        policy = {"previous_sha": BASELINE.sha, "release_id": BASELINE.release_id,
                  "release_tag": BASELINE.version}
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
        annotated = {"object": {"type": "tag", "sha": "e" * 40}}
        tag = {"sha": "e" * 40, "tag": policy["release_tag"], "message": "evidence",
               "object": reference["object"]}
        verify_baseline(policy, release, annotated, policy["previous_sha"], lambda _: tag)
        with self.assertRaises(ValueError):
            verify_baseline(policy, release, annotated, policy["previous_sha"],
                            lambda _: dict(tag, sha="f" * 40))

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
        for kind in ("missing", "duplicate", "unknown", "stale-baseline"):
            altered = deepcopy(policy)
            if kind == "missing":
                altered["matrix"].pop()
            elif kind == "duplicate":
                altered["matrix"][0] = altered["matrix"][1]
            elif kind == "unknown":
                altered["matrix"][0]["test_id"] = "missing.test"
            else:
                altered["previous_sha"] = BASELINE.sha
            with TemporaryDirectory() as temporary:
                path = Path(temporary) / "policy.json"
                path.write_text(json.dumps(altered), encoding="utf-8")
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    load_policy(path)

    @staticmethod
    def _report(policy: dict, checkout: PurePath) -> dict:
        source = "compadre_assistant/fixture.py"
        old = {source: "old", f"{MIGRATIONS}/0001_initial.sql": "sql1"}
        new = dict(old, **{f"{MIGRATIONS}/0002_added.sql": "sql2"})
        worker = {"passed": True, "imports": {"compadre_assistant.fixture": str(checkout / source)},
                  "environment": {"sys_path": [str(checkout)]}}
        tests = {"passed": True, "test_ids": policy["test_ids"], "tests_run": len(policy["test_ids"]),
                 "skips": [], "failures": [], "errors": []}
        candidate = dict(worker, candidate_tests=tests, fresh_fingerprint="schema", upgraded_fingerprint="schema")
        behavior = dict(worker, old_tests=tests, schema_fingerprints=["schema"],
                        candidate_schema_observations=1, schema_errors=[])
        return {"passed": True, "previous_sha": BASELINE.sha,
                "source_verification": "passed", "source_changes": {"previous": [], "candidate": []},
                "sources": {"previous": old, "candidate": new}, "previous_with_candidate_sql": new,
                "previous_seed": worker, "candidate_migrations_data_integrity": candidate,
                "old_repository_behavior": behavior,
                "candidate_verified_previous_updates": dict(worker, schema_fingerprint="schema")}

    def test_report_accepts_worker_paths_from_linux_and_windows(self) -> None:
        """Формат путей зависит от worker, который создал отчёт."""
        policy = load_policy()
        for checkout in (PurePosixPath("/tmp/ci/checkout"), PureWindowsPath(r"C:\Temp\ci\checkout")):
            with self.subTest(checkout=checkout):
                verify_report(self._report(policy, checkout), policy, previous_sha=BASELINE.sha)

    def test_report_requires_absolute_checkout_and_tracked_import_origin(self) -> None:
        """Происхождение импортов проверяется в обоих форматах путей."""
        policy = load_policy()
        for checkout in (PurePosixPath("/tmp/ci/checkout"), PureWindowsPath(r"C:\Temp\ci\checkout")):
            for origin in (checkout.parent / "other/compadre_assistant/fixture.py",
                           checkout / "../checkout/compadre_assistant/fixture.py",
                           checkout / "compadre_assistant/untracked.py",
                           type(checkout)("compadre_assistant/fixture.py")):
                with self.subTest(checkout=checkout, origin=origin), self.assertRaises(ValueError):
                    report = self._report(policy, checkout)
                    report["previous_seed"]["imports"] = {"compadre_assistant.fixture": str(origin)}
                    verify_report(report, policy, previous_sha=BASELINE.sha)
            with self.subTest(checkout=checkout.name), self.assertRaises(ValueError):
                verify_report(self._report(policy, type(checkout)(checkout.name)), policy, previous_sha=BASELINE.sha)

    def test_report_rejects_sql_import_baseline_and_result_substitution(self) -> None:
        """Подмена evidence не скрывается за зелёным агрегированным passed."""
        policy = load_policy()
        report = self._report(policy, Path.cwd().resolve() / "fixture")
        source = "compadre_assistant/fixture.py"
        new = report["sources"]["candidate"]
        verify_report(report, policy, previous_sha=BASELINE.sha)
        with self.assertRaisesRegex(ValueError, "verified previous SHA"):
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
                verify_report(changed, policy, previous_sha=BASELINE.sha)

    def test_ci_evidence_binds_pair_policy_attempt_and_exact_report_bytes(self) -> None:
        """Orchestrator скрывает token от worker и хеширует результаты одной попытки."""
        policy = load_policy()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_root = root / "policy"
            trusted = {}
            for name in ci.DYNAMIC_HARNESS:
                path = policy_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                content = name.encode()
                path.write_bytes(content)
                trusted[name] = hashlib.sha256(content).hexdigest()
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
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(ci, "__file__", str(policy_root / "scripts/ci_repository_compatibility.py")), \
                    patch.object(sys, "argv", ["ci", "--repository", str(root), "--previous-sha",
                                              BASELINE.sha, "--accepted-baseline", json.dumps(asdict(BASELINE)),
                                              "--output", str(output)]), \
                    patch("scripts.deployment_acceptance.resolve_accepted_baseline", return_value=BASELINE) as resolve, \
                    patch.object(ci, "export_commit", side_effect=[trusted, workflows, {"old": "hash"}]) as export, \
                    patch.object(ci, "verify_inventory"), patch.object(ci, "verify_report"), \
                    patch.object(ci.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(ci.main(), 0)
                self.assertFalse(any(key.startswith(("GITHUB_", "ACTIONS_")) for key in run.call_args.kwargs["env"]))
                for kind in ("matrix", "resolver", "workflow", "baseline"):
                    export.side_effect = [trusted, workflows, {"old": "hash"}]
                    run.reset_mock()
                    if kind == "matrix":
                        (policy_root / "scripts/repository_compatibility_policy.json").write_bytes(b"substituted matrix")
                    elif kind == "resolver":
                        (policy_root / "scripts/deployment_acceptance.py").write_bytes(b"substituted resolver")
                    elif kind == "workflow":
                        export.side_effect = [trusted, dict(workflows, **{".github/workflows/full-suite.yml": "substituted"})]
                    else:
                        resolve.return_value = replace(BASELINE, status_id=BASELINE.status_id + 1)
                    with self.subTest(kind=kind), self.assertRaisesRegex(
                            ValueError, "policy checkout|trusted workflow|latest accepted baseline"):
                        ci.main()
                    run.assert_not_called()
                    for path in ("scripts/repository_compatibility_policy.json", "scripts/deployment_acceptance.py"):
                        (policy_root / path).write_bytes(path.encode())
            evidence = json.loads((output / "evidence.json").read_text())
            self.assertEqual((evidence["candidate_sha"], evidence["previous_sha"], evidence["policy_sha"]),
                             ("a" * 40, BASELINE.sha, "b" * 40))
            self.assertEqual(evidence["version"], 2)
            self.assertEqual(evidence["baseline"], asdict(BASELINE))
            self.assertEqual(set(evidence["policy_files"]), set(ci.DYNAMIC_HARNESS))
            self.assertEqual((evidence["run_id"], evidence["run_attempt"]), (12, 3))
            self.assertEqual(evidence["files"]["report.json"], hashlib.sha256((output / "report.json").read_bytes()).hexdigest())
            (output / "report.json").write_text("{}", encoding="utf-8")
            self.assertNotEqual(evidence["files"]["report.json"], hashlib.sha256((output / "report.json").read_bytes()).hexdigest())


class CompatibilitySqlMutationTests(unittest.TestCase):
    """Исполняет неизменный previous API из точного Git export на временных БД."""

    @classmethod
    def setUpClass(cls) -> None:
        """Экспортирует v4.0.0 с актуальным составом тестов совместимости один раз."""
        cls.temporary = TemporaryDirectory(prefix="compatibility-mutations-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.policy = load_policy()
        cls.repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "status", "--short"], cwd=cls.repository, check=True, capture_output=True)
        cls.checkout = cls.root / "previous"
        cls.original = export_commit(cls.repository, "119e36d2bdde0162b970c81cdc980d61dec0cc9c", cls.checkout)

    def _run(self, name: str, sql: str, *, full: bool = False) -> dict:
        source = self.root / name / "source"
        (source / MIGRATIONS).mkdir(parents=True)
        for path in (self.checkout / MIGRATIONS).glob("*.sql"):

            (source / MIGRATIONS / path.name).write_bytes(path.read_bytes())
        (source / MIGRATIONS / "0006_mutation.sql").write_text(sql, encoding="utf-8")
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
        """Новый столбец и удаление неиспользуемого fixture-поля проходят полный набор тестов."""
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
