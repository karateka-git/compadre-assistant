"""Проверяет чтение приёмки Deployments на локальной модели GitHub API."""

from dataclasses import asdict, replace
import unittest

from scripts.ci_repository_compatibility import ReleaseTagEvidence
from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deployment_acceptance import (
    DeploymentAcceptanceError, DeploymentState, read_deployment_record, require_accepted_deployment,
)
from scripts.full_ci_controller import ControllerError, GitHub
from scripts.publish_installation_tag import InstallationReceipt
from scripts.publish_installation_tag import summarize
from scripts.rotate_ci_baseline import require_accepted_installation
from scripts.release_sqlite_summary import MIGRATIONS_PREFIX


VERSION = "v3.4.1"
SHA = "1" * 40
TAG_SHA = "2" * 40
OWNER = {"id": 42288695, "login": "karateka-git"}
RECEIPT = InstallationReceipt(1, VERSION, SHA, "ordinary", 41, 107, 2,
                              "3" * 32, "0002_schema.sql", "4" * 64)


class FakeGitHub:
    """Возвращает точные ответы Release, Deployment и статуса с учётом страниц."""

    repository = EXPECTED_REPOSITORY
    pages = GitHub.pages

    def __init__(self) -> None:
        """Готовит одну принятую запись и счётчик запросов."""
        self.calls: list[tuple[str, dict]] = []
        payload = {key: value for key, value in asdict(RECEIPT).items() if key != "runtime_id"}
        self.deployments = [{
            "id": 7, "sha": SHA, "ref": SHA, "task": "compadre:acceptance",
            "environment": "production", "payload": payload, "creator": OWNER,
            "transient_environment": False, "production_environment": True,
        }]
        self.statuses = [{"id": 8, "state": "success", "environment": "production", "creator": OWNER,
                          "created_at": "2026-01-02T01:00:00Z"}]
        self.fail_path: str | None = None

    def get(self, path: str, **parameters: str | int) -> object:
        """Считает каждый REST GET и имитирует серверную пагинацию."""
        self.calls.append((path, parameters))
        if path == self.fail_path:
            raise ControllerError("API unavailable")
        if path == f"/releases/tags/{VERSION}":
            return {
                "id": 41, "tag_name": VERSION, "name": VERSION, "draft": False,
                "prerelease": False, "immutable": True, "published_at": "2026-01-01T01:00:00Z",
                "target_commitish": SHA,
                "body": f"Тип выпуска: обычный\nCommit: `{SHA}`\nВетка: `release/3.4`\n"
                        f"Полный CI: https://github.com/{EXPECTED_REPOSITORY}/actions/runs/107/attempts/2",
            }
        if path == f"/git/ref/tags/{VERSION}":
            return {"ref": f"refs/tags/{VERSION}", "object": {"type": "tag", "sha": TAG_SHA}}
        if path == f"/git/tags/{TAG_SHA}":
            return {
                "sha": TAG_SHA, "tag": VERSION, "object": {"type": "commit", "sha": SHA},
                "message": ReleaseTagEvidence("5" * 40, "6" * 40, 107, 2, "7" * 64).message(),
            }
        if path == f"/contents/{MIGRATIONS_PREFIX.rstrip('/')}":
            if parameters != {"ref": SHA}:
                raise AssertionError(parameters)
            return [{"name": name, "path": MIGRATIONS_PREFIX + name, "type": "file"}
                    for name in ("0001_initial_schema.sql", "0002_schema.sql")]
        if path == "/deployments":
            if {key: value for key, value in parameters.items() if key not in ("page", "per_page")} != {
                "sha": SHA, "task": "compadre:acceptance", "environment": "production",
            }:
                raise AssertionError(parameters)
            start = (int(parameters["page"]) - 1) * int(parameters["per_page"])
            return self.deployments[start:start + int(parameters["per_page"])]
        if path == "/deployments/7/statuses":
            if parameters.get("per_page") != 100:
                raise AssertionError(parameters)
            start = (int(parameters["page"]) - 1) * int(parameters["per_page"])
            return self.statuses[start:start + int(parameters["per_page"])]
        raise AssertionError(path)


class DeploymentAcceptanceTests(unittest.TestCase):
    """Проверяет допуск, конфликт, авторство и границы списка."""

    def test_accepts_exact_current_success_and_counts_requests(self) -> None:
        """Шесть чтений достаточно для одной записи с аннотированным Release."""
        api = FakeGitHub()
        record = require_accepted_deployment(api, RECEIPT)
        self.assertEqual((record.deployment_id, record.status_id, record.state),
                         (7, 8, DeploymentState.SUCCESS))
        self.assertEqual(len(api.calls), 6)

    def test_missing_and_unfinished_status_do_not_grant_acceptance(self) -> None:
        """Отсутствие записи или статуса доступно для будущего безопасного повтора."""
        api = FakeGitHub()
        api.deployments = []
        self.assertIsNone(read_deployment_record(api, RECEIPT))
        self.assertEqual(len(api.calls), 5)
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)
        api = FakeGitHub()
        api.statuses = []
        self.assertEqual(read_deployment_record(api, RECEIPT).state, DeploymentState.MISSING)
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)
        api.statuses = [{"id": 8, "state": "pending", "environment": "production", "creator": OWNER,
                         "created_at": "2026-01-02T01:00:00Z"}]
        self.assertEqual(read_deployment_record(api, RECEIPT).state, DeploymentState.PENDING)
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)

    def test_rejects_payload_release_and_migration_conflicts(self) -> None:
        """Данные записи должны точно совпадать с Release и текущей приёмкой."""
        for field, value in (("run_attempt", 3), ("generation_id", "9" * 32), ("extra", True)):
            with self.subTest(field=field):
                api = FakeGitHub()
                api.deployments[0]["payload"] = {**api.deployments[0]["payload"], field: value}
                with self.assertRaises(DeploymentAcceptanceError):
                    require_accepted_deployment(api, RECEIPT)
        api = FakeGitHub()
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, InstallationReceipt(
                1, VERSION, SHA, "ordinary", 42, 107, 2, "3" * 32,
                "0002_schema.sql", "4" * 64,
            ))
        api = FakeGitHub()
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, InstallationReceipt(
                1, VERSION, SHA, "ordinary", 41, 107, 2, "3" * 32,
                "0001_initial_schema.sql", "4" * 64,
            ))

    def test_rejects_wrong_author_and_unknown_status(self) -> None:
        """Автор обоих объектов и неизвестный статус влияют на решение."""
        api = FakeGitHub()
        api.deployments[0]["creator"] = {"id": 1, "login": "someone"}
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)
        api = FakeGitHub()
        api.statuses[0]["creator"] = {"id": 1, "login": "someone"}
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)
        api = FakeGitHub()
        api.statuses[0]["state"] = "new_state"
        self.assertEqual(read_deployment_record(api, RECEIPT).state, DeploymentState.UNKNOWN)
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)
        api.statuses[0]["state"] = {"unexpected": "value"}
        self.assertEqual(read_deployment_record(api, RECEIPT).state, DeploymentState.UNKNOWN)

    def test_current_failure_overrides_older_success(self) -> None:
        """Исторический success не подменяет текущий отказ."""
        api = FakeGitHub()
        api.statuses = [
            {"id": 8, "state": "success", "environment": "production", "creator": OWNER,
             "created_at": "2026-01-02T01:00:00Z"},
            {"id": 9, "state": "failure", "environment": "production", "creator": OWNER,
             "created_at": "2026-01-02T01:00:01Z"},
        ]
        self.assertEqual(read_deployment_record(api, RECEIPT).state, DeploymentState.FAILURE)
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)

    def test_ambiguous_timestamp_refuses_acceptance(self) -> None:
        """Два статуса в одну секунду не дают доказать текущий статус."""
        api = FakeGitHub()
        api.statuses.append({"id": 9, "state": "failure", "environment": "production", "creator": OWNER,
                             "created_at": "2026-01-02T01:00:00Z"})
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)

    def test_reads_status_beyond_full_page(self) -> None:
        """Новый отказ за сотым статусом перекрывает старые success."""
        api = FakeGitHub()
        api.statuses = [api.statuses[0] | {"id": number} for number in range(1, 101)]
        api.statuses.append({"id": 101, "state": "failure", "environment": "production",
                             "creator": OWNER, "created_at": "2026-01-02T01:00:01Z"})
        # Исторические статусы имеют одну дату, но только максимальная дата
        # должна однозначно определять текущий статус.
        self.assertEqual(read_deployment_record(api, RECEIPT).state, DeploymentState.FAILURE)
        self.assertEqual([params["page"] for path, params in api.calls
                          if path == "/deployments/7/statuses"], [1, 2])
        self.assertEqual(len(api.calls), 7)

    def test_reads_past_full_page_before_rejecting_duplicates(self) -> None:
        """Дубликат на следующей странице нельзя скрыть первой сотней результатов."""
        api = FakeGitHub()
        api.deployments = api.deployments * 101
        with self.assertRaises(DeploymentAcceptanceError):
            read_deployment_record(api, RECEIPT)
        self.assertEqual([params["page"] for path, params in api.calls if path == "/deployments"], [1, 2])
        self.assertEqual(len(api.calls), 6)

    def test_api_failure_closes_admission(self) -> None:
        """Недоступность статуса не превращается в отсутствие записи."""
        api = FakeGitHub()
        api.fail_path = "/deployments/7/statuses"
        with self.assertRaises(ControllerError):
            require_accepted_deployment(api, RECEIPT)

    def test_rotation_uses_deployment_for_new_last_good_and_forward_recovery(self) -> None:
        """Теговый fallback не допускает новый выпуск после обычного или forward recovery."""
        api = FakeGitHub()
        require_accepted_installation(api, RECEIPT)
        api.statuses = []
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_installation(api, RECEIPT)
        api.statuses = [{"id": 8, "state": "success", "environment": "production", "creator": OWNER,
                         "created_at": "2026-01-02T01:00:00Z"}]
        recovered = replace(RECEIPT, runtime_id="9" * 64)
        require_accepted_installation(api, recovered)
        self.assertFalse(any(path.startswith("/git/ref/tags/production/") for path, _ in api.calls))

    def test_summary_reads_only_verified_deployment_for_new_release(self) -> None:
        """Сводка показывает SQLite только при текущем success точной записи."""
        api = FakeGitHub()
        accepted = summarize(api, VERSION, SHA)
        self.assertEqual(accepted["installation_status"], "accepted")
        self.assertEqual(accepted["deployment_state"], "success")
        self.assertEqual(accepted["generation_id"], RECEIPT.generation_id)
        self.assertEqual(accepted["applied_migration"], RECEIPT.migration)
        self.assertIsNone(accepted["installation_tag_object_sha"])
        self.assertFalse(any(path.startswith("/git/ref/tags/production/") for path, _ in api.calls))

        api = FakeGitHub()
        api.deployments = []
        pending = summarize(api, VERSION, SHA)
        self.assertEqual((pending["installation_status"], pending["deployment_state"]), ("pending", "absent"))
        self.assertIsNone(pending["generation_id"])

        api = FakeGitHub()
        api.statuses[0]["state"] = "failure"
        failed = summarize(api, VERSION, SHA)
        self.assertEqual((failed["installation_status"], failed["deployment_state"]), ("pending", "failure"))
        self.assertIsNone(failed["generation_id"])

        api = FakeGitHub()
        api.deployments[0]["payload"]["generation_id"] = "wrong"
        with self.assertRaises(DeploymentAcceptanceError):
            summarize(api, VERSION, SHA)

    def test_emergency_summary_uses_ci_attempt_from_accepted_payload(self) -> None:
        """Аварийный Release не содержит успешную попытку в манифесте."""
        class EmergencyGitHub(FakeGitHub):
            """Подставляет прямой тег и аварийный манифест Release."""

            def get(self, path: str, **parameters: str | int) -> object:
                """Читает аварийный Release, сохраняя остальные ответы fake."""
                if path == f"/releases/tags/{VERSION}":
                    result = super().get(path, **parameters)
                    result["body"] = (
                        f"Тип выпуска: аварийный\nCommit: `{SHA}`\nВетка: `release/3.4`\n"
                        "Причина: исправление\n"
                        f"Полный CI: release-full.yml failure — https://github.com/{EXPECTED_REPOSITORY}/actions/runs/1/attempts/1\n"
                        f"PR и состояние на момент merge:\n- PR #4 -> release/3.4; merge `{SHA}` "
                        "(release); pr-check=success [ci]; bypass=no\n"
                        "Неподтверждённые или неуспешные проверки:"
                    )
                    return result
                if path == f"/git/ref/tags/{VERSION}":
                    self.calls.append((path, parameters))
                    return {"ref": f"refs/tags/{VERSION}", "object": {"type": "commit", "sha": SHA}}
                return super().get(path, **parameters)

        api = EmergencyGitHub()
        api.deployments[0]["payload"]["kind"] = "emergency"
        summary = summarize(api, VERSION, SHA)
        self.assertEqual((summary["installation_status"], summary["run_id"], summary["run_attempt"]),
                         ("accepted", RECEIPT.run_id, RECEIPT.run_attempt))
        require_accepted_installation(api, replace(RECEIPT, kind="emergency"))


if __name__ == "__main__":
    unittest.main()
