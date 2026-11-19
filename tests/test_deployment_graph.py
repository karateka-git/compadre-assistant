"""Проверяет ограниченные чтения Deployments и связь REST с GraphQL."""

import unittest
from unittest.mock import Mock, patch

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deployment_acceptance import (
    DeploymentAcceptanceError, DeploymentState, _exact_production, _latest_production,
)
from scripts.full_ci_controller import ControllerError, GitHub


class DeploymentGraphTests(unittest.TestCase):
    """Проверяет доверие, порядок, целостность и отсутствие обхода страниц."""

    def setUp(self):
        """Готовит страницу REST с обратным порядком и ID вне GraphQL Int."""
        self.api = Mock(repository=EXPECTED_REPOSITORY)
        self.owner = {"id": 42288695, "login": "karateka-git"}
        self.new = {"id": "D1", "createdAt": "2026-11-01T00:00:00Z", "latestStatus": {"id": "S1"}}
        self.old = self.new | {"id": "D0", "createdAt": "2026-10-01T00:00:00Z"}
        self.connection = {"nodes": [self.new, self.old], "totalCount": 1000}
        self.repo = {"nameWithOwner": EXPECTED_REPOSITORY, "deployments": self.connection}
        self.api.graphql.return_value = {"repository": self.repo, "node": self.new}
        self.row = {"node_id": "D1", "id": 8000000000, "created_at": self.new["createdAt"],
                    "sha": "a" * 40, "ref": "a" * 40, "task": "compadre:acceptance",
                    "environment": "production", "transient_environment": False,
                    "production_environment": True, "creator": self.owner}
        self.status = {"id": 23000000000, "node_id": "S1", "creator": self.owner, "environment": "production",
                       "state": "success", "created_at": self.new["createdAt"], "description": "result"}
        self.deployments = [self.row | {"node_id": "D0"}, self.row]
        self.statuses = [self.status | {"node_id": "S0", "state": "failure"}, self.status]
        self.api.get.side_effect = self.get

    def get(self, path, **filters):
        """Проверяет область и верхнюю границу каждого REST-запроса."""
        if path == "/deployments/8000000000":
            self.assertFalse(filters)
            return self.row
        if path == "/deployments":
            self.assertEqual(filters.pop("environment"), "production")
            result = self.deployments
        else:
            self.assertEqual(path, "/deployments/8000000000/statuses")
            result = self.statuses
        self.assertEqual(filters, {"page": 1, "per_page": 10})
        return result[:10]

    def test_bounded_identity(self):
        """Точный ID статуса различает даже два статуса одной секунды."""
        for read in (_latest_production, lambda api: _exact_production(api, 8000000000)):
            self.api.reset_mock()
            _, record = read(self.api)
            self.assertEqual((record.deployment_id, record.status_id, record.description),
                             (8000000000, 23000000000, "result"))
            self.assertEqual(self.api.graphql.call_count, 1)
            self.assertEqual(self.api.get.call_count, 2)

    def test_rejects_tie_partial_untrusted_and_missing_match(self):
        """Неполная страница, неверный автор или ничья закрывают выбор."""
        cases = [
            (self.old, {"createdAt": self.new["createdAt"]}),
            (self.connection, {"nodes": [self.new]}),
            (self.connection, {"totalCount": False}),
            (self.repo, {"nameWithOwner": "other/repo"}),
            (self.row, {"creator": {}}),
            (self.status, {"creator": {}}),
            (self.new, {"latestStatus": {"id": "absent"}}),
            (self.new, {"latestStatus": None}),
        ]
        for target, change in cases:
            with self.subTest(change=change), patch.dict(target, change), self.assertRaises(DeploymentAcceptanceError):
                _latest_production(self.api)
        self.statuses[:] = [self.status | {"node_id": f"other{i}"} for i in range(10)] + [self.status]
        with self.assertRaises(DeploymentAcceptanceError):
            _latest_production(self.api)

    def test_missing_and_unknown_states(self):
        """Требует явное поле latestStatus и отличает пустой результат от неизвестного."""
        del self.new["latestStatus"]
        with self.assertRaises(DeploymentAcceptanceError):
            _latest_production(self.api)
        self.new["latestStatus"] = {"id": "S1"}
        self.status["state"] = "future"
        self.assertIs(_latest_production(self.api)[1].state, DeploymentState.UNKNOWN)
        self.new["latestStatus"] = None
        self.statuses.clear()
        self.assertIs(_latest_production(self.api)[1].state, DeploymentState.MISSING)

    def test_graphql_partial_response(self):
        """Даже одновременно возвращённые data и errors закрывают допуск."""
        api = GitHub(EXPECTED_REPOSITORY, "dummy")
        for result in ({"data": {}, "errors": [{}]}, {"data": None}, {}):
            with patch.object(api, "_json", return_value=result), self.assertRaises(ControllerError):
                api.graphql("query { viewer { login } }")
