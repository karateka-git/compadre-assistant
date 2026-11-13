"""Проверяет чтение приёмки Deployments на локальной модели GitHub API."""

from dataclasses import asdict, replace
import unittest

from scripts.ci_repository_compatibility import DeploymentReleaseTagEvidence, ReleaseTagEvidence
from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deployment_acceptance import (
    AcceptedBaseline, DeploymentAcceptanceError, DeploymentState, read_deployment_record,
    require_accepted_deployment, resolve_accepted_baseline, verify_accepted_baseline,
)
from scripts.full_ci_controller import ControllerError, GitHub
from scripts.publish_installation_tag import InstallationReceipt
from scripts.publish_installation_tag import summarize
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
        self.statuses_by_deployment: dict[int, list[dict]] = {}
        self.fail_path: str | None = None
        self.annotation = ReleaseTagEvidence("5" * 40, "6" * 40, 107, 2, "7" * 64)

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
                "message": self.annotation.message(),
            }
        if path == f"/contents/{MIGRATIONS_PREFIX.rstrip('/')}":
            if parameters != {"ref": SHA}:
                raise AssertionError(parameters)
            return [{"name": name, "path": MIGRATIONS_PREFIX + name, "type": "file"}
                    for name in ("0001_initial_schema.sql", "0002_schema.sql")]
        if path == "/deployments":
            filters = {key: value for key, value in parameters.items() if key not in ("page", "per_page", "sha")}
            if filters != {"task": "compadre:acceptance", "environment": "production"}:
                raise AssertionError(parameters)
            start = (int(parameters["page"]) - 1) * int(parameters["per_page"])
            sha = parameters.get("sha")
            deployments = [item for item in self.deployments if sha is None or item.get("sha") == sha]
            return deployments[start:start + int(parameters["per_page"])]
        if path.startswith("/deployments/") and path.endswith("/statuses"):
            if parameters.get("per_page") != 100:
                raise AssertionError(parameters)
            start = (int(parameters["page"]) - 1) * int(parameters["per_page"])
            deployment_id = int(path.split("/")[2])
            statuses = self.statuses if deployment_id == 7 else self.statuses_by_deployment[deployment_id]
            return statuses[start:start + int(parameters["per_page"])]
        if path == "/deployments/7":
            return self.deployments[0]
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

    def test_current_release_v2_is_accepted_without_requiring_its_previous_deployment(self) -> None:
        """Resume установленного выпуска может перечитать свой success после перехода базы."""
        api = FakeGitHub()
        old = AcceptedBaseline("5" * 40, "v3.4.0", 4, 5, 6)
        api.annotation = DeploymentReleaseTagEvidence(old.sha, "6" * 40, 107, 2, "7" * 64, old)
        record = require_accepted_deployment(api, RECEIPT)
        self.assertEqual((record.deployment_id, record.status_id), (7, 8))
        self.assertEqual(resolve_accepted_baseline(api), AcceptedBaseline(SHA, VERSION, 41, 7, 8))
        self.assertNotIn("/deployments/5", [path for path, _ in api.calls])

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
        require_accepted_deployment(api, RECEIPT)
        api.statuses = []
        with self.assertRaises(DeploymentAcceptanceError):
            require_accepted_deployment(api, RECEIPT)
        api.statuses = [{"id": 8, "state": "success", "environment": "production", "creator": OWNER,
                         "created_at": "2026-01-02T01:00:00Z"}]
        recovered = replace(RECEIPT, runtime_id="9" * 64)
        require_accepted_deployment(api, recovered)
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
        require_accepted_deployment(api, replace(RECEIPT, kind="emergency"))
        self.assertEqual(resolve_accepted_baseline(api), AcceptedBaseline(SHA, VERSION, 41, 7, 8))


class AcceptedBaselineTests(unittest.TestCase):
    """Проверяет актуальную и сохранённую базу CI из Deployments."""

    @staticmethod
    def second_deployment(api: FakeGitHub, *, time: str = "2026-01-01T01:00:00Z",
                          state: str = "success") -> None:
        """Добавляет запись более высокого номера версии и Deployment ID."""
        other_sha = "8" * 40
        current = api.deployments[0]
        payload = current["payload"] | {"sha": other_sha, "version": "v3.9.0", "release_id": 42}
        api.deployments.append(current | {"id": 9, "sha": other_sha, "ref": other_sha, "payload": payload})
        api.statuses_by_deployment[9] = [api.statuses[0] | {"id": 10, "created_at": time, "state": state}]

    def test_resolves_by_current_success_time_and_validates_selected_release(self) -> None:
        """Порядок выдачи, версия и числовой ID не определяют последнюю приёмку."""
        api = FakeGitHub()
        self.second_deployment(api)
        api.deployments.reverse()
        self.assertEqual(resolve_accepted_baseline(api), AcceptedBaseline(SHA, VERSION, 41, 7, 8))
        self.assertEqual([path for path, _ in api.calls if path.startswith("/releases/tags/")],
                         [f"/releases/tags/{VERSION}"])

    def test_resolves_latest_status_after_incomplete_and_failed_attempts(self) -> None:
        """Записи без действующего success не становятся базой."""
        for state in ("pending", "in_progress", "queued", "error", "failure", "inactive", None):
            with self.subTest(state=state):
                api = FakeGitHub()
                self.second_deployment(api, time="2026-01-03T01:00:00Z", state="success")
                if state is None:
                    api.statuses_by_deployment[9] = []
                else:
                    api.statuses_by_deployment[9].append(
                        api.statuses_by_deployment[9][0] | {
                            "id": 11, "state": state, "created_at": "2026-01-04T01:00:00Z",
                        },
                    )
                self.assertEqual(resolve_accepted_baseline(api).deployment_id, 7)

    def test_refuses_fallback_if_latest_release_id_conflicts(self) -> None:
        """Расхождение последней записи с immutable Release закрывает допуск."""
        api = FakeGitHub()
        self.second_deployment(api)
        api.deployments[0]["payload"]["release_id"] = 99
        with self.assertRaisesRegex(DeploymentAcceptanceError, "Release, CI или миграция"):
            resolve_accepted_baseline(api)

    def test_rejects_untrusted_authors_and_invalid_candidate_payload(self) -> None:
        """Доверие к записи включает авторов и точный commit её payload."""
        for change in ("deployment", "status", "payload", "unknown"):
            with self.subTest(change=change):
                api = FakeGitHub()
                self.second_deployment(api)
                if change == "deployment":
                    api.deployments[0]["creator"] = OWNER | {"login": "impostor"}
                elif change == "status":
                    api.statuses[0]["creator"] = OWNER | {"id": 1}
                elif change == "payload":
                    api.deployments[0]["payload"]["sha"] = "9" * 40
                else:
                    api.statuses[0]["state"] = "unknown"
                with self.assertRaises(DeploymentAcceptanceError):
                    resolve_accepted_baseline(api)

    def test_refuses_ambiguous_latest_and_duplicate_deployments(self) -> None:
        """Одинаковая дата приёмки и повтор записи не разрешаются порядком API."""
        api = FakeGitHub()
        self.second_deployment(api, time=api.statuses[0]["created_at"])
        with self.assertRaisesRegex(DeploymentAcceptanceError, "Неоднозначная последняя"):
            resolve_accepted_baseline(api)
        for field, duplicate in (("id", 7), ("sha", SHA)):
            with self.subTest(field=field):
                api = FakeGitHub()
                self.second_deployment(api)
                api.deployments[1][field] = duplicate
                if field == "sha":
                    api.deployments[1]["ref"] = duplicate
                with self.assertRaisesRegex(DeploymentAcceptanceError, "дубликаты"):
                    resolve_accepted_baseline(api)

    def test_fails_closed_if_acceptance_is_missing_or_api_read_is_incomplete(self) -> None:
        """Отсутствие базы и сбой пагинации завершают поиск явным отказом."""
        api = FakeGitHub()
        api.statuses = []
        with self.assertRaisesRegex(DeploymentAcceptanceError, "отсутствует"):
            resolve_accepted_baseline(api)
        api = FakeGitHub()
        self.second_deployment(api)
        api.fail_path = "/deployments/9/statuses"
        with self.assertRaises(ControllerError):
            resolve_accepted_baseline(api)

    def test_verifies_pinned_status_without_choosing_a_new_baseline(self) -> None:
        """Проверка сохранённой базы читает её запись и требует тот же current success."""
        api = FakeGitHub()
        self.second_deployment(api, time="2026-01-03T01:00:00Z")
        self.assertEqual(verify_accepted_baseline(api, 7, 8), AcceptedBaseline(SHA, VERSION, 41, 7, 8))
        self.assertFalse(any(path == "/deployments" for path, _ in api.calls))
        api.statuses.append(api.statuses[0] | {"id": 11, "created_at": "2026-01-04T01:00:00Z"})
        with self.assertRaisesRegex(DeploymentAcceptanceError, "статус выбранной базы изменился"):
            verify_accepted_baseline(api, 7, 8)
        api.statuses[1]["state"] = "failure"
        with self.assertRaisesRegex(DeploymentAcceptanceError, "статус базы не подтверждает"):
            verify_accepted_baseline(api, 7, 11)


if __name__ == "__main__":
    unittest.main()
