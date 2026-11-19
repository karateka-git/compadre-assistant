"""Проверяет выбор базы v1/v2 через штатные публичные входы."""

from dataclasses import asdict
import os
import unittest
from unittest.mock import patch

from scripts.ci_repository_compatibility import DeploymentReleaseTagEvidence
from scripts.deployment_acceptance import (
    AcceptedBaseline, DeploymentAcceptanceError, require_accepted_deployment,
    resolve_accepted_baseline, summarize_deployment, verify_accepted_baseline,
)
from tests.test_deployment_acceptance import FakeGitHub, RECEIPT, SHA, VERSION


BASE = AcceptedBaseline(SHA, VERSION, 41, 7, 8)


class AttemptGitHub(FakeGitHub):
    """Моделирует повтор установки того же SHA и контракт двух API GitHub."""

    def __init__(self):
        """Готовит принятую v1 и новую попытку v2 с равным SHA."""
        super().__init__()
        old = self.deployments[0]
        old.update(node_id="D7", created_at="2026-01-01T00:00:00Z")
        self.statuses[0]["node_id"] = "S8"
        payload = {key: value for key, value in old["payload"].items() if key not in ("generation_id", "migration")}
        payload.update(schema_version=2, attempt_key="12001:1", baseline=asdict(BASE))
        self.latest = old | {"id": 11, "node_id": "D11", "created_at": "2026-01-03T00:00:00Z", "payload": payload}
        self.deployments.append(self.latest)
        self.status = self.statuses[0] | {"id": 12, "node_id": "S12",
                                          "created_at": "2026-01-03T00:10:00Z",
                                          "description": f"v1 gen={RECEIPT.generation_id} migration={RECEIPT.migration}"}
        self.annotation = DeploymentReleaseTagEvidence(BASE.sha, "6" * 40, 107, 2, "7" * 64, BASE)
        self.count = 16

    def graphql(self, query, **variables):
        """Отдаёт отсортированную пару независимо от порядка REST."""
        self.calls.append(("graphql", variables))
        def node(raw):
            status = self.status if raw is self.latest else self.statuses[0]
            return {"id": raw["node_id"], "createdAt": raw["created_at"], "latestStatus": {"id": status["node_id"]}}
        if "id" in variables:
            return {"node": next(node(row) for row in self.deployments if row["node_id"] == variables["id"])}
        connection = {"nodes": [node(row) for row in reversed(self.deployments)], "totalCount": self.count}
        return {"repository": {"nameWithOwner": self.repository, "deployments": connection,
                               "object": {"oid": SHA, "deployments": connection}}}

    def get(self, path, **parameters):
        """Возвращает ограниченные страницы и delegate для Release и Git."""
        if (parameters.get("per_page") == 10 or path in ("/deployments/7", "/deployments/11")):
            self.calls.append((path, parameters))
            return {"/deployments": self.deployments, "/deployments/7": self.deployments[0],
                    "/deployments/11": self.latest, "/deployments/7/statuses": self.statuses,
                    "/deployments/11/statuses": [self.status]}[path]
        return super().get(path, **parameters)


class AttemptReaderTests(unittest.TestCase):
    """Проверяет режим перехода, точную ссылку возврата и сводку приёмки."""

    def setUp(self):
        """Включает v2 для одного читателя на время теста."""
        self.enterContext(patch.dict(os.environ, COMPADRE_DEPLOYMENT_READER="v2"))
        self.api = AttemptGitHub()

    def test_success_and_exact_reads_have_fixed_cost(self):
        """Длина истории не меняет запросы выбора, frozen проверяет ID статуса."""
        expected = AcceptedBaseline(SHA, VERSION, 41, 11, 12)
        for size in (16, 1000):
            self.api.calls.clear()
            self.api.count = size
            self.assertEqual(resolve_accepted_baseline(self.api), expected)
            self.assertEqual(len(self.api.calls), 7)
        self.assertEqual(verify_accepted_baseline(self.api, 11, 12), expected)
        with self.assertRaises(DeploymentAcceptanceError):
            verify_accepted_baseline(self.api, 11, 8)

    def test_same_sha_summary_and_receipt(self):
        """Сводка и свежая приёмка относятся к новой попытке конкретного SHA."""
        summary = summarize_deployment(self.api, VERSION, SHA, 41, 107, 2, RECEIPT.migration)
        self.assertEqual((summary["deployment_id"], summary["generation_id"]), (11, RECEIPT.generation_id))
        self.assertEqual(require_accepted_deployment(self.api, RECEIPT).status_id, 12)
        self.api.status["state"] = "pending"
        summary = summarize_deployment(self.api, VERSION, SHA, 41, 107, 2, RECEIPT.migration)
        self.assertEqual(summary["deployment_state"], "pending")

    def test_default_mode_keeps_v1_transport(self):
        """Старый токен обходится REST, появление v2 закрывает старый допуск."""
        with patch.dict(os.environ, COMPADRE_DEPLOYMENT_READER="v1"):
            self.assertEqual(resolve_accepted_baseline(FakeGitHub()), BASE)
            with self.assertRaises(DeploymentAcceptanceError):
                resolve_accepted_baseline(self.api)
        self.api.deployments.pop()
        self.api.count = 1
        self.assertEqual(resolve_accepted_baseline(self.api), BASE)

    def test_exact_restoration_only(self):
        """Только прямой возврат с точным доверенным success открывает старую базу."""
        self.api.status.update(state="failure", description="v1 restored=7/8")
        self.assertEqual(resolve_accepted_baseline(self.api), BASE)
        for change in ({"state": "failure"}, {"state": "error"}, {"id": 99}, {"creator": {}}):
            with self.subTest(change=change), patch.dict(self.api.statuses[0], change), self.assertRaises(DeploymentAcceptanceError):
                resolve_accepted_baseline(self.api)
        with patch.dict(self.api.latest["payload"]["baseline"], deployment_id=11), self.assertRaises(DeploymentAcceptanceError):
            resolve_accepted_baseline(self.api)
        with patch.dict(self.api.status, description="loss"), self.assertRaises(DeploymentAcceptanceError):
            resolve_accepted_baseline(self.api)
        with patch.dict(self.api.latest["payload"], baseline=asdict(BASE) | {"status_id": 9}), self.assertRaises(DeploymentAcceptanceError):
            resolve_accepted_baseline(self.api)

    def test_fail_closed_on_malformed_attempt_status_or_provenance(self):
        """Проверяет версию, обязательные поля, формат и длину описания, миграцию и тег."""
        cases = ((self.api.latest["payload"], {"schema_version": True}),
                 (self.api.latest["payload"], {"extra": 1}), (self.api.latest["payload"], {"attempt_key": "a"}),
                 (self.api.latest["payload"], {"run_attempt": True}), (self.api.latest["payload"], {"release_id": 42}),
                 (self.api.latest["payload"], {"baseline": asdict(BASE) | {"sha": "b" * 40}}),
                 (self.api.status, {"description": self.api.status["description"] + " "}),
                 (self.api.status, {"description": "x" * 141}),
                 (self.api.status, {"description": self.api.status["description"].replace("0002", "0003")}))
        for target, change in cases:
            with self.subTest(change=change), patch.dict(target, change), self.assertRaises(DeploymentAcceptanceError):
                resolve_accepted_baseline(self.api)
        for state in ("in_progress", "pending", "queued", "inactive", "future", "error"):
            with self.subTest(state=state), patch.dict(self.api.status, state=state), self.assertRaises(DeploymentAcceptanceError):
                resolve_accepted_baseline(self.api)
