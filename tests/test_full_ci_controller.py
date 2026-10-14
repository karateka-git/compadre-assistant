"""Проверяет выбор полного CI без сетевых запросов и GitHub Actions."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import ANY, patch

from scripts import check_full_migrations
from scripts import full_ci_controller as controller


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


class FakeGitHub:
    """Отдаёт предсказуемые ответы для сценариев контроллера."""

    def __init__(self, releases=None, runs=None, jobs=None, prs=None) -> None:
        """Сохраняет наборы ответов REST API для теста."""
        self.releases = releases or []
        self.runs = runs or {}
        self.jobs = jobs or {}
        self.prs = prs or {}

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
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": SHA_D}}
        if path.startswith("/git/ref/tags/"):
            return {"object": {"type": "commit", "sha": SHA_B}}
        raise AssertionError(path)


class FullCiControllerTests(unittest.TestCase):
    """Проверяет границы счётчика и идентичность полного job."""

    def test_light_controller_success_does_not_count_as_full_success(self) -> None:
        """Run без полного job не сбрасывает счётчик PR."""
        run = {"id": 1, "head_sha": SHA_D, "head_branch": "main", "conclusion": "success"}
        api = FakeGitHub(runs={("/actions/workflows/main-full.yml/runs", SHA_D, "success"): [run]})
        self.assertFalse(controller.full_success(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    def test_full_success_requires_every_reusable_step(self) -> None:
        """Успех полного job требует всех обязательных шагов."""
        run = {"id": 1, "head_sha": SHA_D, "head_branch": "main", "conclusion": "success"}
        jobs = [{"name": "main-full / full-suite", "head_sha": SHA_D,
                 "conclusion": "success", "steps": [
                     {"name": name, "conclusion": "success"}
                     for name in controller.REQUIRED_STEPS
                 ]}]
        api = FakeGitHub(
            runs={("/actions/workflows/main-full.yml/runs", SHA_D, "success"): [run]},
            jobs={"/actions/runs/1/jobs": jobs},
        )
        self.assertTrue(controller.full_success(api, controller.MAIN_WORKFLOW, SHA_D, "main"))
        jobs[0]["steps"][0]["conclusion"] = "skipped"
        self.assertFalse(controller.full_success(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

    def test_skipped_suite_does_not_block_manual_dispatch(self) -> None:
        """Предыдущий лёгкий run не считается попыткой полного прогона."""
        run = {"id": 1, "head_sha": SHA_D, "head_branch": "main"}
        api = FakeGitHub(
            runs={("/actions/workflows/main-full.yml/runs", SHA_D, None): [run]},
            jobs={"/actions/runs/1/jobs": [{
                "name": "main-full / full-suite", "conclusion": "skipped", "steps": [],
            }]},
        )
        self.assertFalse(controller.prior_full_attempt(api, controller.MAIN_WORKFLOW, SHA_D, "main"))

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

    @patch.object(controller, "is_ancestor", side_effect=AssertionError("ancestry is irrelevant"))
    def test_sibling_release_is_valid_migration_base(self, _ancestor) -> None:
        """Опубликованный PATCH из соседней линии выбирается по тегу."""
        api = FakeGitHub(releases=[{
            "tag_name": "v1.0.1", "draft": False, "prerelease": False,
            "published_at": "2026-10-14T00:00:00Z",
        }])
        self.assertEqual(controller.previous_release_sha(api, SHA_D, "main"), SHA_B)

    @patch.object(controller, "is_ancestor", side_effect=AssertionError("ancestry is irrelevant"))
    def test_new_release_line_uses_latest_global_publication(self, _ancestor) -> None:
        """Новая линия сравнивается с последним опубликованным выпуском."""
        api = FakeGitHub(releases=[
            {"tag_name": "v1.1.0", "draft": False, "prerelease": False,
             "published_at": "2026-10-13T00:00:00Z"},
            {"tag_name": "v1.0.2", "draft": False, "prerelease": False,
             "published_at": "2026-10-14T00:00:00Z"},
        ])
        with patch.object(controller, "tag_commit", return_value=SHA_B) as tag:
            self.assertEqual(controller.previous_release_sha(api, SHA_D, "release/2.0"), SHA_B)
        tag.assert_called_once_with(api, "v1.0.2")

    @patch.object(controller, "is_ancestor", return_value=True)
    def test_latest_published_backport_takes_precedence(self, _ancestor) -> None:
        """Поздний PATCH старой линии не скрывается большей SemVer-версией."""
        api = FakeGitHub(releases=[
            {"tag_name": "v2.0.0", "draft": False, "prerelease": False,
             "published_at": "2026-10-13T00:00:00Z"},
            {"tag_name": "v1.0.2", "draft": False, "prerelease": False,
             "published_at": "2026-10-14T00:00:00Z"},
        ])
        with patch.object(controller, "tag_commit", return_value=SHA_B) as tag:
            self.assertEqual(controller.previous_release_sha(api, SHA_D, "main"), SHA_B)
        tag.assert_called_once_with(api, "v1.0.2")

    @patch.object(controller, "is_ancestor", return_value=True)
    def test_bootstrap_used_before_first_published_release(self, _ancestor) -> None:
        """До v1.0.0 используется зафиксированный production commit."""
        self.assertEqual(
            controller.previous_release_sha(FakeGitHub(), SHA_D, "main"),
            controller.BOOTSTRAP_PRODUCTION_SHA,
        )

    @patch.object(controller, "is_ancestor", return_value=False)
    def test_bootstrap_still_requires_ancestry(self, _ancestor) -> None:
        """До первого выпуска прежняя production точка должна быть предком."""
        with self.assertRaises(controller.ControllerError):
            controller.previous_release_sha(FakeGitHub(), SHA_D, "main")

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


if __name__ == "__main__":
    unittest.main()
