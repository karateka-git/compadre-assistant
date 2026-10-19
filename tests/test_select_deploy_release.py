"""Проверяет отказоустойчивый выбор точного выпуска без GitHub и VPS."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import unittest
from unittest.mock import patch

from scripts import select_deploy_release as selector
from tests.test_full_ci_controller import admission_jobs, admission_run


SHA = "a" * 40
NEW_HEAD = "b" * 40
REPO = selector.EXPECTED_REPOSITORY


class FakeApi:
    """Возвращает изменяемые ответы GitHub API для одной версии."""

    repository = REPO

    def __init__(self, version: str = "v1.0.0") -> None:
        """Готовит опубликованный выпуск и закреплённый успешный CI."""
        self.version = version
        major, minor, patch = selector._version(version)
        self.branch = f"release/{major}.{minor}"
        self.workflow = selector.MAIN_WORKFLOW if patch == 0 else selector.RELEASE_WORKFLOW
        self.ci_branch = "main" if patch == 0 else self.branch
        self.head = SHA
        self.release = {
            "id": 12, "tag_name": version, "name": version, "draft": False,
            "prerelease": False, "immutable": True,
            "published_at": "2026-10-15T00:00:00Z",
            "target_commitish": SHA,
            "body": (f"Изменения\n\nТип выпуска: обычный\nCommit: `{SHA}`\n"
                     f"Ветка: `{self.branch}`\nПолный CI: "
                     f"https://github.com/{REPO}/actions/runs/1/attempts/1\n"),
        }
        self.tag = {"ref": f"refs/tags/{version}", "object": {"type": "commit", "sha": SHA}}
        self.run = admission_run(SHA)
        self.jobs = admission_jobs(SHA)
        self.run["repository"] = {"full_name": REPO}
        self.run["head_repository"] = {"full_name": REPO}
        self.run["head_branch"] = self.ci_branch
        self.run["path"] = f".github/workflows/{self.workflow}"
        if patch:
            self.run["workflow_id"] = 2
            self.jobs[0]["name"] = "release-full-controller"
            self.jobs[0]["steps"][0]["name"] = "Checkout exact release commit"
            self.jobs[1]["name"] = "release-full / full-suite"
        self.rules = [{"id": 7, "target": "tag", "enforcement": "active"}]
        self.rule = {
            "target": "tag", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "update"}, {"type": "deletion"}],
        }
        self.error = None
        self.move_on_recheck = False
        self.release_reads = 0
        self.compare_calls = 0
        self.compare_response = None
        self.compare_error = None

    def get(self, path, **parameters):
        """Читает один объект или имитирует отказ API."""
        if self.error:
            raise self.error
        if path == f"/releases/tags/{self.version}":
            self.release_reads += 1
            if self.move_on_recheck and self.release_reads > 1:
                return dict(self.release, body="Изменено")
            return deepcopy(self.release)
        if path == f"/git/ref/tags/{self.version}":
            return deepcopy(self.tag)
        if path == f"/git/ref/heads/{self.branch}":
            return {"ref": f"refs/heads/{self.branch}", "object": {"type": "commit", "sha": self.head}}
        if path == f"/compare/{SHA}...{self.head}":
            self.compare_calls += 1
            if self.compare_error:
                raise self.compare_error
            if self.compare_response is not None:
                return deepcopy(self.compare_response)
            return {"status": "identical" if self.head == SHA else "ahead",
                    "base_commit": {"sha": SHA}, "merge_base_commit": {"sha": SHA}}
        if path == "/rulesets/7":
            return deepcopy(self.rule)
        if path == f"/actions/workflows/{self.workflow}":
            return {"id": 1 if self.workflow == selector.MAIN_WORKFLOW else 2,
                    "path": f".github/workflows/{self.workflow}"}
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Читает список rulesets, run или jobs текущей попытки."""
        if self.error:
            raise self.error
        if path == "/rulesets":
            return deepcopy(self.rules)
        if path == f"/actions/workflows/{self.workflow}/runs":
            return [deepcopy(self.run)]
        if path == "/actions/runs/1/attempts/1/jobs":
            return deepcopy(self.jobs)
        raise AssertionError(path)


class SelectDeployReleaseTests(unittest.TestCase):
    """Проверяет репозиторий, tag, release, ветку и CI до переключения."""

    def setUp(self) -> None:
        """Подменяет только локальное имя origin."""
        self.origin = patch.object(selector, "repository_from_origin", return_value=REPO)
        self.origin.start()
        self.addCleanup(self.origin.stop)
        self.api = FakeApi()

    def select(self):
        """Выбирает подготовленную версию."""
        return selector.select_release(self.api, self.api.version)

    def test_initial_and_patch_select_pinned_ci(self) -> None:
        """Начальная версия использует main-full, PATCH — release-full."""
        selected = self.select()
        self.assertEqual((selected.sha, selected.workflow, selected.run_id, selected.run_attempt),
                         (SHA, selector.MAIN_WORKFLOW, 1, 1))
        self.api = FakeApi("v1.0.1")
        self.assertEqual(self.select().workflow, selector.RELEASE_WORKFLOW)

    def test_old_release_may_be_ancestor_of_new_branch_tip(self) -> None:
        """Продвижение ветки после публикации не отменяет предыдущий выпуск."""
        self.api.head = NEW_HEAD
        self.assertEqual(self.select().sha, SHA)
        self.assertEqual(self.api.compare_calls, 1)

    def test_equal_tag_and_branch_skip_compare(self) -> None:
        """Два независимо проверенных одинаковых SHA не требуют ответа compare."""
        self.api.compare_response = {"status": "identical", "base_commit": {"sha": SHA}}
        self.api.compare_error = selector.ControllerError("compare недоступен")
        self.assertEqual(self.select().sha, SHA)
        self.assertEqual(self.api.compare_calls, 0)

    def test_different_sha_requires_exact_ahead_compare(self) -> None:
        """Для старого тега нужны ahead, base и merge base исходного SHA."""
        bad_responses = (
            {"status": "behind", "base_commit": {"sha": SHA},
             "merge_base_commit": {"sha": SHA}},
            {"status": "diverged", "base_commit": {"sha": SHA},
             "merge_base_commit": {"sha": SHA}},
            {"status": "identical", "base_commit": {"sha": SHA}},
            {"status": "ahead", "base_commit": {"sha": SHA}},
            {"status": "ahead", "base_commit": {"sha": NEW_HEAD},
             "merge_base_commit": {"sha": SHA}},
            {"status": "ahead", "base_commit": {"sha": SHA},
             "merge_base_commit": {"sha": NEW_HEAD}},
            ["ahead"],
        )
        for response in bad_responses:
            with self.subTest(response=response):
                api = FakeApi()
                api.head = NEW_HEAD
                api.compare_response = response
                with self.assertRaises(selector.DeploySelectionError):
                    selector.select_release(api, api.version)
                self.assertEqual(api.compare_calls, 1)

    def test_different_sha_compare_api_failure_propagates(self) -> None:
        """Ошибка compare прерывает выбор выпуска без повторного вызова."""
        self.api.head = NEW_HEAD
        self.api.compare_error = selector.ControllerError("compare недоступен")
        with self.assertRaises(selector.ControllerError):
            self.select()
        self.assertEqual(self.api.compare_calls, 1)

    def test_wrong_repository_or_version_is_denied(self) -> None:
        """Чужой API, origin и неканоническая версия не допускаются."""
        self.api.repository = "other/repo"
        with self.assertRaises(selector.DeploySelectionError):
            self.select()
        self.api.repository = REPO
        with patch.object(selector, "repository_from_origin", return_value="other/repo"):
            with self.assertRaises(selector.DeploySelectionError):
                self.select()
        with self.assertRaises(selector.PublishError):
            selector.select_release(self.api, "v01.0.0")

    def test_release_marker_publication_and_identity_are_required(self) -> None:
        """Черновик, emergency и несовпадение тега не проходят."""
        for field, value in (("draft", True), ("prerelease", True), ("name", "other"),
                             ("published_at", None), ("immutable", False),
                             ("immutable", None), ("target_commitish", NEW_HEAD)):
            with self.subTest(field=field):
                api = FakeApi()
                api.release[field] = value
                with self.assertRaises(selector.DeploySelectionError):
                    selector.select_release(api, api.version)
        for body in ("Аварийный выпуск", self.api.release["body"].replace("обычный", "аварийный"),
                     self.api.release["body"] + "\nТип выпуска: обычный\n"):
            with self.subTest(body=body):
                api = FakeApi()
                api.release["body"] = body
                with self.assertRaises(selector.DeploySelectionError):
                    selector.select_release(api, api.version)

    def test_tag_must_be_direct_and_protected(self) -> None:
        """Annotated tag, изменение tag и отсутствие защиты дают отказ."""
        for changed in (dict(type="tag", sha=SHA), dict(type="commit", sha=NEW_HEAD)):
            api = FakeApi()
            api.tag["object"] = changed
            with self.assertRaises(selector.DeploySelectionError):
                selector.select_release(api, api.version)
        self.api.rule["bypass_actors"] = [{"actor_type": "RepositoryRole"}]
        with self.assertRaises(selector.PublishError):
            self.select()
        self.api.rule.pop("bypass_actors")
        self.assertEqual(self.select().sha, SHA)

    def test_branch_and_manifest_must_match(self) -> None:
        """Чужая ветка, неполный SHA и неверный CI URL закрывают допуск."""
        self.api.release["body"] = self.api.release["body"].replace("release/1.0", "release/1.1")
        with self.assertRaises(selector.DeploySelectionError):
            self.select()
        self.api = FakeApi()
        self.api.release["body"] = self.api.release["body"].replace(f"`{SHA}`", "`aaaa`")
        with self.assertRaises(selector.DeploySelectionError):
            self.select()
        self.api = FakeApi()
        self.api.release["body"] = self.api.release["body"].replace("/attempts/1", "/attempts/2")
        with self.assertRaises(selector.DeploySelectionError):
            self.select()

    def test_ci_must_match_pinned_run_and_all_jobs(self) -> None:
        """Другой run, частичный job или ожидание CI не подходят."""
        self.api.run["id"] = 2
        with self.assertRaises(selector.DeploySelectionError):
            self.select()
        self.api = FakeApi()
        self.api.jobs[1]["conclusion"] = "skipped"
        with self.assertRaises(selector.DeploySelectionError):
            self.select()
        self.api = FakeApi()
        self.api.run["status"] = "in_progress"
        with self.assertRaises(selector.DeploySelectionError):
            self.select()

    def test_api_error_and_race_fail_closed(self) -> None:
        """Недоступный API и правка Release во время чтения прерывают выбор."""
        self.api.error = selector.ControllerError("API недоступен")
        with self.assertRaises(selector.ControllerError):
            self.select()
        self.api = FakeApi()
        self.api.move_on_recheck = True
        with self.assertRaises(selector.DeploySelectionError):
            self.select()

    def test_cli_outputs_one_json_manifest(self) -> None:
        """Успешный CLI выводит только пригодный для парсинга JSON."""
        output = StringIO()
        with patch.object(selector, "GitHub", return_value=self.api), \
             patch.object(selector.sys, "argv", ["select_deploy_release", "v1.0.0"]), \
             redirect_stdout(output):
            selector.main()
        manifest = json.loads(output.getvalue())
        self.assertEqual((manifest["version"], manifest["sha"]), ("v1.0.0", SHA))

    def _emergency_api(self, version: str = "v1.0.0") -> FakeApi:
        """Создаёт immutable аварийный Release с точным служебным блоком."""
        api = FakeApi(version)
        api.release["body"] = (
            f"Исправление\n\nТип выпуска: аварийный\nCommit: `{SHA}`\n"
            f"Ветка: `{api.branch}`\nПричина: срочный сбой\n"
            f"Полный CI: {api.workflow} failure — "
            f"https://github.com/{REPO}/actions/runs/1/attempts/1\n\n"
            "PR и состояние на момент merge:\n"
            f"- PR #42 -> main; merge `{SHA}` (2026-10-15T00:00:00Z); "
            "pr-fast=failure [GitHub Actions]; bypass=yes\n\n"
            "Неподтверждённые или неуспешные проверки:\n- main-full: failure\n"
        )
        api.run["conclusion"] = "failure"
        api.run["created_at"] = "2026-10-15T00:00:00Z"
        return api

    def test_emergency_selector_keeps_full_ci_failure_visible(self) -> None:
        """Новая линия и PATCH допускают failure CI лишь по аварийному пути."""
        for version, workflow in (("v1.0.0", selector.MAIN_WORKFLOW),
                                  ("v1.0.1", selector.RELEASE_WORKFLOW)):
            with self.subTest(version=version):
                api = self._emergency_api(version)
                with patch.object(selector, "_verify_emergency_pulls"):
                    selected = selector.select_emergency_release(api, api.version)
                self.assertEqual((selected.kind, selected.sha, selected.ci_state,
                                  selected.reason, selected.workflow),
                                 ("emergency", SHA, "failure", "срочный сбой", workflow))
                with self.assertRaises(selector.DeploySelectionError):
                    selector.select_release(api, api.version)

    def test_emergency_manifest_and_api_fail_closed(self) -> None:
        """Подмена служебных полей и ошибка API не допускают аварийный SHA."""
        for old, new in ((f"`{SHA}`", f"`{NEW_HEAD}`"),
                         ("Тип выпуска: аварийный", "Тип выпуска: обычный"),
                         ("Причина: срочный сбой", "Причина: "),
                         ("release/1.0", "release/1.1")):
            with self.subTest(old=old):
                api = self._emergency_api()
                api.release["body"] = api.release["body"].replace(old, new)
                with patch.object(selector, "_verify_emergency_pulls"), \
                     self.assertRaises(selector.DeploySelectionError):
                    selector.select_emergency_release(api, api.version)
        api = self._emergency_api()
        api.error = selector.ControllerError("API недоступен")
        with patch.object(selector, "_verify_emergency_pulls"), \
             self.assertRaises(selector.ControllerError):
            selector.select_emergency_release(api, api.version)
        api = self._emergency_api()
        api.release["body"] += "Причина: вторая причина\n"
        with patch.object(selector, "_verify_emergency_pulls"), \
             self.assertRaises(selector.DeploySelectionError):
            selector.select_emergency_release(api, api.version)
        api = self._emergency_api()
        api.tag["object"]["sha"] = NEW_HEAD
        with patch.object(selector, "_verify_emergency_pulls"), \
             self.assertRaises(selector.DeploySelectionError):
            selector.select_emergency_release(api, api.version)
        api = self._emergency_api()
        api.release["body"] = api.release["body"].replace("bypass=yes", "bypass=unknown")
        with patch.object(selector, "_verify_emergency_pulls"), \
             self.assertRaises(selector.DeploySelectionError):
            selector.select_emergency_release(api, api.version)
        api = self._emergency_api()
        api.release["body"] = api.release["body"].replace("failure — https://", "not_run — https://")
        with patch.object(selector, "_verify_emergency_pulls"), \
             self.assertRaises(selector.DeploySelectionError):
            selector.select_emergency_release(api, api.version)


if __name__ == "__main__":
    unittest.main()
