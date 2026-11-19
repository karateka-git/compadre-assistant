"""Проверяет запись D18 и восстановление после потери ответов двух API."""

from dataclasses import asdict, replace
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import deployment_writer as writer
from scripts.deployment_acceptance import DeploymentAcceptanceError, DeploymentAttempt, DeploymentState as State
from scripts.github_publishing import PublishingError
from scripts.publish_deployment_acceptance import DeploymentPublishError
from tests.test_deployment_acceptance import FakeGitHub, OWNER, RECEIPT, SHA, VERSION
from tests.test_deployment_reader_v2 import AttemptGitHub, BASE


ATTEMPT = DeploymentAttempt(2, "12001:1", VERSION, SHA, "ordinary", 41, 107, 2, BASE)


class WriterGitHub(AttemptGitHub):
    """Моделирует задержку видимости, отказ и потерю ответа после POST."""

    def __init__(self):
        """Начинает с принятого D17."""
        super().__init__()
        self.deployments.pop()
        self.records = {7: self.statuses[0]}
        self.posts = []
        self.fault = None

    def authenticated_user(self):
        """Возвращает автора будущей записи."""
        return OWNER

    def graphql(self, query, **variables):
        """Возвращает ограниченные узлы с текущими статусами."""
        nodes = []
        for row in reversed(self.deployments):
            status = self.records.get(row["id"])
            node = {"id": row["node_id"], "createdAt": row["created_at"],
                    "latestStatus": {"id": status["node_id"]} if status else None}
            if variables.get("id") == node["id"]:
                return {"node": node}
            nodes.append(node)
        return {"repository": {"nameWithOwner": self.repository,
                               "deployments": {"nodes": nodes[:2], "totalCount": len(nodes)}}}

    def get(self, path, **parameters):
        """Предоставляет REST-объекты тех же записей."""
        if path == "/deployments" and parameters.get("per_page") == 10:
            return self.deployments
        if path.startswith("/deployments/"):
            deployment_id = int(path.split("/")[2])
            if path.endswith("/statuses"):
                return [self.records[deployment_id]] if deployment_id in self.records else []
            return next(row for row in self.deployments if row["id"] == deployment_id)
        return FakeGitHub.get(self, path, **parameters)

    def post(self, path, body):
        """Записывает объект и имитирует отдельно исход POST и его видимость."""
        self.posts.append(path)
        if self.fault == "rejected":
            raise PublishingError("POST") from HTTPError("", 422, "", {}, None)
        if self.fault == "unknown":
            raise PublishingError("timeout")
        if path == "/deployments":
            self.deployments.append(self.latest | {"payload": body["payload"]})
            result = self.latest["id"]
        else:
            result = 20 + len(self.posts)
            self.records[11] = self.status | body | {"id": result, "node_id": f"S{result}"}
        if self.fault == "lost":
            raise PublishingError("timeout")
        return {"id": result}


class WriterTests(unittest.TestCase):
    """Проверяет ключ, границы статусов и запрет второй отправки."""

    def setUp(self):
        """Создаёт отдельный журнал намерений для каждой попытки."""
        self.enterContext(patch.dict(os.environ, COMPADRE_DEPLOYMENT_READER="v2"))
        self.api = WriterGitHub()
        self.log = set()

    def claim(self, action):
        """Однократно выдаёт разрешение на отправку для действия."""
        if action in self.log:
            return False
        self.log.add(action)
        return True

    def create(self):
        """Открывает попытку по неизменному ключу."""
        return writer.create_attempt(self.api, ATTEMPT, self.claim)

    def status(self, state, receipt=RECEIPT):
        """Запрашивает переход с заданной приёмкой VPS."""
        def authorize(action):
            return receipt if self.claim(action) else None
        completed = None if state is State.IN_PROGRESS else receipt
        return writer.write_status(self.api, ATTEMPT, 11, state, authorize, completed)

    def test_lost_responses_reread_same_objects(self):
        """Каждый ответ теряется после записи, повтор видит тот же D18 и статус."""
        self.api.fault = "lost"
        self.assertEqual(self.create(), self.create())
        for state in (State.IN_PROGRESS, State.SUCCESS):
            self.assertEqual(self.status(state), self.status(state))
        self.assertEqual(len(self.api.posts), 3)
        with self.assertRaises(DeploymentPublishError):
            self.status(State.FAILURE)

    def test_unknown_and_explicit_rejection(self):
        """После неизведанного исхода новая попытка чтения не отправляет POST."""
        for failure, reason in (("unknown", "не подтверждено"), ("rejected", "422")):
            with self.subTest(failure=failure):
                self.api.fault = failure
                self.log.clear()
                self.api.posts.clear()
                with self.assertRaisesRegex(DeploymentPublishError, reason):
                    self.create()
                self.api.fault = None
                with self.assertRaisesRegex(DeploymentPublishError, "сверки"):
                    self.create()
                self.assertEqual(self.api.posts, ["/deployments"])

    def test_return_needs_exact_old_generation_and_status(self):
        """Возврат другого поколения или D17 с изменённым статусом закрыт."""
        self.create()
        self.status(State.IN_PROGRESS)
        with self.assertRaises(DeploymentPublishError):
            self.status(State.FAILURE, replace(RECEIPT, generation_id="9" * 32))
        with patch.dict(self.api.records[7], id=33), self.assertRaises(DeploymentAcceptanceError):
            self.status(State.FAILURE)
        self.assertEqual(self.status(State.FAILURE).description, "v1 restored=7/8")
        self.assertEqual(self.status(State.FAILURE).status_id, self.api.records[11]["id"])

    def test_another_attempt_same_sha_and_stale_runner(self):
        """Владелец определяется ключом и ID, включая повтор прежнего SHA."""
        self.create()
        newer = replace(ATTEMPT, attempt_key="13002:2")
        self.api.deployments.append(self.api.latest | {"id": 15, "node_id": "D15",
                                    "created_at": "2026-01-04T00:00:00Z", "payload": asdict(newer)})
        with self.assertRaises(DeploymentPublishError):
            self.status(State.IN_PROGRESS)
        with self.assertRaises(DeploymentAcceptanceError):
            self.create()
        with self.assertRaises(DeploymentPublishError):
            writer.current_attempt(self.api, newer, 11)
        self.assertEqual(writer.current_attempt(self.api, newer, 15)[0].state, State.MISSING)
        self.assertEqual(len(self.api.posts), 1)

    def test_status_requires_receipt_from_grant(self):
        """Неверная свежая приёмка от контроллера оставляет POST неподтверждённым."""
        self.create()
        self.status(State.IN_PROGRESS)
        changed = replace(RECEIPT, runtime_id="f" * 64)
        with self.assertRaisesRegex(DeploymentPublishError, "Runtime VPS изменился"):
            writer.write_status(self.api, ATTEMPT, 11, State.SUCCESS, lambda action: changed, RECEIPT)
        self.assertEqual(self.api.posts, ["/deployments", "/deployments/11/statuses"])
