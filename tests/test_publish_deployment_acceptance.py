"""Проверяет публикацию приёмки на локальной модели GitHub API."""

from dataclasses import asdict, replace
import unittest

from scripts import publish_deployment_acceptance as publisher
from scripts.deployment_acceptance import DeploymentAcceptanceError, DeploymentState
from scripts.github_publishing import PublishingError
from tests.test_deployment_acceptance import FakeGitHub, OWNER, RECEIPT, SHA


class WritableGitHub(FakeGitHub):
    """Хранит локальные Deployment и status и имитирует потерянные ответы."""

    def __init__(self) -> None:
        """Готовит пустую историю и режимы сбоев."""
        super().__init__()
        self.deployments = []
        self.statuses = []
        self.actor = OWNER
        self.posts: list[tuple[str, dict]] = []
        self.lose_deployment = False
        self.lose_status = False
        self.apply_deployment = True
        self.apply_status = True
        self.run_url = f"https://github.com/{self.repository}/actions/runs/107"

    def authenticated_user(self) -> dict:
        """Возвращает локальную личность владельца."""
        return self.actor

    def get(self, path: str, **parameters: str | int) -> object:
        """Добавляет к существующему fake точную попытку CI."""
        if path == "/actions/runs/107/attempts/2":
            self.calls.append((path, parameters))
            return {"id": 107, "run_attempt": 2, "head_sha": SHA, "status": "completed",
                    "conclusion": "success", "html_url": self.run_url}
        return super().get(path, **parameters)

    def post(self, path: str, body: dict) -> dict:
        """Применяет запись отдельно от возможной потери ответа."""
        self.posts.append((path, body))
        if path == "/deployments":
            if self.apply_deployment:
                self.deployments = [{"id": 7, "sha": SHA, "ref": body["ref"], "task": body["task"],
                                     "environment": body["environment"], "payload": body["payload"],
                                     "creator": OWNER, "transient_environment": body["transient_environment"],
                                     "production_environment": body["production_environment"]}]
            if self.lose_deployment:
                raise PublishingError("deployment response lost")
            return self.deployments[0]
        if path == "/deployments/7/statuses":
            if self.apply_status:
                self.statuses = [{"id": 8, "state": body["state"], "environment": body["environment"],
                                  "creator": OWNER, "created_at": "2026-11-11T01:00:00Z"}]
            if self.lose_status:
                raise PublishingError("status response lost")
            return self.statuses[0]
        raise AssertionError(path)


class PublishDeploymentAcceptanceTests(unittest.TestCase):
    """Проверяет запись, повтор и отказ без слепого повторного POST."""

    def test_creates_exact_deployment_and_status_then_confirms_repeat(self) -> None:
        """Первый сеанс создаёт два объекта, повтор только читает их."""
        api = WritableGitHub()
        record = publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        self.assertEqual((record.deployment_id, record.status_id, record.state),
                         (7, 8, DeploymentState.SUCCESS))
        self.assertEqual([path for path, _ in api.posts], ["/deployments", "/deployments/7/statuses"])
        self.assertEqual(api.posts[0][1], {
            "ref": SHA, "task": "compadre:acceptance", "auto_merge": False,
            "required_contexts": [], "payload": {k: v for k, v in asdict(RECEIPT).items() if k != "runtime_id"},
            "environment": "production", "description": f"Приёмка {RECEIPT.version} на VPS",
            "transient_environment": False, "production_environment": True,
        })
        self.assertEqual(api.posts[1][1]["auto_inactive"], False)
        self.assertEqual(api.posts[1][1]["log_url"],
                         f"https://github.com/{api.repository}/actions/runs/107")
        self.assertNotIn(RECEIPT.runtime_id, repr(api.posts))
        self.assertEqual(publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id), record)
        self.assertEqual(len(api.posts), 2)

    def test_finishes_existing_deployment_without_status(self) -> None:
        """После прерывания создаёт только недостающий статус."""
        api = WritableGitHub()
        api.deployments = FakeGitHub().deployments
        record = publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        self.assertEqual(record.state, DeploymentState.SUCCESS)
        self.assertEqual([path for path, _ in api.posts], ["/deployments/7/statuses"])

    def test_lost_deployment_response_is_read_back_before_status(self) -> None:
        """Потерянный ответ на первый POST не создаёт дубликат."""
        api = WritableGitHub()
        api.lose_deployment = True
        self.assertEqual(publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id).state,
                         DeploymentState.SUCCESS)
        self.assertEqual(len([path for path, _ in api.posts if path == "/deployments"]), 1)

    def test_lost_status_response_is_read_back_before_success(self) -> None:
        """Потерянный ответ на status POST подтверждается новым чтением."""
        api = WritableGitHub()
        api.lose_status = True
        self.assertEqual(publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id).state,
                         DeploymentState.SUCCESS)
        self.assertEqual(len([path for path, _ in api.posts if path.endswith("/statuses")]), 1)

    def test_unconfirmed_post_does_not_retry_blindly(self) -> None:
        """При неопределённом результате следующий сеанс начинает с чтения."""
        api = WritableGitHub()
        api.lose_deployment, api.apply_deployment = True, False
        with self.assertRaises(publisher.DeploymentPublishError):
            publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        self.assertEqual(len(api.posts), 1)
        api = WritableGitHub()
        api.lose_status, api.apply_status = True, False
        with self.assertRaises(publisher.DeploymentPublishError):
            publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        self.assertEqual(len(api.posts), 2)
        api.lose_status, api.apply_status = False, True
        self.assertEqual(publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id).state,
                         DeploymentState.SUCCESS)
        self.assertEqual([path for path, _ in api.posts].count("/deployments"), 1)

    def test_rejects_owner_runtime_ci_and_conflicting_state(self) -> None:
        """Разрешение записи не обходит живую проверку и существующий отказ."""
        api = WritableGitHub()
        api.actor = {"id": 1, "login": "other"}
        with self.assertRaises(publisher.DeploymentPublishError):
            publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        self.assertEqual(api.posts, [])
        api.actor = OWNER
        with self.assertRaises(publisher.DeploymentPublishError):
            publisher.publish(api, lambda: RECEIPT, "0" * 64)
        api.run_url = "https://example.invalid/other"
        with self.assertRaises(publisher.DeploymentPublishError):
            publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        api.run_url = f"https://github.com/{api.repository}/actions/runs/107"
        api.deployments = FakeGitHub().deployments
        api.statuses = [{"id": 8, "state": "failure", "environment": "production", "creator": OWNER,
                         "created_at": "2026-11-11T01:00:00Z"}]
        with self.assertRaises(publisher.DeploymentPublishError):
            publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        self.assertEqual(api.posts, [])

    def test_duplicate_deployment_refuses_write(self) -> None:
        """Две записи того же SHA требуют решения владельца."""
        api = WritableGitHub()
        api.deployments = FakeGitHub().deployments * 2
        with self.assertRaises(DeploymentAcceptanceError):
            publisher.publish(api, lambda: RECEIPT, RECEIPT.runtime_id)
        self.assertEqual(api.posts, [])

    def test_historical_installation_is_not_backfilled(self) -> None:
        """Историческая база CI сохраняет тег как источник приёмки."""
        api = WritableGitHub()
        historical = replace(RECEIPT, version="v3.2.0")
        with self.assertRaises(publisher.DeploymentPublishError):
            publisher.publish(api, lambda: historical, RECEIPT.runtime_id)
        self.assertEqual(api.posts, [])


if __name__ == "__main__":
    unittest.main()
