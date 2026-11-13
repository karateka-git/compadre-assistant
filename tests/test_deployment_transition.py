"""Проверяет переход на Deployments при сбоях записи и чтения GitHub."""

import unittest
from urllib.error import HTTPError

from scripts import publish_deployment_acceptance as publisher
from scripts import publish_installation_tag as installation
from scripts.deployment_acceptance import DeploymentAcceptanceError, require_accepted_deployment
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError
from tests.test_deployment_acceptance import FakeGitHub, RECEIPT
from tests.test_publish_deployment_acceptance import WritableGitHub
from tests.test_publish_installation_tag import FakeGitHub as LegacyGitHub
from tests.test_publish_installation_tag import RECEIPT as LEGACY_RECEIPT


class InterruptedGitHub(WritableGitHub):
    """Теряет первый read-back после выбранного успешного POST."""

    def __init__(self, interrupted_path: str) -> None:
        """Задаёт POST, после которого чтение один раз откажет."""
        super().__init__()
        self.interrupted_path = interrupted_path
        self.fail_next_read = False

    def post(self, path: str, body: dict) -> dict:
        """Сохраняет запись и обрывает последующее чтение результата."""
        result = super().post(path, body)
        if path == self.interrupted_path:
            self.fail_next_read = True
        return result

    def get(self, path: str, **parameters: str | int) -> object:
        """Однократно теряет список Deployments после применения POST."""
        if path == "/deployments" and self.fail_next_read:
            self.fail_next_read = False
            raise ControllerError("403 или потеря ответа read-back")
        return super().get(path, **parameters)


class DeniedGitHub(WritableGitHub):
    """Отказывает в праве записи Deployment или статуса."""

    def __init__(self, denied_path: str) -> None:
        """Задаёт endpoint, который отвечает отказом доступа."""
        super().__init__()
        self.denied_path = denied_path

    def post(self, path: str, body: dict) -> dict:
        """Имитирует 403 до создания объекта GitHub."""
        if path == self.denied_path:
            self.posts.append((path, body))
            raise PublishingError("HTTP 403: Deployments write отсутствует")
        return super().post(path, body)


class DeniedReadGitHub(WritableGitHub):
    """Возвращает локальный HTTP 403 при чтении списка Deployments."""

    def get(self, path: str, **parameters: str | int) -> object:
        """Имитирует ошибку доступа GitHub REST до выдачи записи."""
        if path == "/deployments":
            raise ControllerError("GitHub API недоступен: /deployments: HTTP 403") from HTTPError(
                path, 403, "Deployments read отсутствует", {}, None,
            )
        return super().get(path, **parameters)


class DeploymentTransitionTests(unittest.TestCase):
    """Проверяет остановку и повтор без реальных GitHub-запросов."""

    def test_interrupted_deployment_readback_resumes_without_duplicate(self) -> None:
        """После неизвестного ответа следующий сеанс находит созданный Deployment."""
        api = InterruptedGitHub("/deployments")
        with self.assertRaises(ControllerError):
            publisher.publish(api, lambda: RECEIPT)
        self.assertEqual(len(api.deployments), 1)
        self.assertEqual([path for path, _ in api.posts], ["/deployments"])

        publisher.publish(api, lambda: RECEIPT)
        self.assertEqual([path for path, _ in api.posts], ["/deployments", "/deployments/7/statuses"])
        require_accepted_deployment(api, RECEIPT)

    def test_interrupted_status_readback_resumes_without_second_status(self) -> None:
        """После потери чтения уже записанный success подтверждается повтором."""
        api = InterruptedGitHub("/deployments/7/statuses")
        with self.assertRaises(ControllerError):
            publisher.publish(api, lambda: RECEIPT)
        self.assertEqual(len(api.statuses), 1)
        before = len(api.posts)

        publisher.publish(api, lambda: RECEIPT)
        self.assertEqual(len(api.posts), before)
        require_accepted_deployment(api, RECEIPT)

    def test_write_permission_denial_fails_closed_at_both_endpoints(self) -> None:
        """403 при создании Deployment или статуса не даёт приёмку."""
        api = DeniedGitHub("/deployments")
        with self.assertRaisesRegex(publisher.DeploymentPublishError, "не подтверждено"):
            publisher.publish(api, lambda: RECEIPT)
        self.assertEqual(api.deployments, [])
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)

        api = DeniedGitHub("/deployments/7/statuses")
        api.deployments = FakeGitHub().deployments
        with self.assertRaisesRegex(publisher.DeploymentPublishError, "не подтверждено"):
            publisher.publish(api, lambda: RECEIPT)
        self.assertEqual(api.statuses, [])
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)

    def test_read_permission_denial_blocks_publish_and_rotation(self) -> None:
        """Без Deployments read нельзя подтвердить ни запись, ни ротацию."""
        api = DeniedReadGitHub()
        with self.assertRaisesRegex(ControllerError, "HTTP 403"):
            publisher.publish(api, lambda: RECEIPT)
        with self.assertRaisesRegex(ControllerError, "HTTP 403"):
            require_accepted_deployment(api, RECEIPT)
        self.assertEqual(api.posts, [])

    def test_legacy_tag_remains_and_new_release_requires_success(self) -> None:
        """Тег v3.2.0 остаётся, а следующий last_good не получает fallback."""
        legacy = LegacyGitHub()
        installation.publish(legacy, lambda: LEGACY_RECEIPT, LEGACY_RECEIPT.runtime_id)
        self.assertTrue(installation.installation_tag_exists(
            legacy, LEGACY_RECEIPT.version, LEGACY_RECEIPT.sha, receipt=LEGACY_RECEIPT))
        historical_ref = legacy.ref

        current = WritableGitHub()
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(current, RECEIPT)
        self.assertFalse(any(path.startswith("/git/ref/tags/production/") for path, _ in current.calls))
        publisher.publish(current, lambda: RECEIPT)
        require_accepted_deployment(current, RECEIPT)
        self.assertEqual(legacy.ref, historical_ref)
        self.assertEqual(len(legacy.posts), 2)


if __name__ == "__main__":
    unittest.main()
