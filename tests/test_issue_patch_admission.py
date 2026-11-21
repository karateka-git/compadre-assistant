"""Проверяет выпуск допуска на исходной транспортной фикстуре PATCH."""

from collections import Counter
from contextlib import chdir
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import AbstractHTTPHandler

from scripts.ci_repository_compatibility import _confirmed_baseline
from scripts.full_ci_controller import HttpLocation, collect_http_metrics, decide, immutable_git_reads
from scripts.issue_patch_admission import prepare_admission
from scripts.predeploy_compatibility import EvidenceAPI
from tests.test_patch_http_baseline import (BRANCH, HISTORY_SIZE, MIGRATION, POLICY_SHA, REPOSITORY,
                                           RUNNER_TOKEN, _fixture, _V2RestFixture)


def completed_event(api: _V2RestFixture) -> dict:
    """Связывает исходный run с успешными шагами и digest артефакта совместимости."""
    job = next(job for job in api.jobs if job["name"].endswith(" / check-full-suite"))
    compat_job = next(job for job in api.jobs if job["name"].endswith("repository-compatibility-check"))
    compat_job["steps"].append({"name": "Build isolated compatibility worker",
                                "status": "completed", "conclusion": "success"})
    digest = api.artifact.artifact["digest"].removeprefix("sha256:")
    job["steps"].append({"name": f"Compatibility artifact 22 {digest}",
                         "status": "completed", "conclusion": "success"})
    return {"action": "completed", "repository": {"full_name": REPOSITORY}, "workflow_run": dict(api.run)}


class IssuePatchAdmissionTests(unittest.TestCase):
    """Сопоставляет допуск двух merge с независимым счётчиком HTTP."""

    def setUp(self) -> None:
        """Создаёт отдельные коммиты двух связанных PR и завершённый полный CI."""
        root = Path(self.enterContext(TemporaryDirectory()))
        graph, _, fingerprint = _fixture(root)
        self.assertEqual(len(graph.pairs), 2)
        self.api = _V2RestFixture(graph, fingerprint, HISTORY_SIZE)
        self.job = next(job for job in self.api.jobs if job["name"].endswith(" / check-full-suite"))
        self.compat_job = next(job for job in self.api.jobs if job["name"].endswith("repository-compatibility-check"))
        self.event = completed_event(self.api)
        self.enterContext(patch.object(AbstractHTTPHandler, "do_open", side_effect=self.api.transport))

    def _prepare(self):
        with immutable_git_reads():
            return prepare_admission(EvidenceAPI(REPOSITORY, RUNNER_TOKEN), self.event,
                                     self.api.graph.checkout, POLICY_SHA)

    def test_success_and_transport(self) -> None:
        """Проверяет предыдущий тег, точный диапазон и все фактические запросы."""
        with collect_http_metrics(HttpLocation.CI) as metrics:
            proof = self._prepare()
        self.assertEqual((proof.version, proof.branch, proof.previous_tag, proof.previous_tag_sha),
                         ("v4.2.1", BRANCH, "v4.2.0", self.api.graph.base))
        self.assertEqual((proof.migration, proof.candidate_sha, proof.baseline),
                         (MIGRATION, self.api.graph.candidate, self.api._baseline()))
        self.assertFalse(any("/pulls" in path for _, path in self.api.rest_reads))
        measured = Counter()
        for row in metrics.rows():
            measured[row["target"], row["method"]] += row["attempts"]
        self.assertEqual(measured, {(target, method): count for (_, target, method), count in self.api.calls.items()})
        self.assertEqual(measured["artifact_storage", "GET"], 1)

    def test_prior_ci_transport(self) -> None:
        """Измеряет прежние чтения контроллера и harness до отдельной выдачи допуска."""
        graph = self.api.graph
        with (chdir(graph.checkout), patch.dict(self.api.run, status="in_progress", conclusion=None),
              patch.dict(os.environ, GITHUB_EVENT_NAME="push", GITHUB_RUN_ID="1",
                         COMPATIBILITY_POLICY_SHA=POLICY_SHA, GITHUB_TOKEN=RUNNER_TOKEN),
              collect_http_metrics(HttpLocation.CI) as metrics):
            outputs = decide(EvidenceAPI(REPOSITORY, RUNNER_TOKEN), "release", graph.candidate, BRANCH, False)
            self.assertEqual(_confirmed_baseline(REPOSITORY, graph.base, outputs["accepted-baseline"])["sha"], graph.base)
        measured = sum(row["attempts"] for row in metrics.rows())
        self.assertEqual(measured, sum(self.api.calls.values()))

    def test_wrong_receipt_and_failed_suite(self) -> None:
        """Отказывает при загрузке другого артефакта и неуспешном исходном run."""
        self.job["steps"][-1]["name"] = "Compatibility artifact 22 " + "0" * 64
        with self.assertRaisesRegex(ValueError, "Артефакт"):
            self._prepare()
        self.event["workflow_run"]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "Событие"):
            self._prepare()

    def test_wrong_branch_boundary(self) -> None:
        """Тег боковой ветки не может служить началом диапазона PATCH."""
        self.api.refs["v4.2.0"]["object"]["sha"] = self.api.graph.main
        with self.assertRaises((ValueError, RuntimeError)):
            self._prepare()

    def test_unisolated_producer_and_wrong_attempt(self) -> None:
        """Только подтверждённый GitHub запуск с изолированным producer выдаёт допуск."""
        self.compat_job["steps"].pop()
        with self.assertRaisesRegex(ValueError, "изолированный"):
            self._prepare()
        self.event["workflow_run"]["head_sha"] = "1" * 40
        with self.assertRaisesRegex(ValueError, "попытку"):
            self._prepare()
