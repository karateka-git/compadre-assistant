"""Проверяет аварийную публикацию на поддельном GitHub API без записи в сеть."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import publish_emergency_release as emergency


SHA = "a" * 40
MERGE = "b" * 40
HEAD = "c" * 40
OLD = "d" * 40
MERGED_AT = "2026-10-16T08:00:00Z"


class FakeApi:
    """Возвращает воспроизводимые PR, CI и rule suite и записывает POST."""

    repository = emergency.EXPECTED_REPOSITORY

    def __init__(self) -> None:
        """Создаёт пустую историю с неуспешным быстрым и ожидающим полным CI."""
        self.releases: list[dict] = []
        self.tags: list[dict] = []
        self.posts: list[tuple[str, dict]] = []
        self.fast_conclusion = "failure"
        self.fast_completed = "2026-10-16T07:59:00Z"
        self.full_status = "in_progress"
        self.full_conclusion = None
        self.suite_result = "bypass"
        self.fail_api = False
        self.release_immutable = True
        self.owner_id = 42288695

    def authenticated_user(self):
        """Подтверждает владельца token для аварийного допуска."""
        if self.fail_api:
            raise emergency.PublishingError("GitHub /user недоступен")
        return {"id": self.owner_id, "login": "karateka-git"}

    def get(self, path, **parameters):
        """Имитирует читающие endpoints, включая подтверждение объектов."""
        if self.fail_api:
            raise emergency.ControllerError("GitHub API недоступен")
        if path == "/immutable-releases":
            return {"enabled": True}
        if path == "/rulesets/7":
            return {"target": "tag", "enforcement": "active", "bypass_actors": [],
                    "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
                    "rules": [{"type": "update"}, {"type": "deletion"}]}
        if path == "/git/ref/heads/main":
            return {"object": {"sha": SHA}}
        if path == "/pulls/11":
            return {"number": 11, "merged_at": MERGED_AT, "base": {"ref": "main"},
                    "head": {"sha": HEAD}, "merge_commit_sha": MERGE}
        if path == f"/git/commits/{MERGE}":
            return {"parents": [{"sha": OLD}, {"sha": HEAD}]}
        if path == f"/compare/{MERGE}...{SHA}":
            return {"status": "ahead", "base_commit": {"sha": MERGE}}
        if path == "/actions/workflows/pr-fast.yml":
            return {"id": 10, "path": ".github/workflows/pr-fast.yml"}
        if path == "/actions/workflows/main-full.yml":
            return {"id": 20, "path": ".github/workflows/main-full.yml"}
        if path == "/rulesets/rule-suites/31":
            return {"id": 31, "ref": "refs/heads/main", "after_sha": MERGE,
                    "result": self.suite_result, "actor_id": 42288695,
                    "rule_evaluations": [{"rule_source": {"id": 25067104},
                                          "rule_type": "required_status_checks", "result": "fail"}]}
        if path == "/git/ref/tags/v1.0.0":
            return {"ref": "refs/tags/v1.0.0", "object": {"type": "commit", "sha": SHA}}
        if path == "/releases/tags/v1.0.0":
            return {"id": 42, "tag_name": "v1.0.0", "target_commitish": SHA,
                    "body": self.posts[-1][1]["body"], "immutable": self.release_immutable,
                    "draft": False, "prerelease": False, "published_at": "2026-10-16T09:00:00Z"}
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Имитирует полные страницы GitHub API для одного кандидата."""
        if self.fail_api:
            raise emergency.ControllerError("GitHub API недоступен")
        if path == "/releases":
            return self.releases
        if path == "/git/matching-refs/tags/v":
            return self.tags
        if path == "/rulesets":
            return [{"id": 7, "target": "tag", "enforcement": "active"}]
        if path == "/actions/workflows/pr-fast.yml/runs":
            return [{"id": 101, "head_sha": "e" * 40, "workflow_id": 10,
                     "event": "pull_request", "repository": {"full_name": self.repository},
                     "pull_requests": [{"number": 11, "head": {"sha": HEAD},
                                        "base": {"ref": "main"}}],
                     "created_at": "2026-10-16T07:58:00Z",
                     "run_attempt": 1}]
        if path == "/actions/runs/101/attempts/1/jobs":
            return [{"name": "pr-fast", "started_at": "2026-10-16T07:58:10Z",
                     "completed_at": self.fast_completed, "conclusion": self.fast_conclusion}]
        if path == "/rulesets/rule-suites":
            return [{"id": 31, "ref": "refs/heads/main", "after_sha": MERGE,
                     "pushed_at": MERGED_AT, "result": self.suite_result}]
        if path == "/actions/workflows/main-full.yml/runs":
            return [{"id": 102, "head_sha": SHA, "head_branch": "main",
                     "workflow_id": 20, "event": "push",
                     "path": ".github/workflows/main-full.yml@main",
                     "repository": {"full_name": self.repository},
                     "created_at": "2026-10-16T08:01:00Z", "run_attempt": 1,
                     "status": self.full_status, "conclusion": self.full_conclusion}]
        raise AssertionError(path)

    def post(self, path, payload):
        """Создаёт фиктивный ref или Release после фиксации аргументов."""
        self.posts.append((path, payload))
        if path == "/git/refs":
            self.tags.append({"ref": payload["ref"]})
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        if path == "/releases":
            self.releases.append({"tag_name": payload["tag_name"]})
            return {"id": 42, "tag_name": payload["tag_name"], "draft": False,
                    "prerelease": False, "html_url": "https://github.com/example/release"}
        raise AssertionError(path)


class EmergencyPublishTests(unittest.TestCase):
    """Проверяет снимок на момент merge и fail-closed запись выпуска."""

    def setUp(self) -> None:
        """Изолирует локальный Git и родство кандидата."""
        self.api = FakeApi()
        for name, value in (("_candidate", None), ("checked_main_head", SHA),
                            ("tag_commit", SHA)):
            mocked = patch.object(emergency, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def prepared(self):
        """Готовит первый выпуск с одним подтверждённым PR."""
        return emergency.prepare(self.api, "v1.0.0", SHA, "initial",
                                 "срочное исправление", "Изменения", (11,))

    def test_records_failed_fast_pending_full_and_verified_bypass(self) -> None:
        """Манифест содержит причину, SHA, источник CI и rule suite."""
        result = self.prepared()
        self.assertEqual(result.pulls[0].fast.state, "failure")
        self.assertEqual(result.full.state, "pending")
        self.assertEqual(result.pulls[0].bypass, "yes")
        self.assertIn("rule suite 31", result.body)
        self.assertIn(f"Commit: `{SHA}`", result.body)
        self.assertIn("Причина: срочное исправление", result.body)
        self.assertEqual(self.api.posts, [])

    def test_fast_snapshot_uses_merge_time_not_later_result(self) -> None:
        """Успех после merge остаётся состоянием pending на момент merge."""
        self.api.fast_conclusion = "success"
        self.api.fast_completed = "2026-10-16T08:05:00Z"
        self.assertEqual(self.prepared().pulls[0].fast.state, "pending")

    def test_unverified_and_not_run_are_distinct(self) -> None:
        """Отсутствующий журнал обхода и run обозначаются разными статусами."""
        self.api.suite_result = "fail"
        self.assertEqual(self.prepared().pulls[0].bypass, "unverified")
        original = self.api.pages

        def no_runs(path, key=None, **parameters):
            if path.endswith("/runs"):
                return []
            return original(path, key=key, **parameters)

        with patch.object(self.api, "pages", side_effect=no_runs):
            result = self.prepared()
        self.assertEqual(result.pulls[0].fast.state, "not_run")
        self.assertEqual(result.full.state, "not_run")

    def test_full_success_needs_strict_selector_and_failure_is_recorded(self) -> None:
        """Успех полного run требует selector, а провал записывается отдельно."""
        self.api.full_status = "completed"
        self.api.full_conclusion = "failure"
        self.assertEqual(self.prepared().full.state, "failure")
        self.api.full_conclusion = "success"
        with patch.object(emergency, "select_successful_run", return_value=None):
            self.assertEqual(self.prepared().full.state, "unverified")
        with patch.object(emergency, "select_successful_run", return_value=object()) as selector:
            self.assertEqual(self.prepared().full.state, "success")
        selector.assert_called_with(self.api, self.api.repository, emergency.MAIN_WORKFLOW,
                                    SHA, "main", 102, 1)

    def test_validation_rejects_missing_reason_duplicate_pr_and_occupied_tag(self) -> None:
        """Обязательные реквизиты и одноразовый SemVer проверяются до записи."""
        with self.assertRaises(emergency.EmergencyPublishError):
            emergency.prepare(self.api, "v1.0.0", SHA, "initial", " ", "Изменения", (11,))
        with self.assertRaises(emergency.EmergencyPublishError):
            emergency.prepare(self.api, "v1.0.0", SHA, "initial", "срочно", "Изменения", (11, 11))
        self.api.tags.append({"ref": "refs/tags/v1.0.0"})
        with self.assertRaises(emergency.PublishError):
            self.prepared()
        self.assertEqual(self.api.posts, [])

    def test_api_failure_never_publishes(self) -> None:
        """Недоступный GitHub API завершает операцию до POST."""
        self.api.fail_api = True
        with self.assertRaises(emergency.PublishingError):
            self.prepared()
        self.assertEqual(self.api.posts, [])

    def test_other_user_cannot_prepare_emergency_release(self) -> None:
        """Чужой token не может выбрать аварийный маршрут."""
        self.api.owner_id = 7
        with self.assertRaisesRegex(emergency.EmergencyPublishError, "владелец"):
            self.prepared()
        self.assertEqual(self.api.posts, [])

    def test_patch_requires_exact_two_prs_and_uses_pair_history(self) -> None:
        """PATCH требует обе пары и сохраняет историю без release-full."""
        self.api.releases = [{"tag_name": "v1.0.0", "published_at": MERGED_AT}]
        with patch.object(emergency, "check_release", return_value=[(10, 11, "same")]) as pairs, \
             patch.object(emergency, "_pull", side_effect=lambda api, number, branch, sha, **kwargs:
                          {"number": number, "base": {"ref": branch}, "head": {"sha": HEAD},
                           "merged_at": MERGED_AT, "merge_commit_sha": MERGE}), \
             patch.object(emergency, "_fast_at_merge", return_value=emergency.CheckSnapshot("failure", "run")), \
             patch.object(emergency, "_bypass_at_merge", return_value=("yes", 31)), \
             patch.object(emergency, "_full_snapshot", return_value=emergency.CheckSnapshot("pending", "full")):
            with self.assertRaisesRegex(emergency.EmergencyPublishError, "точно совпадать"):
                emergency.prepare(self.api, "v1.0.1", SHA, "patch", "срочно", "Исправление", (10,))
            result = emergency.prepare(self.api, "v1.0.1", SHA, "patch", "срочно",
                                       "Исправление", (10, 11))
        pairs.assert_called_with(self.api, "release/1.0", SHA, SHA, require_full_ci=False)
        self.assertEqual(result.pairs, ((10, 11, "same"),))
        self.assertIn("PR #10 -> release/1.0", result.body)
        self.assertIn("PR #11 -> main", result.body)

    def test_patch_with_existing_main_commit_requires_only_release_pr(self) -> None:
        """Аварийный PATCH сохраняет основание main без выдуманного PR."""
        self.api.releases = [{"tag_name": "v1.0.0", "published_at": MERGED_AT}]
        with patch.object(emergency, "check_release", return_value=[(10, None, SHA)]), \
             patch.object(emergency, "_pull", return_value={
                 "number": 10, "base": {"ref": "release/1.0"}, "head": {"sha": HEAD},
                 "merged_at": MERGED_AT, "merge_commit_sha": MERGE}), \
             patch.object(emergency, "_fast_at_merge", return_value=emergency.CheckSnapshot("failure", "run")), \
             patch.object(emergency, "_bypass_at_merge", return_value=("yes", 31)), \
             patch.object(emergency, "_full_snapshot", return_value=emergency.CheckSnapshot("pending", "full")):
            result = emergency.prepare(self.api, "v1.0.1", SHA, "patch", "срочно",
                                       "Исправление", (10,))
        self.assertEqual(result.pairs, ((10, None, SHA),))
        self.assertIn(f"/commit/{SHA} подтверждён в main", result.body)

    def test_publish_confirms_tag_and_immutable_release(self) -> None:
        """Запись делает два POST и повторно сверяет точные данные."""
        prepared = self.prepared()
        self.assertEqual(emergency.publish(self.api, prepared), "https://github.com/example/release")
        self.assertEqual([path for path, _ in self.api.posts], ["/git/refs", "/releases"])
        self.assertEqual(self.api.posts[0][1]["sha"], SHA)

    def test_changed_snapshot_stops_before_tag_and_bad_immutable_after_release(self) -> None:
        """Изменение CI закрывает запись, а плохой read-back не подтверждает Release."""
        prepared = self.prepared()
        self.api.fast_conclusion = "success"
        with self.assertRaisesRegex(emergency.EmergencyPublishError, "изменился"):
            emergency.publish(self.api, prepared)
        self.assertEqual(self.api.posts, [])
        self.api.fast_conclusion = "failure"
        self.api.release_immutable = False
        with self.assertRaisesRegex(emergency.EmergencyPublishError, "не подтверждён"):
            emergency.publish(self.api, prepared)


if __name__ == "__main__":
    unittest.main()
