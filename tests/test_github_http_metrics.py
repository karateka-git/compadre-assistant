"""Проверяет учёт фактических HTTP-переходов без доступа к GitHub."""

from __future__ import annotations

from contextlib import redirect_stderr
from email.message import Message
import io
from itertools import count
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import AbstractHTTPHandler, Request
from urllib.response import addinfourl

from scripts.deploy_metrics import HttpOperation, github_http_report, relay_vps_http_report
from scripts.full_ci_controller import (
    ControllerError, GitHub, HttpLocation, HttpPhase, collect_http_metrics, http_phase, immutable_git_reads,
)
from scripts.github_publishing import PublishingGitHub
from scripts.predeploy_compatibility import EvidenceAPI, PredeployError


def _response(request: Request, body: bytes, *, code: int = 200, location: str | None = None) -> addinfourl:
    headers = Message()
    if location is not None:
        headers["Location"] = location
    response = addinfourl(io.BytesIO(body), headers, request.full_url, code)
    response.msg = "fixture"
    return response


class GitHubHttpMetricsTests(unittest.TestCase):
    """Сравнивает отчёт с запросами, дошедшими до сетевого handler urllib."""

    def setUp(self) -> None:
        self.api = GitHub("owner/repo", "TOKEN_SENTINEL")
        tick = count(0, 5)
        self.enterContext(patch("scripts.full_ci_controller.perf_counter_ns", side_effect=lambda: next(tick)))

    def test_counts_pages_and_writes_with_independent_scopes(self) -> None:
        def route(connection, request, **kwargs):
            if request.get_method() == "POST":
                self.assertIn(b"BODY_SENTINEL", request.data)
                return _response(request, b'{"id": 17}', code=201)
            if request.full_url.endswith("/user"):
                return _response(request, b'{"login": "fixture"}')
            self.assertIn("QUERY_SENTINEL", request.full_url)
            items = list(range(100)) if "page=1&" in request.full_url else [100]
            return _response(request, json.dumps({"items": items}).encode())

        publisher = PublishingGitHub(self.api.repository, self.api.token)
        with patch.object(AbstractHTTPHandler, "do_open", side_effect=route) as outgoing:
            with collect_http_metrics(HttpLocation.RUNNER) as runner:
                with http_phase(HttpPhase.RELEASE_SELECT):
                    self.assertEqual(self.api.pages("/items", key="items", search="QUERY_SENTINEL"), list(range(101)))
                    with collect_http_metrics(HttpLocation.VPS) as vps:
                        self.assertEqual(publisher.authenticated_user(), {"login": "fixture"})
                with http_phase(HttpPhase.DEPLOYMENT_PUBLISH):
                    self.assertEqual(publisher.post("/deployments", {"notes": "BODY_SENTINEL"}), {"id": 17})

        self.assertEqual(outgoing.call_count, 4)
        self.assertEqual(runner.rows(), [
            {"location": "runner", "phase": "deployment_publish", "target": "github_api", "method": "POST",
             "attempts": 1, "pages": 0, "errors": 0, "elapsed_ns": 10},
            {"location": "runner", "phase": "release_select", "target": "github_api", "method": "GET",
             "attempts": 2, "pages": 2, "errors": 0, "elapsed_ns": 20},
        ])
        self.assertEqual(vps.rows(), [
            {"location": "vps", "phase": "other", "target": "github_api", "method": "GET",
             "attempts": 1, "pages": 0, "errors": 0, "elapsed_ns": 10},
        ])
        self.assertNotIn("SENTINEL", json.dumps(runner.rows() + vps.rows()))

    def test_counts_each_failed_http_attempt_once(self) -> None:
        class Unreadable(io.BytesIO):
            def read(self, *args, **kwargs):
                raise OSError("fixture interrupted body")

        def route(connection, request, **kwargs):
            if request.full_url.endswith("/missing"):
                return _response(request, b'{"message": "missing"}', code=404)
            if request.full_url.endswith("/offline"):
                raise URLError("fixture offline")
            response = addinfourl(Unreadable(), Message(), request.full_url, 200)
            response.msg = "fixture"
            return response

        with patch.object(AbstractHTTPHandler, "do_open", side_effect=route):
            with collect_http_metrics(HttpLocation.RUNNER) as metrics:
                for path, cause in (("/missing", HTTPError), ("/offline", URLError), ("/body", OSError)):
                    with self.subTest(path=path), self.assertRaises(ControllerError) as error:
                        self.api.get(path)
                    self.assertIsInstance(error.exception.__cause__, cause)
                    if isinstance(error.exception.__cause__, HTTPError):
                        error.exception.__cause__.close()

        self.assertEqual(metrics.rows(), [
            {"location": "runner", "phase": "other", "target": "github_api", "method": "GET",
             "attempts": 3, "pages": 0, "errors": 3, "elapsed_ns": 20},
        ])

    def test_relays_read_only_graphql_separately_from_rest_writes(self) -> None:
        """Сохраняет POST чтения GraphQL при передаче агрегата контроллера."""
        def route(connection, request, **kwargs):
            self.assertEqual(request.get_method(), "POST")
            if request.full_url == "https://api.github.com/graphql":
                return _response(request, b'{"data":{"node":{"id":"D_fixture"}}}')
            return _response(request, b'{"id":17}', code=201)

        publisher = PublishingGitHub(self.api.repository, self.api.token)
        controller, runner = io.StringIO(), io.StringIO()
        with patch.object(AbstractHTTPHandler, "do_open", side_effect=route):
            with redirect_stderr(controller), github_http_report(HttpLocation.VPS, HttpOperation.ATTEMPT):
                self.assertEqual(self.api.graphql("query { node { id } }"), {"node": {"id": "D_fixture"}})
                self.assertEqual(publisher.post("/deployments", {}), {"id": 17})
        with redirect_stderr(runner):
            relay_vps_http_report(controller.getvalue(), HttpOperation.ATTEMPT)
        report = json.loads(runner.getvalue().removeprefix("deploy-http-metrics "))
        self.assertEqual([(row["target"], row["method"], row["attempts"]) for row in report["http"]],
                         [("github_api", "POST", 1), ("github_graphql_query", "POST", 1)])

    def test_immutable_git_cache_keeps_refs_and_other_tokens_fresh(self) -> None:
        """Проверяет область кэша SHA, свежие refs и независимый контекст контроллера."""
        reads = count(1)

        def route(connection, request, **kwargs):
            return _response(request, json.dumps({"read": next(reads)}).encode())

        sha = "a" * 40
        tag = f"/git/tags/{sha}"
        ref = "/git/ref/tags/v1.0.0"
        contents = "/contents/test.txt"
        controller = GitHub(self.api.repository, "FIXTURE_VPS")
        with patch.object(AbstractHTTPHandler, "do_open", side_effect=route), immutable_git_reads():
            tag_object = self.api.get(tag)
            tag_object["read"] = 99
            self.assertEqual(self.api.get(tag), {"read": 1})
            self.assertEqual(self.api.get(contents, ref=sha), {"read": 2})
            self.assertEqual(self.api.get(contents, ref=sha), {"read": 2})
            self.assertNotEqual(self.api.get(ref), self.api.get(ref))
            self.assertNotEqual(self.api.get(contents, ref="main"), self.api.get(contents, ref="main"))
            self.assertEqual(controller.get(tag), {"read": 7})
            with immutable_git_reads():
                self.assertEqual(self.api.get(tag), {"read": 8})
            self.assertEqual(self.api.get(tag), {"read": 1})

    def test_measures_artifact_redirect_and_storage_without_sending_authorization(self) -> None:
        signed = "https://artifact-storage.example/archive?sig=SIGNED_SENTINEL"

        def route(connection, request, **kwargs):
            if request.full_url == signed:
                self.assertNotIn("Authorization", dict(request.header_items()))
                return _response(request, b"ZIP_SENTINEL")
            self.assertEqual(request.get_header("Authorization"), "Bearer TOKEN_SENTINEL")
            self.assertTrue(request.full_url.endswith("/actions/artifacts/17/zip"))
            return _response(request, b"", code=302, location=signed)

        evidence = EvidenceAPI(self.api.repository, self.api.token)
        with patch.object(AbstractHTTPHandler, "do_open", side_effect=route) as outgoing:
            with collect_http_metrics(HttpLocation.VPS) as metrics, http_phase(HttpPhase.VPS_PREPARE):
                self.assertEqual(evidence.download(17), b"ZIP_SENTINEL")

        self.assertEqual(outgoing.call_count, 2)
        self.assertEqual(metrics.rows(), [
            {"location": "vps", "phase": "vps_prepare", "target": "artifact_storage", "method": "GET",
             "attempts": 1, "pages": 0, "errors": 0, "elapsed_ns": 10},
            {"location": "vps", "phase": "vps_prepare", "target": "github_api", "method": "GET",
             "attempts": 1, "pages": 0, "errors": 0, "elapsed_ns": 10},
        ])
        self.assertNotIn("SENTINEL", json.dumps(metrics.rows()))

    def test_unsupported_redirect_records_error_without_storage_attempt(self) -> None:
        def route(connection, request, **kwargs):
            return _response(request, b"", code=302, location="http://artifact-storage.example/archive")

        evidence = EvidenceAPI(self.api.repository, self.api.token)
        with patch.object(AbstractHTTPHandler, "do_open", side_effect=route) as outgoing:
            with collect_http_metrics(HttpLocation.VPS) as metrics:
                with self.assertRaisesRegex(PredeployError, "SAFE_ARTIFACT_REDIRECT"):
                    evidence.download(17)

        self.assertEqual(outgoing.call_count, 1)
        self.assertEqual(metrics.rows(), [
            {"location": "vps", "phase": "other", "target": "github_api", "method": "GET",
             "attempts": 1, "pages": 0, "errors": 1, "elapsed_ns": 5},
        ])
