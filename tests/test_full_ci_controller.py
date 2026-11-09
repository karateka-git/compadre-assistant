"""Проверяет выбор полного CI без сетевых запросов и GitHub Actions."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import ANY, patch

from scripts import check_full_migrations
from scripts import full_ci_controller as controller
from scripts import full_ci_admission as admission


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def admission_run(sha: str) -> dict:
    """Создаёт успешный run полного caller на точном SHA."""
    repository = {"full_name": "owner/repo"}
    return {
        "id": 1, "run_attempt": 1, "head_sha": sha, "head_branch": "main",
        "repository": repository, "head_repository": repository,
        "workflow_id": 1, "path": ".github/workflows/main-full.yml@main",
        "event": "push", "status": "completed", "conclusion": "success",
    }


def admission_jobs(sha: str) -> list[dict]:
    """Создаёт обязательные успешные jobs и steps полного caller."""
    def steps(names):
        return [{"name": name, "status": "completed", "conclusion": "success"} for name in names]

    return [
        {"name": "main-full-controller", "head_sha": sha, "status": "completed",
         "conclusion": "success", "steps": steps(("Checkout exact main commit", "Decide full run"))},
        {"name": "main-full / full-suite", "head_sha": sha, "status": "completed",
         "conclusion": "success", "steps": steps(admission.FULL_STEPS)},
        {"name": "main-full / repository-compatibility", "head_sha": sha, "status": "completed",
         "conclusion": "success", "steps": steps(admission.COMPATIBILITY_STEPS)},
    ]


class FakeGitHub:
    """Отдаёт предсказуемые ответы для сценариев контроллера."""

    def __init__(self, releases=None, runs=None, jobs=None, prs=None, policy=None,
                 release=None, reference=None, content=None, tag_object=None) -> None:
        """Сохраняет наборы ответов REST API для теста."""
        self.releases = releases or []
        self.runs = runs or {}
        self.jobs = jobs or {}
        self.prs = prs or {}
        self.repository = "owner/repo"
        self.policy = policy if policy is not None else {
            "version": 1, "repository": self.repository, "previous_sha": SHA_B,
            "release_tag": "v3.1.0", "release_id": 10,
        }
        self.release = release
        self.reference = reference
        self.tag_object = tag_object
        self.content = content
        self.contents_refs = []

    def pages(self, path, key=None, **parameters):
        """Возвращает одну подготовленную страницу API."""
        if path == "/releases":
            return self.releases
        if path.endswith("/runs"):
            return self.runs.get((path, parameters.get("head_sha"), parameters.get("status")), [])
        if path.endswith("/jobs"):
            return self.jobs.get(path, [])
        if path.endswith("/pulls"):
            return self.prs.get(path, [])
        raise AssertionError(path)

    def get(self, path, **parameters):
        """Возвращает Git ref для текущего SHA или опубликованного тега."""
        if path == "/actions/workflows/main-full.yml":
            return {"id": 1, "path": ".github/workflows/main-full.yml"}
        if path == "/actions/workflows/release-full.yml":
            return {"id": 2, "path": ".github/workflows/release-full.yml"}
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": SHA_D}}
        if path.startswith("/git/ref/tags/"):
            return self.reference if self.reference is not None else {
                "object": {"type": "commit", "sha": SHA_B}}
        if path.startswith("/git/tags/"):
            return self.tag_object
        if path == "/contents/scripts/repository_compatibility_policy.json":
            self.contents_refs.append(parameters.get("ref"))
            return self.content if self.content is not None else {"encoding": "base64", "content": base64.b64encode(
                json.dumps(self.policy).encode()).decode()}
        if path.startswith("/releases/tags/"):
            return self.release if self.release is not None else {
                "id": 10, "tag_name": "v3.1.0", "immutable": True,
                "draft": False, "prerelease": False, "published_at": "2026-11-02T00:00:00Z"}
        raise AssertionError(path)


class FullCiControllerTests(unittest.TestCase):
    """Проверяет границы счётчика и идентичность полного job."""

    def test_light_controller_success_does_not_count_as_full_success(self) -> None:
        """Run без полного job не сбрасывает счётчик PR."""
        run = admission_run(SHA_D)
        api = FakeGitHub(runs={("/actions/workflows/main-full.yml/runs", SHA_D, None): [run]})
        self.assertFalse(controller.full_success(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    def test_full_success_requires_every_reusable_step(self) -> None:
        """Успех полного job требует всех обязательных шагов."""
        run = admission_run(SHA_D)
        jobs = admission_jobs(SHA_D)
        api = FakeGitHub(
            runs={("/actions/workflows/main-full.yml/runs", SHA_D, None): [run]},
            jobs={"/actions/runs/1/attempts/1/jobs": jobs},
        )
        self.assertTrue(controller.full_success(api, controller.MAIN_WORKFLOW, SHA_D, "main"))
        jobs[1]["steps"][0]["conclusion"] = "skipped"
        self.assertFalse(controller.full_success(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    def test_skipped_suite_does_not_block_manual_dispatch(self) -> None:
        """Предыдущий лёгкий run не считается попыткой полного прогона."""
        run = {"id": 1, "head_sha": SHA_D, "head_branch": "main", "status": "completed"}
        api = FakeGitHub(
            runs={("/actions/workflows/main-full.yml/runs", SHA_D, None): [run]},
            jobs={"/actions/runs/1/jobs": [{
                "name": "main-full / full-suite", "conclusion": "skipped", "steps": [],
            }]},
        )
        self.assertFalse(controller.prior_full_attempt(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    def test_queued_full_attempt_blocks_parallel_dispatch(self) -> None:
        """Ожидающая попытка без steps требует дождаться результата или rerun."""
        run = {"id": 1, "head_sha": SHA_D, "head_branch": "main", "status": "queued"}
        api = FakeGitHub(runs={("/actions/workflows/main-full.yml/runs", SHA_D, None): [run]})
        self.assertTrue(controller.prior_full_attempt(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    def test_failed_compatibility_requires_rerun_even_when_suite_was_skipped(self) -> None:
        """Отказ обязательного dependency job уже является полной попыткой."""
        api = FakeGitHub(
            runs={("/actions/workflows/main-full.yml/runs", SHA_D, None): [
                {"id": 1, "head_sha": SHA_D, "head_branch": "main", "status": "completed"}]},
            jobs={"/actions/runs/1/jobs": [
                {"name": "main-full / full-suite", "conclusion": "skipped"},
                {"name": "main-full / repository-compatibility", "conclusion": "failure"}]},
        )
        self.assertTrue(controller.prior_full_attempt(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    def test_failed_full_job_without_steps_blocks_new_dispatch(self) -> None:
        """Неуспешный полный job без steps остаётся прежней попыткой."""
        run = {"id": 1, "head_sha": SHA_D, "head_branch": "main", "status": "completed"}
        api = FakeGitHub(
            runs={("/actions/workflows/main-full.yml/runs", SHA_D, None): [run]},
            jobs={"/actions/runs/1/jobs": [{"name": "main-full / full-suite", "conclusion": "cancelled"}]},
        )
        self.assertTrue(controller.prior_full_attempt(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    @patch.object(controller, "activation_sha", return_value=SHA_A)
    @patch.object(controller, "git")
    def test_count_only_merged_pr_on_first_parent(self, git, _anchor) -> None:
        """Побочный merge без слитого PR не приближает порог."""
        def git_result(*args, **kwargs):
            if args[:2] == ("rev-list", "--first-parent"):
                return "\n".join([SHA_D, SHA_C, SHA_B, SHA_A])
            if args[:2] == ("rev-list", "--parents"):
                return f"{args[-1]} {SHA_A} {SHA_B}"
            raise AssertionError(args)
        git.side_effect = git_result
        prs = {
            f"/commits/{SHA_D}/pulls": [{"merged_at": "date", "merge_commit_sha": SHA_D, "base": {"ref": "main"}}],
            f"/commits/{SHA_C}/pulls": [],
            f"/commits/{SHA_B}/pulls": [{"merged_at": "date", "merge_commit_sha": SHA_B, "base": {"ref": "main"}}],
        }
        self.assertEqual(controller.merged_since_last_success(FakeGitHub(prs=prs), SHA_D), (2, SHA_A))

    @patch.object(controller, "activation_sha", return_value=SHA_A)
    @patch.object(controller, "git")
    def test_third_merge_reaches_threshold(self, git, _anchor) -> None:
        """Третий подтверждённый PR вызывает полный прогон."""
        def git_result(*args, **kwargs):
            if args[:2] == ("rev-list", "--first-parent"):
                return "\n".join([SHA_D, SHA_C, SHA_B, SHA_A])
            return f"{args[-1]} {SHA_A} {SHA_B}"
        git.side_effect = git_result
        prs = {f"/commits/{sha}/pulls": [{
            "merged_at": "date", "merge_commit_sha": sha, "base": {"ref": "main"},
        }] for sha in (SHA_D, SHA_C, SHA_B)}
        self.assertEqual(controller.merged_since_last_success(FakeGitHub(prs=prs), SHA_D), (3, SHA_A))

    @patch.dict("os.environ", {"COMPATIBILITY_POLICY_SHA": SHA_A})
    def test_published_but_not_installed_uses_pinned_baseline_in_both_lines(self) -> None:
        """Публикация v3.2.0 не меняет базу установленной v3.1.0."""
        api = FakeGitHub(releases=[{
            "tag_name": "v3.2.0", "draft": False, "prerelease": False,
            "published_at": "2026-11-06T00:00:00Z",
        }])
        self.assertEqual(controller.previous_release_sha(api, "main"), SHA_B)
        self.assertEqual(controller.previous_release_sha(api, "release/3.3"), SHA_B)
        self.assertEqual(api.contents_refs, [SHA_A, SHA_A])

    @patch.dict("os.environ", {"COMPATIBILITY_POLICY_SHA": SHA_A,
                            "GITHUB_EVENT_NAME": "workflow_dispatch"})
    @patch.object(controller, "git", return_value=SHA_D)
    @patch.object(controller, "merged_since_last_success", return_value=(3, SHA_A))
    @patch.object(controller, "prior_full_attempt", return_value=False)
    @patch.object(controller, "full_success", return_value=False)
    def test_decide_uses_pinned_previous_for_main_and_release(
        self, _success, _attempt, _merged, _git,
    ) -> None:
        """Оба caller передают full-suite базу внешней policy."""
        api = FakeGitHub(releases=[{"tag_name": "v3.2.0"}])
        for mode, branch in (("main", "main"), ("release", "release/3.3")):
            with self.subTest(mode=mode):
                self.assertEqual(controller.decide(api, mode, SHA_D, branch, False),
                                 {"run-full": "true", "previous-release-sha": SHA_B})

    @patch.dict("os.environ", {"COMPATIBILITY_POLICY_SHA": SHA_A, "GITHUB_EVENT_NAME": "workflow_dispatch"})
    @patch.object(controller, "git", return_value=SHA_D)
    @patch.object(controller, "full_success", side_effect=AssertionError("ротация не ищет старый CI"))
    def test_rotation_forces_full_with_exact_new_pin_and_main(self, _old, _git) -> None:
        """При ротации прежняя зелёная попытка не подменяет запуск с новым pin."""
        api = FakeGitHub()
        self.assertEqual(controller.decide(api, "main", SHA_D, "main", False,
                                          rotation_policy_sha=SHA_A, rotation_main_sha=SHA_D),
                         {"run-full": "true", "previous-release-sha": SHA_B})
        self.assertEqual(api.contents_refs, [SHA_A])
        for mode, branch, pin, main in (("main", "main", SHA_C, SHA_D), ("main", "main", SHA_A, SHA_C),
                                        ("main", "main", "", SHA_D), ("release", "release/3.2", SHA_A, SHA_D)):
            with self.subTest(mode=mode, pin=pin, main=main), self.assertRaises(controller.ControllerError):
                controller.decide(api, mode, SHA_D, branch, False, rotation_policy_sha=pin, rotation_main_sha=main)
        with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "push"}), self.assertRaises(controller.ControllerError):
            controller.decide(api, "main", SHA_D, "main", False,
                              rotation_policy_sha=SHA_A, rotation_main_sha=SHA_D)
        self.assertEqual(api.contents_refs, [SHA_A])

    def test_missing_or_invalid_external_pin_fails_closed(self) -> None:
        """Без внешнего полного SHA кандидат не может выбрать свою policy."""
        for pin in ("", "bad", SHA_A.upper()):
            with self.subTest(pin=pin), patch.dict("os.environ", {"COMPATIBILITY_POLICY_SHA": pin}):
                with self.assertRaisesRegex(controller.ControllerError, "внешний SHA"):
                    controller.previous_release_sha(FakeGitHub(), "main")

    @patch.dict("os.environ", {"COMPATIBILITY_POLICY_SHA": SHA_A})
    def test_wrong_repository_release_or_tag_fails_closed(self) -> None:
        """Доверенный pin не разрешает чужую базу и mutable Release/tag."""
        for change in ({"repository": "other/repo"}, {"release_id": 11},
                       {"release_tag": "v3.2.0"}, {"previous_sha": SHA_C}):
            with self.subTest(change=change):
                api = FakeGitHub(policy={**FakeGitHub().policy, **change})
                with self.assertRaises((controller.ControllerError, ValueError)):
                    controller.previous_release_sha(api, "main")
        for api in (FakeGitHub(release={**FakeGitHub().get("/releases/tags/v3.1.0"),
                                        "immutable": False}),
                    FakeGitHub(reference={"object": {"type": "tag", "sha": SHA_B}})):
            with self.assertRaises(ValueError):
                controller.previous_release_sha(api, "release/3.3")

    @patch.dict("os.environ", {"COMPATIBILITY_POLICY_SHA": SHA_A})
    def test_ci_accepts_annotated_production_baseline(self) -> None:
        """После ротации CI использует commit аннотированного immutable тега."""
        api = FakeGitHub(
            reference={"object": {"type": "tag", "sha": SHA_C}},
            tag_object={"sha": SHA_C, "tag": "v3.1.0", "message": "evidence",
                        "object": {"type": "commit", "sha": SHA_B}},
        )
        self.assertEqual(controller.previous_release_sha(api, "release/3.3"), SHA_B)

    @patch.dict("os.environ", {"COMPATIBILITY_POLICY_SHA": SHA_A})
    def test_corrupt_external_policy_bytes_fail_closed(self) -> None:
        """Пустой или повреждённый ответ Contents не заменяется локальным JSON."""
        for content in ({"encoding": "none", "content": ""},
                        {"encoding": "base64", "content": "%%%"}):
            with self.subTest(content=content):
                with self.assertRaises(controller.ControllerError):
                    controller.previous_release_sha(FakeGitHub(content=content), "main")

    def test_sibling_schema_prefix_mismatch_fails_closed(self) -> None:
        """Несовпадающие имена и SQL соседней линии останавливают набор."""
        with TemporaryDirectory() as temporary:
            migrations = Path(temporary)
            (migrations / "0001_initial_schema.sql").write_bytes(b"CREATE TABLE fresh (id INTEGER);")
            with (patch.object(check_full_migrations, "MIGRATIONS_DIR", migrations),
                  patch.object(check_full_migrations, "previous_migrations", return_value={
                      "0001_other_schema.sql": b"CREATE TABLE old (id INTEGER);",
                  })):
                with self.assertRaisesRegex(ValueError, "not a prefix"):
                    check_full_migrations.check_migrations(SHA_B)
            with (patch.object(check_full_migrations, "MIGRATIONS_DIR", migrations),
                  patch.object(check_full_migrations, "previous_migrations", return_value={
                      "0001_initial_schema.sql": b"CREATE TABLE old (id INTEGER);",
                  })):
                with self.assertRaisesRegex(ValueError, "previous migration changed"):
                    check_full_migrations.check_migrations(SHA_B)

    @patch.object(controller, "git", return_value=SHA_D)
    @patch.object(controller, "full_success", return_value=True)
    def test_release_creation_reuses_exact_main_success(self, success, _git) -> None:
        """Создание release от проверенного main SHA пропускает полный job."""
        result = controller.decide(FakeGitHub(), "release", SHA_D, "release/1.0", True)
        self.assertEqual(result["run-full"], "false")
        success.assert_called_once_with(ANY, controller.MAIN_WORKFLOW, SHA_D, "main")

    @patch.object(controller, "git", return_value=SHA_D)
    @patch.object(controller, "full_success", return_value=False)
    def test_release_creation_without_main_success_fails(self, _success, _git) -> None:
        """Создание release от непроверенного SHA не даёт допуска."""
        with self.assertRaises(controller.ControllerError):
            controller.decide(FakeGitHub(), "release", SHA_D, "release/1.0", True)

    @patch.object(controller, "git", return_value=SHA_D)
    @patch.object(controller, "previous_release_sha", return_value=SHA_A)
    @patch.object(controller, "prior_full_attempt", return_value=False)
    @patch.object(controller, "full_success", return_value=False)
    def test_changed_release_does_not_query_main_workflow(
        self, success, _attempt, _previous, _git,
    ) -> None:
        """PR merge release запускает suite даже до регистрации main-full."""
        result = controller.decide(FakeGitHub(), "release", SHA_D, "release/0.0", False)
        self.assertEqual(result, {"run-full": "true", "previous-release-sha": SHA_A})
        success.assert_called_once_with(ANY, controller.RELEASE_WORKFLOW, SHA_D, "release/0.0")

    @patch.object(controller, "git", return_value=SHA_C)
    def test_stale_checkout_is_rejected_before_api(self, _git) -> None:
        """Устаревший или подменённый checkout не запускает полный набор."""
        with self.assertRaises(controller.ControllerError):
            controller.decide(FakeGitHub(), "main", SHA_D, "main", False)

    @patch.object(controller, "git", return_value=SHA_D)
    def test_moved_ref_rejects_old_run(self, _git) -> None:
        """После смены tip run прежнего SHA прекращается на controller."""
        class MovedRef(FakeGitHub):
            """Возвращает новый SHA для ветки."""

            def get(self, path, **parameters):
                """Имитирует новое состояние ref GitHub."""
                if path.startswith("/git/ref/heads/"):
                    return {"object": {"sha": SHA_C}}
                return super().get(path, **parameters)

        with self.assertRaisesRegex(controller.ControllerError, "устаревший run"):
            controller.decide(MovedRef(), "main", SHA_D, "main", False)


if __name__ == "__main__":
    unittest.main()
