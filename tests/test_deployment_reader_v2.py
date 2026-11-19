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
            statuses = self.statuses_by_deployment.get(raw["id"], self.statuses)
            status = self.status if raw is self.latest else statuses[0]
            return {"id": raw["node_id"], "createdAt": raw["created_at"], "latestStatus": {"id": status["node_id"]}}
        if "id" in variables:
            return {"node": next(node(row) for row in self.deployments if row["node_id"] == variables["id"])}
        if "ids" in variables:
            return {"nodes": [node(row) for row in self.deployments if row["node_id"] in variables["ids"]]}
        connection = {"nodes": [node(row) for row in self.deployments[-2:][::-1]], "totalCount": self.count}
        return {"repository": {"nameWithOwner": self.repository, "deployments": connection,
                               "object": {"oid": SHA, "deployments": connection}}}

    def get(self, path, **parameters):
        """Возвращает ограниченные страницы и delegate для Release и Git."""
        if (parameters.get("per_page") == 10 or path in ("/deployments/7", "/deployments/11")):
            self.calls.append((path, parameters))
            if path.startswith("/deployments/") and path.endswith("/statuses"):
                key = int(path.split("/")[2])
                return [self.status] if key == 11 else self.statuses_by_deployment.get(key, self.statuses)
            return {"/deployments": self.deployments, "/deployments/7": self.deployments[0],
                    "/deployments/11": self.latest}[path]
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

    def test_default_mode_reads_v2_and_v1_can_be_selected_explicitly(self):
        """Чистое окружение включает v2, а принудительный v1 требует только REST."""
        with patch.dict(os.environ, COMPADRE_DEPLOYMENT_READER="v1"):
            self.assertEqual(resolve_accepted_baseline(FakeGitHub()), BASE)
            with self.assertRaises(DeploymentAcceptanceError):
                resolve_accepted_baseline(self.api)
        del os.environ["COMPADRE_DEPLOYMENT_READER"]
        self.assertEqual(resolve_accepted_baseline(self.api), AcceptedBaseline(SHA, VERSION, 41, 11, 12))

    def test_history_batches_global_ids_and_reuses_latest_observation(self):
        """Подтверждает полный список v1 и отклоняет неполные пакеты и конфликт REST."""
        newest = self.api.deployments[0]
        for identifier in (1, 2, 3):
            sha = str(identifier + 3) * 40
            current = newest | {"id": identifier, "node_id": f"D{identifier}", "sha": sha, "ref": sha,
                                "created_at": f"2025-12-0{identifier}T00:00:00Z",
                                "payload": newest["payload"] | {"sha": sha}}
            self.api.deployments.insert(0, current)
            self.api.statuses_by_deployment[identifier] = [
                self.api.statuses[0] | {"id": identifier, "node_id": f"S{identifier}", "state": "failure"}]
        self.api.deployments.pop()
        self.api.count = len(self.api.deployments)

        with patch("scripts.deployment_acceptance._HISTORY_BATCH_SIZE", 2):
            self.assertEqual(resolve_accepted_baseline(self.api), BASE)
        self.assertEqual([len(params["ids"]) for path, params in self.api.calls if "ids" in params], [2, 1])
        self.assertEqual(sum(path == "/deployments/7/statuses" for path, params in self.api.calls), 1)

        original = self.api.graphql
        for response in ({"nodes": []}, {"nodes": [{"id": "D999"}] * 3}):
            with self.subTest(response=response), patch.object(
                self.api, "graphql", side_effect=lambda query, **params: response if "ids" in params else original(query, **params)
            ), self.assertRaisesRegex(DeploymentAcceptanceError, "исторические записи"):
                resolve_accepted_baseline(self.api)
        with patch.dict(self.api.statuses_by_deployment[1][0], creator={}), self.assertRaises(DeploymentAcceptanceError):
            resolve_accepted_baseline(self.api)
        get = self.api.get
        def duplicate_global_id(path, **params):
            result = get(path, **params)
            if path == "/deployments" and params.get("per_page") == 100:
                result[0] = result[0] | {"node_id": newest["node_id"]}
            return result
        with patch.object(self.api, "get", side_effect=duplicate_global_id), self.assertRaisesRegex(
                DeploymentAcceptanceError, "дубликат глобального ID"):
            resolve_accepted_baseline(self.api)

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

    def test_main_release_and_pinned_harness_follow_same_transition(self):
        """Оба caller и независимый harness закрывают допуск, пока D18 открыт."""
        import json
        from scripts import full_ci_controller as ci
        from scripts import ci_repository_compatibility as harness
        from scripts.full_ci_controller import ControllerError

        api = self.api
        real_get = api.get
        def get(path, **parameters):
            if path.startswith("/git/ref/heads/"):
                return {"object": {"sha": SHA}}
            return real_get(path, **parameters)
        pin = "9" * 40
        self.enterContext(patch.object(api, "get", side_effect=get))
        self.enterContext(patch.object(ci, "git", return_value=SHA))
        self.enterContext(patch.object(ci, "GitHub", return_value=api))
        self.enterContext(patch.dict(os.environ, GITHUB_EVENT_NAME="workflow_dispatch", COMPATIBILITY_POLICY_SHA=pin,
                                     GITHUB_TOKEN="fixture"))
        for mode, branch in (("main", "main"), ("release", "release/3.4")):
            def outputs(expected_pin=pin):
                return ci.decide(api, mode, SHA, branch, False, expected_sha=SHA, expected_policy_sha=expected_pin)
            with self.subTest(mode=mode):
                with self.assertRaises(ControllerError):
                    outputs("8" * 40)
                with patch.dict(api.status, state="in_progress"):
                    with self.assertRaises(ControllerError):
                        outputs()
                current = outputs()
                self.assertEqual(current["policy-sha"], pin)
                self.assertEqual(harness._confirmed_baseline(api.repository, SHA, current["accepted-baseline"]),
                                 json.loads(current["accepted-baseline"]))
                with patch.dict(api.status, state="failure", description="v1 restored=7/8"):
                    with self.assertRaises(ValueError):
                        harness._confirmed_baseline(api.repository, SHA, current["accepted-baseline"])
                    restored = outputs()
                    self.assertEqual(harness._confirmed_baseline(api.repository, SHA, restored["accepted-baseline"]),
                                     asdict(BASE))
