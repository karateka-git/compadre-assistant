"""Воспроизводит HTTP-маршруты PATCH на локальных фикстурах."""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, chdir, closing, redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import AbstractHTTPHandler, Request
from urllib.response import addinfourl

from scripts import deploy_acceptance, deploy_attempt, deploy_cleanup_old, deploy_prod, publish_release, release_production
from scripts import predeploy_compatibility as compatibility
from scripts.ci_repository_compatibility import DeploymentReleaseTagEvidence
from scripts.create_release_branch import EXPECTED_REPOSITORY as REPOSITORY
from scripts.deployment_acceptance import AcceptedBaseline, DeploymentAttempt, ENVIRONMENT, TASK
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
SOURCE_RUN = 12001
RESUME_RUN = 13002
SNAPSHOT = "c" * 40
BASE_GENERATION = "3" * 32


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


def _main_checkout(graph: _Graph, checkout: Path) -> Path:
    """Воспроизводит shallow checkout из workflow без Git-объекта кандидата."""
    _git(graph.checkout, "clone", "--quiet", "--depth", "1", "--single-branch", "--branch", "main",
         graph.checkout.as_uri(), str(checkout))
    _git(checkout, "remote", "set-url", "origin", f"https://github.com/{REPOSITORY}.git")
    candidate = subprocess.run(("git", "cat-file", "-e", f"{graph.candidate}^{{commit}}"),
                               cwd=checkout, capture_output=True)
    if _git(checkout, "rev-parse", "HEAD") != graph.main or candidate.returncode == 0:
        raise AssertionError("Runner checkout must contain main without the release candidate")
    return checkout


def _fixture(root: Path) -> tuple[_Graph, Path, str]:
    """Создаёт независимый checkout runner, граф GitHub/VPS и схему SQLite."""
    graph = _make_graph(root / "checkout", ArtifactAPI().workflow_bytes)
    runner = _main_checkout(graph, root / "runner")
    with closing(sqlite3.connect(root / "schema.db")) as database:
        database.execute("CREATE TABLE schema_migrations (version INTEGER, name TEXT)")
        database.execute("INSERT INTO schema_migrations VALUES (1, ?)", (MIGRATION,))
        return graph, runner, compatibility.schema_fingerprint(database)


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
        self.release_tip = graph.candidate
        self.calls: Counter[tuple[str, str, str]] = Counter()
        self.publisher_gets: Counter[tuple[str, str]] = Counter()
        self.posts: list[str] = []
        self.rule = {"id": 1, "target": "tag", "enforcement": "active", "bypass_actors": [],
                     "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
                     "rules": [{"type": "update"}, {"type": "deletion"}]}
        self._seed_history()
        self._seed_pairs()
        baseline = self._baseline()
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

    def _baseline(self) -> AcceptedBaseline:
        return AcceptedBaseline(self.graph.base, PREVIOUS_VERSION, HISTORY_SIZE, HISTORY_SIZE, HISTORY_SIZE + 100)

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
                self.graph.main if branch == "main" else self.release_tip)}}
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
        target = "github_graphql_query" if path == "/graphql" else "github_api"
        self.calls[location, target, method] += 1
        if path == "/graphql":
            if method != "POST":
                raise AssertionError("Expected a GraphQL query via POST")
            value = self._graphql(location, json.loads(request.data))
            return _response(request, json.dumps({"data": value}).encode())
        if path == "/actions/artifacts/22/zip":
            if location != "runner":
                raise AssertionError("Annotated controller should not download CI ZIP")
            return _response(request, b"", code=302, location=STORAGE)
        if method == "POST":
            record = self._write(path, json.loads(request.data))
            return _response(request, json.dumps(record).encode(), code=201)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        if location == "runner" and "/releases" not in self.posts:
            self.publisher_gets[path, query.get("ref", "")] += 1
        value = self._read(path, query)
        return _response(request, json.dumps(value).encode(), code=404 if value is None else 200)

    def _graphql(self, location: str, body: dict) -> dict:
        raise AssertionError("Unexpected GraphQL request with the v1 reader")


class _V2RestFixture(_RestFixture):
    def __init__(self, graph: _Graph, fingerprint: str, history_size: int, *, first_v2: bool = False) -> None:
        self.history_size = history_size
        self.first_v2 = first_v2
        self.source_finished = False
        self.lose_success_response = False
        self.graphql_routes = Counter()
        self.rest_reads = Counter()
        self.status_writes: list[str] = []
        super().__init__(graph, fingerprint)

    def _baseline(self) -> AcceptedBaseline:
        return AcceptedBaseline(self.graph.base, PREVIOUS_VERSION, HISTORY_SIZE, self.history_size,
                                self.history_size + 100)

    @staticmethod
    def _time(identifier: int) -> str:
        return (datetime(2026, 1, 1) + timedelta(minutes=identifier)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _seed_history(self) -> None:
        super()._seed_history()
        release_id = HISTORY_SIZE - 1
        base = self.deployments[HISTORY_SIZE]
        earlier = AcceptedBaseline(self.deployments[release_id]["sha"], f"v4.1.{release_id}",
                                   release_id, release_id, release_id + 100)
        if not self.first_v2:
            tag_sha = "f" * 40
            evidence = DeploymentReleaseTagEvidence(earlier.sha, POLICY_SHA, 2, 1, "e" * 64, earlier)
            self.refs[PREVIOUS_VERSION]["object"] = {"type": "tag", "sha": tag_sha}
            self.tags[tag_sha] = {"sha": tag_sha, "tag": PREVIOUS_VERSION, "message": evidence.message(),
                                  "object": {"type": "commit", "sha": self.graph.base}}
        for identifier in range(1, self.history_size + 1):
            record = base if identifier >= HISTORY_SIZE else self.deployments[identifier]
            payload = record["payload"]
            self.deployments[identifier] = record | {
                "id": identifier, "node_id": f"deployment-{identifier}", "created_at": self._time(identifier),
            }
            # Дополнительные попытки того же Release вернулись на прежнюю базу.
            if not self.first_v2 and identifier >= HISTORY_SIZE:
                self.deployments[identifier]["payload"] = asdict(DeploymentAttempt(
                    2, f"{20000 + identifier}:1", payload["version"], payload["sha"], payload["kind"],
                    payload["release_id"], payload["run_id"], payload["run_attempt"], earlier,
                ))
            restored = HISTORY_SIZE <= identifier < self.history_size
            description = (f"v1 restored={earlier.deployment_id}/{earlier.status_id}" if restored
                           else f"v1 gen={BASE_GENERATION} migration={MIGRATION}")
            self.statuses[identifier] = [{
                "id": identifier + 100, "node_id": f"status-{identifier + 100}",
                "state": "failure" if restored else "success", "created_at": self._time(identifier),
                "environment": ENVIRONMENT, "creator": OWNER, "description": description,
            }]

    def _node(self, record: dict) -> dict:
        statuses = self.statuses.get(record["id"], [])
        return {"id": record["node_id"], "createdAt": record["created_at"],
                "latestStatus": {"id": statuses[0]["node_id"]} if statuses else None}

    def _graphql(self, location: str, body: dict) -> dict:
        query, variables = body["query"], body["variables"]
        if not query.startswith("query"):
            raise AssertionError("GraphQL fixture accepts only read operations")
        if "id" in variables:
            identifier = int(variables["id"].removeprefix("deployment-"))
            self.graphql_routes[location, "node"] += 1
            return {"node": self._node(self.deployments[identifier])}
        order = re.search(r"field:\s*CREATED_AT,\s*direction:\s*DESC", query)
        if f"{variables['owner']}/{variables['name']}" != REPOSITORY or order is None:
            raise AssertionError("Deployment query must request newest first")
        sha = variables.get("sha")
        self.graphql_routes[location, "sha" if sha else "latest"] += 1
        deployments = [record for record in reversed(self.deployments.values()) if sha in (None, record["sha"])]
        connection = {"deployments": {"totalCount": len(deployments),
                                      "nodes": [self._node(record) for record in deployments[:2]]}}
        return {"repository": {"nameWithOwner": REPOSITORY} | (
            connection if sha is None else {"object": {"oid": sha} | connection}
        )}

    def _list(self, path: str, query: dict[str, str]) -> object:
        if path != "/deployments":
            return super()._list(path, query)
        items = [item for item in reversed(self.deployments.values()) if query.get("sha", item["sha"]) == item["sha"]]
        size, page = int(query["per_page"]), int(query["page"])
        return items[(page - 1) * size:page * size]

    def _read(self, path: str, query: dict[str, str]) -> object:
        if identifier := re.fullmatch(r"/deployments/(\d+)", path):
            return self.deployments.get(int(identifier[1]))
        if owner := re.fullmatch(rf"/actions/runs/({SOURCE_RUN}|{RESUME_RUN})/attempts/1", path):
            run = int(owner[1])
            repository = {"full_name": REPOSITORY}
            return {"repository": repository, "head_repository": repository, "id": run, "run_attempt": 1,
                    "path": ".github/workflows/production-release.yml", "event": "workflow_dispatch",
                    "head_branch": "main",
                    "status": "completed" if run == SOURCE_RUN and self.source_finished else "in_progress"}
        return super()._read(path, query)

    def _write(self, path: str, body: dict) -> dict:
        identifier = self.history_size + 1
        if path == "/deployments":
            expected = DeploymentAttempt(2, f"{SOURCE_RUN}:1", VERSION, self.graph.candidate, "ordinary",
                                         RELEASE_ID, 1, 1, self._baseline())
            if body["payload"] != asdict(expected) or identifier in self.deployments:
                raise AssertionError("Unexpected or repeated Deployment creation")
            self.posts.append(path)
            record = _deployment(identifier, self.receipt) | {
                "payload": body["payload"], "node_id": f"deployment-{identifier}",
                "created_at": self._time(identifier),
            }
            self.deployments[identifier] = record
            return record
        if path == f"/deployments/{identifier}/statuses":
            previous = self.statuses.setdefault(identifier, [])
            state = body["state"]
            if [item["state"] for item in previous] != ([] if state == "in_progress" else ["in_progress"]):
                raise AssertionError("Unexpected or repeated Deployment transition")
            self.posts.append(path)
            self.status_writes.append(state)
            status_id = 5000 + len(previous)
            record = body | {"id": status_id, "node_id": f"status-{status_id}", "creator": OWNER,
                             "created_at": self._time(identifier + len(previous))}
            previous.insert(0, record)
            if self.lose_success_response and state == "success":
                raise URLError("fixture lost reply after success POST")
            return record
        return super()._write(path, body)

    def transport(self, connection, request: Request, **kwargs) -> addinfourl:
        """Отдельно считает чтения реквизитов по фактическому пути REST."""
        parts = urlsplit(request.full_url)
        if request.get_method() == "GET" and parts.netloc == "api.github.com":
            path = unquote(parts.path).removeprefix(f"/repos/{REPOSITORY}")
            location = "runner" if request.get_header("Authorization") == f"Bearer {RUNNER_TOKEN}" else "vps"
            self.rest_reads[location, path] += 1
        return super().transport(connection, request, **kwargs)


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

    def _switch_patches(self):
        return (patch.object(self.host, "finalize_database"),)

    def _private_text(self, path: Path) -> str:
        return POLICY_SHA if path.name == "compatibility-policy.sha" else VPS_TOKEN

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
        self.calls.append(command[2] if command[1:2] == ["--attempt"] else
                          command[1] if len(command) > 1 else "install")
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for patcher in (*system.DeployProdTests()._patches(), *self._switch_patches()):
                stack.enter_context(patcher)
            host_type = deploy_prod.Host
            for patcher in (
                patch.object(host, "select", side_effect=lambda version: host_type.select(host, version)),
                patch.object(host, "predeploy", side_effect=lambda *args, **opts: host_type.predeploy(host, *args, **opts)),
                patch.object(host, "uses_selector_runtime", return_value=True),
                patch.object(host, "require_switch_rollout"), patch.object(host, "migrate_copy"),
                patch.object(host, "release_api", return_value=GitHub(REPOSITORY, VPS_TOKEN)),
                patch.object(compatibility, "private_text", side_effect=self._private_text),
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


class _V2ControllerFixture(_ControllerFixture):
    def __init__(self, root: Path, api: _V2RestFixture, *, restore: bool = False) -> None:
        super().__init__(root, api, installed=False)
        self.base_state = deploy_prod.prep.read_state(self.host.state_dir, self.host.checkout)
        self.restore = restore
        self.lose_completion_response = False
        self.switches = 0
        self.cleanup_count = 0

    def _private_text(self, path: Path) -> str:
        if path.name == deploy_attempt.JOURNAL:
            return path.read_text(encoding="utf-8")
        return super()._private_text(path)

    def _switch_patches(self):
        return (
            patch.object(self.host, "finalize_database", side_effect=self._finalize),
            patch.object(deploy_acceptance, "_verify_locked", side_effect=self._verify),
            patch.object(deploy_cleanup_old, "_snapshot"),
            patch.object(deploy_prod, "_recover", side_effect=self._recover),
        )

    def _transition(self, host, state: dict, save, write) -> None:
        self.switches += 1
        if self.restore:
            raise deploy_prod.DeployError("Фикстура остановила системное переключение")
        super()._transition(host, state, save, write)
        state.update(transition_id="4" * 32,
                     old={"generation_id": BASE_GENERATION, "host_path": f"/fixture/data/app-{BASE_GENERATION}.db",
                          "device": 1, "inode": 2},
                     new={"generation_id": GENERATION})

    def _finalize(self, state: dict, outcome: str) -> None:
        if outcome != "success" or not self.installed:
            raise AssertionError("Expected candidate runtime")
        host, candidate = self.host, state["candidate"]
        host.marker(False)
        deploy_prod._state(host, state, "success", last_good={
            "version": candidate["version"], "sha": candidate["sha"], "tag": candidate["tag"],
            "image_id": candidate["image_id"], "ci_run_id": candidate["run_id"],
            "ci_run_attempt": candidate["run_attempt"],
        })

    def _recover(self, host, state: dict) -> None:
        if state.get("stage") in ("success", None):
            return
        if not self.restore or state.get("stage") != "rollback_started":
            raise AssertionError(f"Unexpected recovery state: {state.get('stage')}")
        # Подтверждённый возврат системы оставляет прежние образ и поколение.
        host.restore_checkout(self.api.graph.base)
        host.current_image, host.running = system.OLD_IMAGE, True
        host.marker(False)
        state.clear()
        state.update(self.base_state)
        deploy_prod._state(host, state, "success")

    def _verify(self, host, version: str, sha: str) -> dict:
        if not self.installed and (version, sha) == (PREVIOUS_VERSION, self.api.graph.base):
            return asdict(InstallationReceipt(1, version, sha, "ordinary", HISTORY_SIZE, 2, 1,
                                              BASE_GENERATION, MIGRATION, "3" * 64))
        return super()._verify(host, version, sha)

    def _cleanup(self, host, version: str, sha: str, runtime: str) -> dict:
        super()._cleanup(host, version, sha, runtime)
        self.cleanup_count += 1
        state = deploy_prod.prep.read_state(host.state_dir, host.checkout)
        result = deploy_prod.database_switch.old_cleanup_record(state)
        deploy_prod._state(host, state, "success", old_generation_cleanup=result)
        return result

    def execute(self, arguments: tuple[str, ...], **kwargs) -> subprocess.CompletedProcess[str]:
        """Передаёт CLI и моделирует обрыв SSH после первой проверки установленного кандидата."""
        result = super().execute(arguments, **kwargs)
        if self.lose_completion_response and arguments[5] == "complete":
            timeout = subprocess.TimeoutExpired(arguments, 300, stderr=result.stderr)
            raise InstallationTagError("Фикстура потеряла ответ SSH") from timeout
        return result


def _measure(root: Path, mode: str, api: _RestFixture, runner: Path, *, installed: bool = False,
             sha: str | None = None, expected_error: str | None = None,
             controller: _ControllerFixture | None = None) -> dict:
    stdout, stderr = io.StringIO(), io.StringIO()
    notes = root / "notes.md"
    notes.write_text("Фикстура PATCH\n", encoding="utf-8")
    flags = ["--resume"] if installed else ["--release", "--change", "patch", "--notes-file", str(notes)]
    argv = ["release_production", VERSION, sha or api.graph.candidate, "--ssh-host", "vps.example.invalid",
            "--known-hosts", str(notes), *flags]
    v2 = isinstance(api, _V2RestFixture)
    environment = {"GITHUB_TOKEN": RUNNER_TOKEN, "COMPADRE_DEPLOYMENT_READER": "v2" if v2 else "v1",
                   "COMPADRE_DEPLOYMENT_WRITER": "v2" if v2 else "v1", "COMPADRE_CONTROLLER_SHA": SNAPSHOT,
                   "GITHUB_RUN_ID": str(RESUME_RUN if installed else SOURCE_RUN), "GITHUB_RUN_ATTEMPT": "1",
                   "RELEASE_SOURCE_ATTEMPT": f"{SOURCE_RUN}:1" if installed and v2 else ""}
    with (patch.object(system, "OLD_SHA", api.graph.base), patch.object(system, "NEW_SHA", api.graph.candidate),
          chdir(runner)):
        if controller is None:
            controller = _ControllerFixture(root / mode, api, installed)
        with (patch.object(release_production, "run_vps_command", side_effect=controller.execute),
              patch.object(AbstractHTTPHandler, "do_open", side_effect=api.transport),
              patch.object(sys, "argv", argv), patch.dict(os.environ, environment),
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
    if expected_error is not None:
        if exit_code != 1 or expected_error not in stderr.getvalue():
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
    result = {"scenario": mode, "exit_code": exit_code, "wall_ns": elapsed, "reports": reports,
              "posts": api.posts.copy(), "ssh": controller.calls.copy()}
    if v2:
        baseline = api._baseline().deployment_id
        result.update(history_size=api.history_size, first_v2=api.first_v2, status_writes=api.status_writes.copy(),
                      routes={location: {
                          "graphql": {key: count for (place, key), count in api.graphql_routes.items() if place == location},
                          "deployments": api.rest_reads[location, "/deployments"],
                          "direct_base": api.rest_reads[location, f"/deployments/{baseline}"],
                          "base_status": api.rest_reads[location, f"/deployments/{baseline}/statuses"],
                          "candidate_status": api.rest_reads[location, f"/deployments/{baseline + 1}/statuses"],
                          "attempt_workflow": sum(api.rest_reads[location, f"/actions/runs/{run}/attempts/1"]
                                                  for run in (SOURCE_RUN, RESUME_RUN)),
                      } for location in ("runner", "vps")})
    return result


def measure_patch() -> list[dict]:
    """Измеряет четыре сценария с одинаковым PATCH и историей выпусков."""
    with TemporaryDirectory(prefix="patch-http-baseline-") as directory:
        root = Path(directory)
        graph, runner, fingerprint = _fixture(root)

        first = _RestFixture(graph, fingerprint)
        published = _measure(root, "publish", first, runner)
        published["paired_prs"] = re.findall(r"^- release .+?/pull/(\d+), main .+?/pull/(\d+);",
                                             first.releases[VERSION]["body"], re.MULTILINE)
        published["publisher_gets"] = {
            "parents_per_commit": [first.publisher_gets[f"/git/commits/{sha}", ""] for sha, _ in graph.pairs],
            "migration_for_candidate": first.publisher_gets[f"/contents/{MIGRATIONS_PREFIX.rstrip('/')}",
                                                            graph.candidate],
            "workflow": first.publisher_gets[f"/actions/workflows/{RELEASE_WORKFLOW}", ""],
            "run_list": first.publisher_gets[f"/actions/workflows/{RELEASE_WORKFLOW}/runs", ""],
            "attempt_jobs": first.publisher_gets["/actions/runs/1/attempts/1/jobs", ""],
        }
        ready = _RestFixture(graph, fingerprint)
        ready.seed_release(first)
        installed = _measure(root, "install_ready", ready, runner)
        resumed = _RestFixture(graph, fingerprint)
        resumed.seed_release(first, installed=True)
        continued = _measure(root, "resume", resumed, runner, installed=True)
        refused = _measure(root, "wrong_sha", _RestFixture(graph, fingerprint), runner, sha=graph.main,
                           expected_error="Candidate не является текущим tip")
        return [published, installed, continued, refused]


def measure_patch_v2() -> list[dict]:
    """Измеряет штатный Deployment v2 и однократный переход поверх v1."""
    with TemporaryDirectory(prefix="patch-http-v2-") as directory:
        root = Path(directory)
        graph, runner, fingerprint = _fixture(root)

        def setup(case, history_size=HISTORY_SIZE, *, first_v2=False, restore=False):
            api = _V2RestFixture(graph, fingerprint, history_size, first_v2=first_v2)
            with patch.object(system, "OLD_SHA", graph.base):
                return api, _V2ControllerFixture(root / case, api, restore=restore)

        first, controller = setup("publish_v2")
        published = _measure(root, "publish_v2", first, runner, controller=controller)

        ready, controller = setup("install_ready_v2")
        ready.seed_release(first)
        installed = _measure(root, "install_ready_v2", ready, runner, controller=controller)

        interrupted_api, controller = setup("interrupted_v2")
        interrupted_api.seed_release(first)
        controller.lose_completion_response = True
        interrupted = _measure(root, "interrupted_v2", interrupted_api, runner, controller=controller,
                               expected_error="Фикстура потеряла ответ SSH")
        interrupted_api.source_finished = True
        for counter in (interrupted_api.calls, interrupted_api.posts, interrupted_api.graphql_routes,
                        interrupted_api.rest_reads, interrupted_api.status_writes, controller.calls):
            counter.clear()
        controller.lose_completion_response = False
        continued = _measure(root, "resume_v2", interrupted_api, runner, installed=True, controller=controller)
        if (controller.switches, controller.cleanup_count) != (1, 1):
            raise AssertionError("Resume must reuse the original switch and cleanup")

        restored_api, controller = setup("restored_v2", restore=True)
        restored_api.seed_release(first)
        restored = _measure(root, "restored_v2", restored_api, runner, controller=controller,
                            expected_error=f"возвратом на {PREVIOUS_VERSION}")

        lost_reply, controller = setup("lost_post_v2")
        lost_reply.seed_release(first)
        lost_reply.lose_success_response = True
        recovered = _measure(root, "lost_post_v2", lost_reply, runner, controller=controller)

        wrong, controller = setup("wrong_sha_v2")
        refused = _measure(root, "wrong_sha_v2", wrong, runner, controller=controller, sha=graph.main,
                           expected_error="Candidate не является текущим tip")

        long, controller = setup("publish_v2_1000", 1000)
        published_1000 = _measure(root, "publish_v2_1000", long, runner, controller=controller)

        first_v2, controller = setup("publish_first_v2", first_v2=True)
        transition = _measure(root, "publish_first_v2", first_v2, runner, controller=controller)
        return [published, published_1000, transition, installed, interrupted, continued, restored, recovered, refused]


def _summary(scenario: dict) -> dict:
    fields = ("get", "graphql_query", "post", "pages", "errors", "http_ns", "base_get", "base_graphql", "storage")
    totals = {location: dict.fromkeys(fields, 0) for location in ("runner", "vps")}
    for report in scenario["reports"]:
        for row in report["http"]:
            total = totals[row["location"]]
            if row["target"] == "artifact_storage":
                total["storage"] += row["attempts"]
            elif row["target"] == "github_graphql_query":
                total["graphql_query"] += row["attempts"]
                if row["phase"] == "deployment_base":
                    total["base_graphql"] += row["attempts"]
            else:
                total[row["method"].lower()] += row["attempts"]
            total["pages"] += row["pages"]
            total["errors"] += row["errors"]
            total["http_ns"] += row["elapsed_ns"]
            if row["phase"] == "deployment_base" and row["method"] == "GET":
                total["base_get"] += row["attempts"]
    metadata = {key: scenario[key] for key in ("history_size", "first_v2") if key in scenario}
    return {"scenario": scenario["scenario"], "wall_ns": scenario["wall_ns"], **metadata, **totals}


class PatchHttpBaselineTests(unittest.TestCase):
    """Проверяет маршруты и соответствие метрик реальным вызовам транспорта."""

    def test_patch_routes(self) -> None:
        """Определяет зафиксированные операции публикации, установки, resume и отказа."""
        publish, ready, resume, refused = measure_patch()
        status = f"/deployments/{HISTORY_SIZE + 1}/statuses"
        self.assertEqual(publish["posts"], ["/git/tags", "/git/refs", "/releases", "/deployments", status])
        self.assertEqual(ready["posts"], ["/deployments", status])
        self.assertEqual(resume["posts"], [status])
        self.assertEqual((refused["posts"], refused["ssh"]), ([], []))
        self.assertEqual([publish["ssh"][0], ready["ssh"][0], resume["ssh"][0]],
                         ["install", "install", "--verify-installed"])
        self.assertEqual(publish["paired_prs"], [("1", "11"), ("2", "12")])
        self.assertEqual(publish["publisher_gets"], {"parents_per_commit": [1, 1], "migration_for_candidate": 1,
                                                   "workflow": 3, "run_list": 3, "attempt_jobs": 3})

    def test_v2_http_cost_and_transitions(self) -> None:
        """Сравнивает расход при 16 и 1000 Deployment и проверяет все записанные переходы."""
        short, long, first, ready, interrupted, resume, restored, lost_reply, refused = measure_patch_v2()
        for location in ("runner", "vps"):
            self.assertEqual(short["routes"][location], long["routes"][location])
            short_total, long_total = _summary(short)[location], _summary(long)[location]
            self.assertEqual({key: value for key, value in short_total.items() if key != "http_ns"},
                             {key: value for key, value in long_total.items() if key != "http_ns"})
            self.assertGreater(_summary(first)[location]["base_graphql"], short_total["base_graphql"])
        for result in (short, long, first, ready, lost_reply):
            self.assertEqual(result["status_writes"], ["in_progress", "success"])
            self.assertEqual(result["posts"].count("/deployments"), 1)
        self.assertEqual(interrupted["status_writes"], ["in_progress"])
        self.assertEqual(resume["status_writes"], ["success"])
        self.assertNotIn("/deployments", resume["posts"])
        self.assertNotIn("install", resume["ssh"])
        self.assertEqual(restored["status_writes"], ["in_progress", "failure"])
        self.assertEqual(_summary(lost_reply)["runner"]["errors"], 1)
        self.assertEqual((refused["posts"], refused["ssh"]), ([], []))

    def test_patch_refusals_close_before_writes_or_ssh(self) -> None:
        """Сдвиг ветки, неполная история, неверная связь и чужой CI закрывают допуск."""
        root = Path(self.enterContext(TemporaryDirectory(prefix="patch-http-refusals-")))
        graph, runner, fingerprint = _fixture(root)

        def assert_refusal(case: str, api: _RestFixture, message: str) -> None:
            scenario = _measure(root, case, api, runner, expected_error=message)
            self.assertEqual((scenario["posts"], scenario["ssh"]), ([], []))

        moving_branch = _RestFixture(graph, fingerprint)
        read_moving_branch = moving_branch._read

        def move_tip_during_history(path: str, query: dict[str, str]) -> object:
            if path == "/pulls/1":
                moving_branch.release_tip = graph.pairs[0][0]
            return read_moving_branch(path, query)

        with patch.object(moving_branch, "_read", side_effect=move_tip_during_history):
            assert_refusal("moved_tip", moving_branch, "Релизная ветка сдвинулась")

        missing_history = _RestFixture(graph, fingerprint)
        read_missing_history = missing_history._read

        def omit_commit_parents(path: str, query: dict[str, str]) -> object:
            if path == f"/git/commits/{graph.pairs[0][0]}":
                return {"sha": graph.pairs[0][0]}
            return read_missing_history(path, query)

        with patch.object(missing_history, "_read", side_effect=omit_commit_parents):
            assert_refusal("incomplete_history", missing_history, "Не подтверждены родители")

        wrong_link = _RestFixture(graph, fingerprint)
        wrong_link.pulls[12]["body"] = f"Hotfix-Links:\nhttps://github.com/{REPOSITORY}/pull/1\n"
        assert_refusal("wrong_pair", wrong_link, "однозначную группу")

        wrong_ci = _RestFixture(graph, fingerprint)
        wrong_ci.run["head_sha"] = graph.main
        assert_refusal("wrong_ci", wrong_ci, "Нет успешного полного CI")

        failed_ci = _RestFixture(graph, fingerprint)
        failed_ci.jobs[-1]["conclusion"] = "failure"
        assert_refusal("failed_ci", failed_ci, "Нет успешного полного CI")

    def test_manual_publisher_requires_clean_candidate_checkout(self) -> None:
        """Ручной CLI отказывает на main и грязном checkout, затем публикует с точного SHA."""
        root = Path(self.enterContext(TemporaryDirectory(prefix="patch-http-manual-")))
        graph, runner, fingerprint = _fixture(root)
        api = _RestFixture(graph, fingerprint)
        notes = root / "notes.md"
        notes.write_text("Ручной PATCH\n", encoding="utf-8")
        argv = ["publish_release", VERSION, graph.candidate, "--change", "patch", "--notes-file", str(notes), "--publish"]

        def run(checkout: Path, expected_error: str | None = None) -> str:
            stdout, stderr = io.StringIO(), io.StringIO()
            with (chdir(checkout), patch.object(AbstractHTTPHandler, "do_open", side_effect=api.transport),
                  patch.object(sys, "argv", argv), patch.dict(os.environ, {"GITHUB_TOKEN": RUNNER_TOKEN,
                                                                          "COMPADRE_DEPLOYMENT_READER": "v1"}),
                  redirect_stdout(stdout), redirect_stderr(stderr)):
                if expected_error is None:
                    publish_release.main()
                else:
                    with self.assertRaises(SystemExit) as error:
                        publish_release.main()
                    self.assertEqual(error.exception.code, 1)
                    self.assertIn(expected_error, stderr.getvalue())
            return stdout.getvalue()

        run(runner, "HEAD не соответствует candidate SHA")
        with TemporaryDirectory(dir=graph.checkout) as dirty:
            (Path(dirty) / "untracked.txt").touch()
            run(graph.checkout, "Для публикации нужен чистый checkout")
        self.assertEqual(api.posts, [])
        self.assertIn("Опубликовано:", run(graph.checkout))
        self.assertEqual(api.posts, ["/git/tags", "/git/refs", "/releases"])


if __name__ == "__main__":
    scenarios = measure_patch_v2() if sys.argv[1:] == ["--v2"] else measure_patch()
    print(json.dumps({"summary": [_summary(scenario) for scenario in scenarios], "scenarios": scenarios}, indent=2))
