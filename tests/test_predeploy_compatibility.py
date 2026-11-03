"""Проверяет provenance artifact и online SQLite-copy без сети и production."""

import base64
from contextlib import closing, ExitStack, redirect_stderr
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from scripts import predeploy_compatibility as gate
from scripts import deploy_prod as prod
from scripts.repository_compatibility_contract import load_policy
from scripts.select_deploy_release import SelectedRelease
from tests.test_full_ci_controller import admission_jobs, admission_run
from tests.test_deploy_prod import FakeHost, OLD_SHA, OLD_IMAGE, NEW_IMAGE
from tests import test_deploy_prod as deploy_tests
from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations


POLICY_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40


class ArtifactAPI:
    """Моделирует API, artifact ZIP и Git blobs без подмены самого verifier."""

    def __init__(self):
        """Создаёт полное evidence с настоящей policy и согласованным manifest."""
        self.policy = load_policy()
        self.repository = self.policy["repository"]
        self.selected = SelectedRelease(self.repository, "v3.2.0", "release/3.2", CANDIDATE_SHA,
                                        10, "main-full.yml", 1, 1)
        self.run = admission_run(CANDIDATE_SHA)
        self.run["repository"] = self.run["head_repository"] = {"full_name": self.repository}
        self.jobs = admission_jobs(CANDIDATE_SHA)
        self.workflow = {"id": 1, "path": ".github/workflows/main-full.yml"}
        self.trusted = {name: name.encode() for name in gate.HARNESS}
        self.trusted["repository_compatibility_policy.json"] = json.dumps(self.policy).encode()
        self.workflow_bytes = b"trusted workflow"
        self.candidate_workflow_bytes = self.workflow_bytes
        root = Path.cwd().resolve() / "fake-ci-checkout"
        source = {"compadre_assistant/fixture.py": "hash"}
        worker = {"passed": True, "imports": {"fixture": str(root / next(iter(source)))},
                  "environment": {"sys_path": [str(root)]}}
        tests = {"passed": True, "test_ids": self.policy["test_ids"], "tests_run": 173,
                 "skips": [], "failures": [], "errors": []}
        self.report = {"passed": True, "previous_sha": self.policy["previous_sha"],
                       "candidate_sha": CANDIDATE_SHA, "source_verification": "passed",
                       "source_changes": {"previous": [], "candidate": []},
                       "sources": {"previous": source, "candidate": source},
                       "previous_with_candidate_sql": source, "previous_seed": worker,
                       "candidate_migrations_data_integrity": dict(worker, candidate_tests=deepcopy(tests),
                           fresh_fingerprint="c" * 64, upgraded_fingerprint="c" * 64),
                       "old_repository_behavior": dict(worker, old_tests=deepcopy(tests),
                           schema_fingerprints=["c" * 64], candidate_schema_observations=1, schema_errors=[]),
                       "candidate_verified_previous_updates": dict(worker, schema_fingerprint="c" * 64)}
        self.evidence = {"version": 1, "repository": self.repository, "candidate_sha": CANDIDATE_SHA,
                         "previous_sha": self.policy["previous_sha"], "policy_sha": POLICY_SHA,
                         "run_id": 1, "run_attempt": 1,
                         "workflow_ref": f"{self.repository}/.github/workflows/main-full.yml@refs/heads/main",
                         "baseline": {"release_id": self.policy["release_id"], "tag": self.policy["release_tag"], "immutable": True},
                         "results": {key: True for key in ("candidate_migrations_data_integrity",
                                     "old_repository_behavior", "candidate_verified_previous_updates")},
                         "policy_files": {name: hashlib.sha256(data).hexdigest() for name, data in self.trusted.items()}}
        self.artifact = {"id": 22, "name": "repository-compatibility-1-1", "expired": False,
                         "expires_at": "2099-01-01T00:00:00Z",
                         "workflow_run": {"id": 1, "head_sha": CANDIDATE_SHA}}
        self.pack()

    def pack(self):
        """Пересобирает ZIP, имитируя злоупотребление даже с новым внешним digest."""
        report = json.dumps(self.report).encode()
        self.evidence["files"] = {"report.json": hashlib.sha256(report).hexdigest()}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("evidence.json", json.dumps(self.evidence))
            archive.writestr("report.json", report)
        self.payload = stream.getvalue()
        self.artifact["digest"] = "sha256:" + hashlib.sha256(self.payload).hexdigest()

    def get(self, path, **parameters):
        """Возвращает только ожидаемые GitHub endpoints."""
        if path.startswith("/contents/"):
            name = path.rsplit("/", 1)[1]
            data = self.trusted[name] if "/scripts/" in path else (
                self.workflow_bytes if parameters["ref"] == POLICY_SHA else self.candidate_workflow_bytes)
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(data).decode(),
                    "sha": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}
        if path.startswith("/releases/tags/"):
            return {"id": self.policy["release_id"], "tag_name": self.policy["release_tag"],
                    "immutable": True, "draft": False, "prerelease": False, "published_at": "date"}
        if path.startswith("/git/ref/"):
            return {"object": {"type": "commit", "sha": self.policy["previous_sha"]}}
        if "/attempts/" in path:
            return self.run
        if path == "/actions/workflows/main-full.yml":
            return self.workflow
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Отдаёт полные списки, включая точную попытку jobs."""
        if path.endswith("/runs"):
            return [self.run]
        if path.endswith("/jobs"):
            return self.jobs
        if path.endswith("/artifacts"):
            return [self.artifact]
        raise AssertionError(path)

    def download(self, artifact_id):
        """Возвращает ZIP только по доверенному ID API."""
        assert artifact_id == 22
        return self.payload


class EvidenceTests(unittest.TestCase):
    """Проверяет успешную цепочку и fail-closed повреждения на её границах."""

    def verify(self, api):
        """Вызывает реальный verifier без подмены функций admission/manifest."""
        return gate.verify_evidence(api, api.selected, POLICY_SHA, api.policy["previous_sha"])

    def test_full_evidence_and_emergency_compatibility_success(self):
        """Обычный полный успех и emergency с отдельным зелёным gate допустимы."""
        api = ArtifactAPI()
        self.assertEqual(self.verify(api)["artifact_id"], 22)
        api.selected = replace(api.selected, kind="emergency")
        api.run["conclusion"] = "failure"
        api.jobs[1]["conclusion"] = "failure"
        self.assertEqual(self.verify(api)["artifact_id"], 22)
        api.jobs[2]["conclusion"] = "failure"
        with self.assertRaises(ValueError):
            self.verify(api)

    def test_artifact_metadata_digest_and_api_fail_closed(self):
        """Неверный run/SHA, expiry, digest и API outage закрывают допуск."""
        mutations = [("name", "repository-compatibility-1-2"), ("expired", True),
                     ("expires_at", "2000-01-01T00:00:00Z"), ("digest", "sha256:" + "0" * 64),
                     ("workflow_run", {"id": 2, "head_sha": CANDIDATE_SHA}),
                     ("workflow_run", {"id": 1, "head_sha": "d" * 40})]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                api = ArtifactAPI()
                api.artifact[key] = value
                with self.assertRaises(ValueError):
                    self.verify(api)
        api = ArtifactAPI()
        with patch.object(api, "get", side_effect=OSError("API unavailable")), self.assertRaises(OSError):
            self.verify(api)

    def test_evidence_baseline_policy_attempt_hashes_and_incomplete_report(self):
        """Новый ZIP digest не оправдывает чужую базу, policy либо неполный набор."""
        for key, value in (("previous_sha", "d" * 40), ("policy_sha", "e" * 40),
                           ("run_attempt", 2), ("run_id", 2), ("policy_files", {}),
                           ("results", {}), ("candidate_sha", "f" * 40)):
            api = ArtifactAPI()
            api.evidence[key] = value
            api.pack()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify(api)
        api = ArtifactAPI()
        api.report["old_repository_behavior"]["old_tests"]["test_ids"].pop()
        api.pack()
        with self.assertRaises(ValueError):
            self.verify(api)
        api = ArtifactAPI()
        api.candidate_workflow_bytes = b"verifier removed"
        with self.assertRaises(ValueError):
            self.verify(api)
        api = ArtifactAPI()
        with self.assertRaises(ValueError):
            gate.verify_evidence(api, api.selected, POLICY_SHA, "e" * 40)
        api.run["run_attempt"] = 2
        with self.assertRaises(ValueError):
            self.verify(api)

    def test_internal_bytes_and_zip_paths_are_checked(self):
        """Порча report при обновлённом ZIP digest и traversal не принимаются."""
        api = ArtifactAPI()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("evidence.json", json.dumps(api.evidence))
            archive.writestr("report.json", "{}")
        api.payload = stream.getvalue()
        api.artifact["digest"] = "sha256:" + hashlib.sha256(api.payload).hexdigest()
        with self.assertRaises(ValueError):
            self.verify(api)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../outside", "bad")
        with self.assertRaises(ValueError):
            gate._files(stream.getvalue())


class CopyTests(unittest.TestCase):
    """Проверяет реальные online-копии, WAL, FK и сохранение данных миграцией."""

    def test_real_candidate_migrator_on_project_schema(self):
        """Настоящий runner применяет новую SQL к полной схеме с приватной строкой."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            migrations = root / "migrations"
            shutil.copytree(MIGRATIONS_DIR, migrations)
            source = root / "live.db"
            before = sorted(path.name for path in migrations.glob("*.sql"))
            with closing(sqlite3.connect(source)) as live:
                apply_migrations(live, migrations)
                live.execute("INSERT INTO app_users(display_name) VALUES ('private fixture')")
                live.commit()
            added = f"{len(before) + 1:04d}_predeploy_fixture.sql"
            (migrations / added).write_text("ALTER TABLE app_users ADD COLUMN unused TEXT;", encoding="utf-8")
            with closing(sqlite3.connect(":memory:")) as fresh:
                apply_migrations(fresh, migrations)
                fingerprint = gate.schema_fingerprint(fresh)
            work = root / "work"
            work.mkdir()

            def migrate(directory):
                """Вызывает реальный candidate runner на копии."""
                with closing(sqlite3.connect(directory / "app.db")) as copy:
                    apply_migrations(copy, migrations)

            gate.verify_copy(source, work, before, before + [added], fingerprint, migrate)
            with closing(sqlite3.connect(source)) as live:
                self.assertEqual(live.execute("SELECT display_name FROM app_users").fetchall(), [("private fixture",)])
                self.assertNotIn("unused", [row[1] for row in live.execute("PRAGMA table_info(app_users)")])

    def test_copy_preserves_live_wal_and_rejects_loss_wrong_schema_or_journal(self):
        """Upgrade работает только на копии, DML loss/FK/incomplete migration дают отказ."""
        mutations = {"ok": "ALTER TABLE records ADD COLUMN extra TEXT;",
                     "drop_unused": "ALTER TABLE records DROP COLUMN unused;",
                     "lost": "DELETE FROM records WHERE id=2;",
                     "changed": "UPDATE records SET value='changed';",
                     "table": "DELETE FROM child; DROP TABLE records;",
                     "foreign": "INSERT INTO child VALUES (2, 999);",
                     "journal": "SELECT 1;", "fingerprint": "SELECT 1;"}
        for kind, sql in mutations.items():
            with self.subTest(kind=kind), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "live.db"
                with closing(sqlite3.connect(source)) as live:
                    live.execute("PRAGMA journal_mode=WAL")
                    live.executescript("CREATE TABLE schema_migrations(version INTEGER, name TEXT);"
                        "INSERT INTO schema_migrations VALUES (1, '0001_initial.sql');"
                        "CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT, unused TEXT);"
                        "CREATE TABLE child(id INTEGER PRIMARY KEY, owner INTEGER REFERENCES records(id));"
                        "INSERT INTO records VALUES (1, 'private live row', NULL);"
                        "INSERT INTO child VALUES (1, 1);")
                    live.execute("INSERT INTO records VALUES (2, 'WAL row', NULL)")
                    live.commit()
                    probe = root / "probe"
                    probe.mkdir()
                    # Fingerprint вычисляется на независимой synthetic целевой схеме.
                    with closing(sqlite3.connect(":memory:")) as target:
                        live.backup(target)
                        target.executescript(sql)
                        fingerprint = gate.schema_fingerprint(target)
                    if kind == "fingerprint":
                        fingerprint = "0" * 64

                    def migrate(directory):
                        """Применяет fixture SQL только к writable копии."""
                        with closing(sqlite3.connect(directory / "app.db")) as connection:
                            connection.executescript(sql)
                            if kind != "journal":
                                connection.execute("INSERT INTO schema_migrations VALUES (2, '0002_change.sql')")
                            connection.commit()

                    arguments = (source, probe, ["0001_initial.sql"],
                                 ["0001_initial.sql", "0002_change.sql"], fingerprint, migrate)
                    if kind in ("ok", "drop_unused"):
                        gate.verify_copy(*arguments)
                    else:
                        codes = {"lost": "SAFE_COPY_DATA_LOSS", "changed": "SAFE_COPY_DATA_LOSS",
                                 "table": "SAFE_COPY_TABLE_LOSS", "foreign": "SAFE_COPY_FOREIGN_KEYS",
                                 "journal": "SAFE_COPY_CANDIDATE_SCHEMA", "fingerprint": "SAFE_COPY_FINGERPRINT"}
                        with self.assertRaisesRegex(gate.PredeployError, codes[kind]):
                            gate.verify_copy(*arguments)
                    self.assertEqual(live.execute("SELECT value FROM records ORDER BY id").fetchall(),
                                     [("private live row",), ("WAL row",)])
                    self.assertEqual(live.execute("SELECT COUNT(*) FROM schema_migrations").fetchone(), (1,))


class HostPredeployTests(unittest.TestCase):
    """Проверяет реальную композицию predeploy и контейнерную границу доступа."""

    def test_host_uses_private_copy_and_rechecks_evidence_after_migration(self):
        """Host вызывает мигратор только с копией и отвергает отозванный результат."""
        for revoked in (False, True):
            with self.subTest(revoked=revoked), TemporaryDirectory() as temporary:
                host = FakeHost(Path(temporary))
                source = host.checkout / "data/app.db"
                with closing(sqlite3.connect(source)) as connection:
                    fingerprint = gate.schema_fingerprint(connection)
                evidence = {"artifact_id": 22, "artifact_digest": "sha256:" + "c" * 64,
                            "policy_sha": POLICY_SHA, "schema_fingerprint": fingerprint}
                good = prod.prep.read_state(host.state_dir, host.checkout)["last_good"]
                paths = []

                def migrate(image_id, directory):
                    """Проверяет, что workspace с рабочей БД недоступен мигратору."""
                    self.assertEqual(image_id, NEW_IMAGE)
                    self.assertNotEqual(directory, source.parent)
                    self.assertTrue(directory.is_relative_to(host.state_dir))
                    self.assertTrue((directory / "app.db").is_file())
                    self.assertEqual(list(directory.iterdir()), [directory / "app.db"])
                    paths.append(directory)

                checks = [evidence, dict(evidence, artifact_id=23) if revoked else evidence]
                with patch.object(gate, "private_text", return_value=POLICY_SHA), patch.object(
                    gate, "verify_evidence", side_effect=checks,
                ) as verify, patch.object(host, "migrate_copy", side_effect=migrate):
                    arguments = (host, host.select("v1.1.0"), NEW_IMAGE,
                                 ["0001_initial_schema.sql"], ["0001_initial_schema.sql"],
                                 good, OLD_SHA, OLD_IMAGE)
                    if revoked:
                        with self.assertRaisesRegex(gate.PredeployError, "SAFE_EVIDENCE_CHANGED"):
                            prod.Host.predeploy(*arguments)
                    else:
                        prod.Host.predeploy(*arguments)
                    self.assertEqual(verify.call_count, 2)
                self.assertEqual(len(paths), 1)
                self.assertFalse(paths[0].exists())
                self.assertTrue(host.running)
                self.assertNotIn("marker_on", host.actions)

    def test_copy_container_has_no_live_mount_or_secrets_and_timeout_removes_it(self):
        """Timeout не оставляет контейнер, которому доступна приватная копия."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            directory = host.state_dir / "private-copy"
            with patch.object(prod.subprocess, "run", side_effect=[
                subprocess.TimeoutExpired("docker", 180), subprocess.CompletedProcess([], 0),
            ]) as run, self.assertRaises(subprocess.TimeoutExpired):
                host.migrate_copy(NEW_IMAGE, directory)
            command = run.call_args_list[0].args[0]
            self.assertIn(f"{directory}:/app/data:rw", command)
            self.assertNotIn(str(host.checkout / "data"), " ".join(command))
            self.assertNotIn("--env-file", command)
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertEqual(set(run.call_args_list[0].kwargs["env"]), {"PATH", "DOCKER_HOST"})
            self.assertEqual(run.call_args_list[1].args[0][:3], ["docker", "rm", "-f"])


class AdmissionBoundaryTests(unittest.TestCase):
    """Связывает настоящий evidence verifier с state machine и безопасной диагностикой."""

    def test_evidence_failures_reach_operator_and_report_before_stop(self):
        """Mismatch SHA, missing test, digest и API outage сохраняют точный код."""
        faults = {"baseline": "SAFE_INSTALLED_PREVIOUS_SHA",
                  "missing_test": "SAFE_TEST_MANIFEST", "digest": "SAFE_ARTIFACT_DIGEST",
                  "api": "SAFE_EVIDENCE_READ"}
        for fault, code in faults.items():
            for emergency in (False, True):
                with self.subTest(fault=fault, emergency=emergency), TemporaryDirectory() as temporary:
                    host = FakeHost(Path(temporary))
                    api = ArtifactAPI()
                    if fault != "baseline":
                        api.policy["previous_sha"] = OLD_SHA
                        api.trusted["repository_compatibility_policy.json"] = json.dumps(api.policy).encode()
                        api.evidence["policy_files"] = {name: hashlib.sha256(data).hexdigest()
                                                       for name, data in api.trusted.items()}
                        api.evidence["previous_sha"] = OLD_SHA
                        api.report["previous_sha"] = OLD_SHA
                    if fault == "missing_test":
                        api.report["old_repository_behavior"]["old_tests"]["test_ids"].pop()
                    api.pack()
                    if fault == "digest":
                        api.artifact["digest"] = "sha256:" + "0" * 64
                    if emergency:
                        api.selected = replace(api.selected, kind="emergency")
                    real_predeploy = prod.Host.predeploy
                    stderr = io.StringIO()
                    with ExitStack() as stack:
                        for patcher in deploy_tests.DeployProdTests()._patches():
                            stack.enter_context(patcher)
                        stack.enter_context(patch.object(host, "select", return_value=api.selected))
                        stack.enter_context(patch.object(host, "select_emergency", return_value=api.selected))
                        stack.enter_context(patch.object(host, "predeploy", side_effect=lambda *args:
                                                        real_predeploy(host, *args)))
                        stack.enter_context(patch.object(gate, "private_text", return_value=POLICY_SHA))
                        stack.enter_context(patch.object(gate, "EvidenceAPI", return_value=api))
                        if fault == "api":
                            stack.enter_context(patch.object(api, "get", side_effect=OSError("secret token/private row")))
                        with self.assertRaises(prod.DeployError) as failure:
                            prod.deploy(host, "v3.2.0", emergency=emergency,
                                        reason="fixture" if emergency else None)
                    argv = ["deploy-prod", "v3.2.0"]
                    with patch.object(prod, "os", SimpleNamespace(name="posix", geteuid=lambda: 0)), \
                            patch.object(prod, "Host", return_value=host), \
                            patch.object(prod, "deploy", side_effect=failure.exception), \
                            patch.object(sys, "argv", argv), redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as stopped:
                            prod.main()
                        self.assertEqual(stopped.exception.code, 1)
                    state = prod.prep.read_state(host.state_dir, host.checkout)
                    self.assertEqual(state["stage"], "failed_preflight")
                    self.assertEqual(state["error"], code)
                    report = json.loads(next(host.state_dir.glob("metrics-*.json")).read_text())
                    self.assertEqual(report["predeploy_reason"], code)
                    self.assertIn(code, stderr.getvalue())
                    self.assertNotIn("secret token/private row", stderr.getvalue() + json.dumps(report))
                    self.assertNotIn("marker_on", host.actions)
                    self.assertNotIn("stop_both", host.actions)
                    self.assertTrue(host.running)
                    self.assertEqual(host.head, OLD_SHA)

    def test_preparation_schema_and_image_checks_have_safe_codes(self):
        """Ранние проверки до evidence сохраняют код вместо generic DeployError."""
        faults = {"prefix": "SAFE_SCHEMA_PREFIX", "source": "SAFE_SOURCE_SCHEMA",
                  "image": "SAFE_CANDIDATE_IMAGE", "manifest": "SAFE_CANDIDATE_MANIFEST"}
        for fault, code in faults.items():
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                host = FakeHost(Path(temporary))
                with ExitStack() as stack:
                    for patcher in deploy_tests.DeployProdTests()._patches():
                        stack.enter_context(patcher)
                    if fault == "prefix":
                        stack.enter_context(patch.object(prod, "_schema_names", return_value=["0001_other.sql"]))
                    elif fault == "manifest":
                        stack.enter_context(patch.object(prod, "_schema_names", side_effect=OSError("secret")))
                    else:
                        method = "validate_schema" if fault == "source" else "check_candidate_image"
                        stack.enter_context(patch.object(prod.prep, method, side_effect=ValueError("secret")))
                    with self.assertRaises(prod.DeployError) as failure:
                        prod.deploy(host, "v1.1.0")
                self.assertEqual(failure.exception.code, code)
                self.assertIn(code, str(failure.exception))
                self.assertNotIn("secret", str(failure.exception))
                state = prod.prep.read_state(host.state_dir, host.checkout)
                self.assertEqual(state["error"], code)
                report = json.loads(next(host.state_dir.glob("metrics-*.json")).read_text())
                self.assertEqual(report["predeploy_reason"], code)
                self.assertNotIn("marker_on", host.actions)
                self.assertNotIn("stop_both", host.actions)
                self.assertEqual(host.head, OLD_SHA)
