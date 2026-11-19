"""Воспроизводит HTTP-маршруты PATCH на локальных фикстурах."""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, chdir, closing, redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import AbstractHTTPHandler, Request
from urllib.response import addinfourl

from scripts import deploy_acceptance, deploy_cleanup_old, deploy_prod, release_production
from scripts import predeploy_compatibility as compatibility
from scripts.create_release_branch import EXPECTED_REPOSITORY as REPOSITORY
from scripts.deployment_acceptance import AcceptedBaseline, ENVIRONMENT, TASK
from scripts.full_ci_admission import RELEASE_WORKFLOW
from scripts.full_ci_controller import GitHub
from scripts.publish_installation_tag import InstallationReceipt, InstallationTagError
from scripts.release_sqlite_summary import MIGRATIONS_PREFIX
from tests import test_deploy_prod as system
from tests.test_deployment_acceptance import OWNER
from tests.test_github_http_metrics import _response
from tests.test_predeploy_compatibility import ArtifactAPI, POLICY_SHA
from tests.test_select_deploy_release import FakeApi as SelectionFixture


PREVIOUS_VERSION = "v4.2.0"
VERSION = "v4.2.1"
BRANCH = "release/4.2"
MIGRATION = "0001_initial_schema.sql"
RELEASE_ID = 42
HISTORY_SIZE = 16
RUNNER_TOKEN = "fixture-runner"
VPS_TOKEN = "fixture-vps"
STORAGE = "https://storage.example.invalid/fixture?sig=fixture"
GENERATION = "1" * 32
RUNTIME = "2" * 64


def _git(checkout: Path, *arguments: str, data: str | None = None) -> str:
    environment = os.environ | {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                                 "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    command = ("git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *arguments)
    payload = data.encode() if data is not None else None
    return subprocess.run(command, cwd=checkout, env=environment, input=payload,
                          capture_output=True, check=True).stdout.decode().strip()


@dataclass(frozen=True)
class _Graph:
    checkout: Path
    base: str
    candidate: str
    main: str
    pairs: list[tuple[str, str]]


def _make_graph(checkout: Path, workflow_bytes: bytes) -> _Graph:
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "--object-format=sha1")
    _git(checkout, "config", "core.autocrlf", "false")
    _git(checkout, "config", "core.hooksPath", ".git/fixture-hooks")
    _git(checkout, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    (checkout / ".gitignore").write_text("data/\n", encoding="utf-8", newline="\n")
    for name in compatibility.WORKFLOWS:
        path = checkout / ".github" / "workflows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(workflow_bytes)
    migration = checkout / MIGRATIONS_PREFIX / MIGRATION
    migration.parent.mkdir(parents=True)
    migration.write_bytes(b"-- fixture migration\n")
    _git(checkout, "add", ".")
    tree = _git(checkout, "write-tree")

    def commit(label: str, *parents: str) -> str:
        parent_flags = [item for parent in parents for item in ("-p", parent)]
        return _git(checkout, "commit-tree", tree, *parent_flags, "-m", label)

    def merge(label: str, parent: str) -> str:
        return commit(label, parent, commit(f"topic {label}", parent))

    base = candidate = main = commit("base")
    pairs = []
    for index in (1, 2):
        candidate = merge(f"release PR {index}", candidate)
        main = merge(f"main PR {index}", main)
        pairs.append((candidate, main))
    _git(checkout, "update-ref", "refs/heads/main", main)
    _git(checkout, "update-ref", f"refs/heads/{BRANCH}", candidate)
    _git(checkout, "checkout", "--quiet", "--detach", candidate)
    return _Graph(checkout, base, candidate, main, pairs)


def _deployment(identifier: int, receipt: InstallationReceipt) -> dict:
    payload = asdict(receipt)
    del payload["runtime_id"]
    return {"id": identifier, "sha": receipt.sha, "ref": receipt.sha, "task": TASK, "environment": ENVIRONMENT,
            "transient_environment": False, "production_environment": True, "creator": OWNER, "payload": payload}


def _status(identifier: int, minute: int) -> dict:
    return {"id": identifier, "state": "success", "environment": ENVIRONMENT, "creator": OWNER,
            "created_at": f"2026-01-01T10:{minute:02}:00Z"}


class _RestFixture:
    def __init__(self, graph: _Graph, fingerprint: str) -> None:
        self.graph = graph
        self.artifact = ArtifactAPI()
        self.artifact.use_full_discovery_policy()
        selection = SelectionFixture(VERSION)
        self.run = selection.run | {"head_sha": graph.candidate, "head_branch": BRANCH,
                                    "html_url": f"https://github.com/{REPOSITORY}/actions/runs/1/attempts/1"}
        self.jobs = [job | {"head_sha": graph.candidate} for job in selection.jobs]
        self.receipt = InstallationReceipt(1, VERSION, graph.candidate, "ordinary", RELEASE_ID, 1, 1,
                                           GENERATION, MIGRATION, RUNTIME)
        self.releases: dict[str, dict] = {}
        self.refs: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}
        self.deployments: dict[int, dict] = {}
        self.statuses: dict[int, list[dict]] = {}
        self.pulls: dict[int, dict] = {}
        self.calls: Counter[tuple[str, str, str]] = Counter()
        self.posts: list[str] = []
        self.rule = {"id": 1, "target": "tag", "enforcement": "active", "bypass_actors": [],
                     "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
                     "rules": [{"type": "update"}, {"type": "deletion"}]}
        self._seed_history()
        self._seed_pairs()
        baseline = AcceptedBaseline(graph.base, PREVIOUS_VERSION, HISTORY_SIZE, HISTORY_SIZE, HISTORY_SIZE + 100)
        self.artifact.report.update(previous_sha=graph.base, candidate_sha=graph.candidate)
        self.artifact.report["previous_git"]["head"] = graph.base
        self.artifact.report["candidate_migrations_data_integrity"].update(
            fresh_fingerprint=fingerprint, upgraded_fingerprint=fingerprint)
        self.artifact.report["old_repository_behavior"]["schema_fingerprints"] = [fingerprint]
        self.artifact.report["candidate_verified_previous_updates"]["schema_fingerprint"] = fingerprint
        self.artifact.evidence.update(previous_sha=graph.base, candidate_sha=graph.candidate, baseline=asdict(baseline),
                                      workflow_ref=f"{REPOSITORY}/.github/workflows/{RELEASE_WORKFLOW}@refs/heads/{BRANCH}")
        self.artifact.artifact["workflow_run"]["head_sha"] = graph.candidate
        self.artifact.pack()

    def _seed_history(self) -> None:
        for identifier in range(1, HISTORY_SIZE + 1):
            latest = identifier == HISTORY_SIZE
            tag = PREVIOUS_VERSION if latest else f"v4.1.{identifier}"
            sha = self.graph.base if latest else f"{identifier:040x}"
            branch = BRANCH if latest else "release/4.1"
            body = (f"Тип выпуска: обычный\nCommit: `{sha}`\nВетка: `{branch}`\n"
                    f"Полный CI: https://github.com/{REPOSITORY}/actions/runs/2/attempts/1\n")
            self.releases[tag] = {"id": identifier, "tag_name": tag, "name": tag, "target_commitish": sha,
                                  "body": body, "draft": False, "prerelease": False, "immutable": True,
                                  "published_at": "2026-01-01T00:00:00Z"}
            self.refs[tag] = {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": sha}}
            receipt = InstallationReceipt(1, tag, sha, "ordinary", identifier, 2, 1, "3" * 32, MIGRATION, RUNTIME)
            self.deployments[identifier] = _deployment(identifier, receipt)
            self.statuses[identifier] = [_status(identifier + 100, identifier)]

    def _seed_pairs(self) -> None:
        for index, (release_sha, main_sha) in enumerate(self.graph.pairs, 1):
            release, main = index, index + 10
            for number, branch, sha, other in ((release, BRANCH, release_sha, main), (main, "main", main_sha, release)):
                self.pulls[number] = {"number": number, "base": {"ref": branch}, "merge_commit_sha": sha,
                                      "merged_at": "2026-01-01T08:00:00Z",
                                      "body": f"Hotfix-Links:\nhttps://github.com/{REPOSITORY}/pull/{other}\n"}

    def seed_release(self, published: _RestFixture, *, installed: bool = False) -> None:
        """Подставляет точно такой же опубликованный Release и при необходимости незавершённую приёмку."""
        self.releases[VERSION] = published.releases[VERSION]
        ref = self.refs[VERSION] = published.refs[VERSION]
        self.tags[ref["object"]["sha"]] = published.tags[ref["object"]["sha"]]
        if installed:
            self.deployments[HISTORY_SIZE + 1] = _deployment(HISTORY_SIZE + 1, self.receipt)

    def _list(self, path: str, query: dict[str, str]) -> object:
        key = None
        if path == "/releases":
            items = list(self.releases.values())
        elif path == "/git/matching-refs/tags/v":
            items = list(self.refs.values())
        elif path == "/rulesets":
            items = [self.rule]
        elif path == "/deployments":
            items = [item for item in self.deployments.values() if query.get("sha", item["sha"]) == item["sha"]]
        elif path.startswith("/deployments/") and path.endswith("/statuses"):
            items = self.statuses.get(int(path.split("/")[2]), [])
        elif path.startswith("/commits/") and path.endswith("/pulls"):
            sha = path.split("/")[2]
            items = [item for item in self.pulls.values() if item["merge_commit_sha"] == sha]
        elif path == f"/actions/workflows/{RELEASE_WORKFLOW}/runs":
            items, key = [self.run], "workflow_runs"
        elif path == "/actions/runs/1/attempts/1/jobs":
            items, key = self.jobs, "jobs"
        elif path == "/actions/runs/1/artifacts":
            items, key = [self.artifact.artifact], "artifacts"
        else:
            raise AssertionError(f"Unexpected list route: {path}")
        size = int(query["per_page"])
        offset = (int(query["page"]) - 1) * size
        page = items[offset:offset + size]
        return {key: page} if key else page

    def _read(self, path: str, query: dict[str, str]) -> object:
        if "page" in query:
            return self._list(path, query)
        if path == "/user":
            return OWNER
        if path == "/immutable-releases":
            return {"enabled": True}
        if path == "/rulesets/1":
            return self.rule
        if path.startswith("/releases/tags/"):
            return self.releases.get(path.removeprefix("/releases/tags/"))
        if path in {"/git/ref/heads/main", f"/git/ref/heads/{BRANCH}"}:
            branch = path.removeprefix("/git/ref/heads/")
            return {"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": (
                self.graph.main if branch == "main" else self.graph.candidate)}}
        if path.startswith("/git/ref/tags/"):
            return self.refs[path.removeprefix("/git/ref/tags/")]
        if path.startswith("/git/commits/"):
            sha = path.removeprefix("/git/commits/")
            current, *parents = _git(self.graph.checkout, "rev-list", "--parents", "-n", "1", sha).split()
            return {"sha": current, "parents": [{"sha": parent} for parent in parents]}
        if path.startswith("/git/tags/"):
            return self.tags[path.removeprefix("/git/tags/")]
        if path.startswith("/pulls/"):
            return self.pulls[int(path.removeprefix("/pulls/"))]
        if path.startswith("/compare/"):
            sha, head = path.removeprefix("/compare/").split("...")
            if self.graph.pairs[0][1] != sha or self.graph.main != head:
                raise AssertionError(f"Unexpected comparison: {path}")
            return {"status": "ahead", "base_commit": {"sha": sha}, "merge_base_commit": {"sha": sha}}
        if path == f"/actions/workflows/{RELEASE_WORKFLOW}":
            return {"id": self.run["workflow_id"], "path": f".github/workflows/{RELEASE_WORKFLOW}"}
        if path == "/actions/variables/COMPATIBILITY_POLICY_SHA":
            return {"name": "COMPATIBILITY_POLICY_SHA", "value": POLICY_SHA}
        if path == "/actions/runs/1/attempts/1":
            return self.run
        if path == f"/contents/{MIGRATIONS_PREFIX.rstrip('/')}":
            return [{"name": MIGRATION, "path": MIGRATIONS_PREFIX + MIGRATION, "type": "file"}]
        if path.startswith("/contents/"):
            return self.artifact.get(path, **query)
        raise AssertionError(f"Unexpected read route: {path}")

    def _write(self, path: str, body: dict) -> dict:
        self.posts.append(path)
        if path == "/git/tags":
            data = (f"object {body['object']}\ntype commit\ntag {body['tag']}\n"
                    f"tagger Fixture <fixture@example.invalid> 1767225600 +0000\n\n{body['message']}\n")
            sha = _git(self.graph.checkout, "mktag", data=data)
            record = {"sha": sha, "tag": body["tag"], "message": body["message"],
                      "object": {"type": "commit", "sha": body["object"]}}
            self.tags[sha] = record
            return record
        if path == "/git/refs":
            _git(self.graph.checkout, "update-ref", body["ref"], body["sha"])
            record = {"ref": body["ref"], "object": {"type": "tag", "sha": body["sha"]}}
            self.refs[body["ref"].removeprefix("refs/tags/")] = record
            return record
        if path == "/releases":
            record = body | {"id": RELEASE_ID, "immutable": True, "published_at": "2026-01-01T12:00:00Z",
                             "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{VERSION}"}
            self.releases[body["tag_name"]] = record
            return record
        identifier = HISTORY_SIZE + 1
        if path == "/deployments":
            record = _deployment(identifier, self.receipt)
            if body["payload"] != record["payload"]:
                raise AssertionError("Unexpected Deployment payload")
            self.deployments[identifier] = record
            return record
        if path == f"/deployments/{identifier}/statuses" and body["state"] == "success":
            record = _status(identifier + 100, identifier)
            self.statuses[identifier] = [record]
            return record
        raise AssertionError(f"Unexpected write route: {path}")

    def transport(self, connection, request: Request, **kwargs) -> addinfourl:
        """Возвращает HTTP-ответ и ведёт независимый счётчик сетевых переходов."""
        if request.full_url == STORAGE:
            if request.get_header("Authorization") is not None:
                raise AssertionError("Authorization on artifact storage")
            self.calls["runner", "artifact_storage", "GET"] += 1
            return _response(request, self.artifact.payload)
        parts = urlsplit(request.full_url)
        if parts.netloc != "api.github.com":
            raise AssertionError(f"Unexpected HTTP destination: {parts.netloc}")
        path = unquote(parts.path).removeprefix(f"/repos/{REPOSITORY}")
        method = request.get_method()
        authorization = request.get_header("Authorization")
        location = {f"Bearer {RUNNER_TOKEN}": "runner", f"Bearer {VPS_TOKEN}": "vps"}[authorization]
        self.calls[location, "github_api", method] += 1
        if path == "/actions/artifacts/22/zip":
            if location != "runner":
                raise AssertionError("Annotated controller should not download CI ZIP")
            return _response(request, b"", code=302, location=STORAGE)
        if method == "POST":
            record = self._write(path, json.loads(request.data))
            return _response(request, json.dumps(record).encode(), code=201)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        value = self._read(path, query)
        return _response(request, json.dumps(value).encode(), code=404 if value is None else 200)


class _ControllerFixture:
    def __init__(self, root: Path, api: _RestFixture, installed: bool) -> None:
        root.mkdir()
        self.api = api
        self.host = system.FakeHost(root)
        fixture_data = self.host.checkout / "data"
        data = api.graph.checkout / "data"
        data.mkdir(exist_ok=True)
        (data / "app.db").write_bytes((fixture_data / "app.db").read_bytes())
        self.host.checkout = api.graph.checkout
        state = deploy_prod.prep.read_state(self.host.state_dir, self.host.checkout)
        state["last_good"]["version"] = PREVIOUS_VERSION
        deploy_prod.prep.write_state(self.host.state_dir, self.host.checkout, state)
        self.installed = installed
        self.calls: list[str] = []
        self.errors = ""

    def _transition(self, host, state: dict, save, write) -> None:
        """Моделирует системное переключение после настоящего допуска контроллера."""
        host.current_image = system.NEW_IMAGE
        host.running = True
        state.update(protocol_version=deploy_prod.database_switch.PROTOCOL_VERSION,
                     snapshot={"path": str(host.state_dir / "backup.db")})
        self.installed = True

    def _verify(self, host, version: str, sha: str) -> dict:
        if not self.installed or (version, sha) != (VERSION, self.api.graph.candidate):
            raise AssertionError("Unexpected verification target")
        return asdict(self.api.receipt)

    def _cleanup(self, host, version: str, sha: str, runtime: str) -> dict:
        if not self.installed or (version, sha, runtime) != (VERSION, self.api.graph.candidate, RUNTIME):
            raise AssertionError("Unexpected cleanup target")
        return {"version": 1, "transition_id": "3" * 32, "old_generation_id": "legacy",
                "old_host_path": "/fixture/data/app.db", "old_device": 1, "old_inode": 2,
                "new_generation_id": GENERATION}

    def execute(self, arguments: tuple[str, ...], **kwargs) -> subprocess.CompletedProcess[str]:
        """Запускает CLI контроллера с захваченными stdout и stderr одного SSH-вызова."""
        host = self.host
        command = list(arguments[3:])
        self.calls.append(command[1] if len(command) > 1 else "install")
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for patcher in system.DeployProdTests()._patches():
                stack.enter_context(patcher)
            host_type = deploy_prod.Host
            for patcher in (
                patch.object(host, "select", side_effect=lambda version: host_type.select(host, version)),
                patch.object(host, "predeploy", side_effect=lambda *args, **opts: host_type.predeploy(host, *args, **opts)),
                patch.object(host, "uses_selector_runtime", return_value=True),
                patch.object(host, "require_switch_rollout"), patch.object(host, "migrate_copy"),
                patch.object(host, "release_api", return_value=GitHub(REPOSITORY, VPS_TOKEN)),
                patch.object(host, "finalize_database"),
                patch.object(compatibility, "private_text", side_effect=lambda path:
                             POLICY_SHA if path.name == "compatibility-policy.sha" else VPS_TOKEN),
                patch.object(deploy_prod.database_switch, "register_initial"),
                patch.object(deploy_prod.database_switch, "transition", side_effect=self._transition),
                patch.object(deploy_acceptance, "verify_installed", side_effect=self._verify),
                patch.object(deploy_cleanup_old, "cleanup_old", side_effect=self._cleanup),
                patch.object(deploy_prod, "Host", return_value=host),
                patch.object(deploy_prod, "os", SimpleNamespace(**(vars(os) | {"name": "posix", "geteuid": lambda: 0}))),
                patch.object(deploy_prod, "_atomic_text", side_effect=lambda path, value:
                             path.write_text(value, encoding="utf-8")),
                patch.object(sys, "argv", ["deploy-prod", *command]),
                redirect_stdout(stdout), redirect_stderr(stderr),
            ):
                stack.enter_context(patcher)
            try:
                deploy_prod.main()
            except SystemExit as error:
                self.errors = stderr.getvalue()
                failure = subprocess.CalledProcessError(error.code, arguments, output=stdout.getvalue(), stderr=self.errors)
                raise InstallationTagError("Синтетическая команда VPS остановилась") from failure
        return subprocess.CompletedProcess(arguments, 0, stdout.getvalue(), stderr.getvalue())


def _measure(root: Path, mode: str, api: _RestFixture, *, installed: bool = False) -> dict:
    stdout, stderr = io.StringIO(), io.StringIO()
    notes = root / "notes.md"
    notes.write_text("Фикстура PATCH\n", encoding="utf-8")
    flags = ["--resume"] if installed else ["--release", "--change", "patch", "--notes-file", str(notes)]
    sha = api.graph.main if mode == "wrong_sha" else api.graph.candidate
    argv = ["release_production", VERSION, sha, "--ssh-host", "vps.example.invalid",
            "--known-hosts", str(notes), *flags]
    with (patch.object(system, "OLD_SHA", api.graph.base), patch.object(system, "NEW_SHA", api.graph.candidate),
          chdir(api.graph.checkout)):
        controller = _ControllerFixture(root / mode, api, installed)
        with (patch.object(release_production, "run_vps_command", side_effect=controller.execute),
              patch.object(AbstractHTTPHandler, "do_open", side_effect=api.transport),
              patch.object(sys, "argv", argv), patch.dict(os.environ, {"GITHUB_TOKEN": RUNNER_TOKEN}),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            started = perf_counter_ns()
            exit_code = 0
            try:
                release_production.main()
            except SystemExit as error:
                exit_code = error.code
            elapsed = perf_counter_ns() - started
    reports = [json.loads(line.removeprefix("deploy-http-metrics ")) for line in stderr.getvalue().splitlines()
               if line.startswith("deploy-http-metrics ")]
    if mode == "wrong_sha":
        if exit_code != 1 or "Candidate не является текущим tip" not in stderr.getvalue():
            raise AssertionError(stderr.getvalue())
    elif exit_code != 0 or json.loads(stdout.getvalue().splitlines()[-1])["state"] != "success":
        raise AssertionError(f"{mode}: {stderr.getvalue()}\n{stdout.getvalue()}\n{controller.errors}")
    measured = Counter()
    for report in reports:
        for row in report["http"]:
            measured[row["location"], row["target"], row["method"]] += row["attempts"]
    if measured != api.calls:
        raise AssertionError(f"HTTP metrics differ from the transport: {measured} != {api.calls}")
    if len(reports) != len(controller.calls) + 1:
        raise AssertionError("Unexpected number of runner and controller reports")
    return {"scenario": mode, "exit_code": exit_code, "wall_ns": elapsed, "reports": reports, "posts": api.posts,
            "ssh": controller.calls}


def measure_baseline() -> list[dict]:
    """Измеряет четыре сценария с одинаковым PATCH и историей выпусков."""
    with TemporaryDirectory(prefix="patch-http-baseline-") as directory:
        root = Path(directory)
        graph = _make_graph(root / "checkout", ArtifactAPI().workflow_bytes)
        schema = root / "schema.db"
        with closing(sqlite3.connect(schema)) as database:
            database.execute("CREATE TABLE schema_migrations (version INTEGER, name TEXT)")
            database.execute("INSERT INTO schema_migrations VALUES (1, ?)", (MIGRATION,))
            fingerprint = compatibility.schema_fingerprint(database)

        first = _RestFixture(graph, fingerprint)
        published = _measure(root, "publish", first)
        ready = _RestFixture(graph, fingerprint)
        ready.seed_release(first)
        installed = _measure(root, "install_ready", ready)
        resumed = _RestFixture(graph, fingerprint)
        resumed.seed_release(first, installed=True)
        continued = _measure(root, "resume", resumed, installed=True)
        _git(graph.checkout, "checkout", "--quiet", "--detach", graph.main)
        refused = _measure(root, "wrong_sha", _RestFixture(graph, fingerprint))
        return [published, installed, continued, refused]


def _summary(scenario: dict) -> dict:
    totals = {location: dict.fromkeys(("get", "post", "pages", "errors", "http_ns", "base_get", "storage"), 0)
              for location in ("runner", "vps")}
    for report in scenario["reports"]:
        for row in report["http"]:
            total = totals[row["location"]]
            if row["target"] == "artifact_storage":
                total["storage"] += row["attempts"]
            else:
                total[row["method"].lower()] += row["attempts"]
            total["pages"] += row["pages"]
            total["errors"] += row["errors"]
            total["http_ns"] += row["elapsed_ns"]
            if row["phase"] == "deployment_base" and row["method"] == "GET":
                total["base_get"] += row["attempts"]
    return {"scenario": scenario["scenario"], "wall_ns": scenario["wall_ns"], **totals}


class PatchHttpBaselineTests(unittest.TestCase):
    """Проверяет маршруты и соответствие метрик реальным вызовам транспорта."""

    def test_patch_routes(self) -> None:
        """Определяет зафиксированные операции публикации, установки, resume и отказа."""
        publish, ready, resume, refused = measure_baseline()
        status = f"/deployments/{HISTORY_SIZE + 1}/statuses"
        self.assertEqual(publish["posts"], ["/git/tags", "/git/refs", "/releases", "/deployments", status])
        self.assertEqual(ready["posts"], ["/deployments", status])
        self.assertEqual(resume["posts"], [status])
        self.assertEqual((refused["posts"], refused["ssh"]), ([], []))
        self.assertEqual([publish["ssh"][0], ready["ssh"][0], resume["ssh"][0]],
                         ["install", "install", "--verify-installed"])


if __name__ == "__main__":
    scenarios = measure_baseline()
    print(json.dumps({"summary": [_summary(scenario) for scenario in scenarios], "scenarios": scenarios}, indent=2))
