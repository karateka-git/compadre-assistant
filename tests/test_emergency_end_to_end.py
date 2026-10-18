"""Проверяет аварийный выпуск целиком на локальном Git, fake API и fake host."""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import publish_emergency_release as publish
from scripts import select_deploy_release as select
from tests import test_deploy_prod as host_tests
from tests import test_publish_emergency_release as api_tests


class IntegratedApi(api_tests.FakeApi):
    """Добавляет fake API чтения опубликованного Release и branch selector."""

    def get(self, path, **parameters):
        """Сверяет новые refs без сетевого соединения."""
        if path == "/git/ref/heads/release/1.0":
            return {"ref": "refs/heads/release/1.0", "object": {"type": "commit", "sha": api_tests.SHA}}
        if path == f"/compare/{api_tests.SHA}...{api_tests.SHA}":
            return {"status": "identical", "base_commit": {"sha": api_tests.SHA},
                    "head_commit": {"sha": api_tests.SHA}}
        value = super().get(path, **parameters)
        if path == "/releases/tags/v1.0.0":
            return dict(value, name="v1.0.0")
        return value


class IntegratedHost(host_tests.FakeHost):
    """Направляет fake host в реальный серверный selector с fake API."""

    def __init__(self, root: Path, api: IntegratedApi) -> None:
        """Сохраняет fake API для каждого повторного допуска."""
        root.mkdir()
        super().__init__(root)
        self.api = api

    def select(self, version: str) -> select.SelectedRelease:
        """Использует обычный selector на том же опубликованном Release."""
        return select.select_release(self.api, version)

    def select_emergency(self, version: str) -> select.SelectedRelease:
        """Использует аварийный selector на том же опубликованном Release."""
        return select.select_emergency_release(self.api, version)


class PatchApi(api_tests.FakeApi):
    """Представляет два взаимно связанных merge PR и новый PATCH."""

    def __init__(self, baseline: str, release_sha: str, main_sha: str,
                 release_head: str, main_head: str) -> None:
        """Создаёт историю fake GitHub вокруг локальных Git merge commits."""
        super().__init__()
        self.baseline = baseline
        self.release_sha = release_sha
        self.main_sha = main_sha
        self.release_head = release_head
        self.main_head = main_head
        self.releases = [{"tag_name": "v1.0.0", "published_at": api_tests.MERGED_AT,
                          "draft": False, "prerelease": False}]
        self.tags = [{"ref": "refs/tags/v1.0.0"}]

    def _pr(self, number: int) -> dict:
        """Возвращает точный PR с взаимным URL на вторую ветку."""
        release = number == 10
        linked = 11 if release else 10
        body = ("Hotfix-Links:\n"
                f"https://github.com/{select.EXPECTED_REPOSITORY}/pull/{linked}\n")
        return {"number": number, "body": body,
                "merged_at": api_tests.MERGED_AT,
                "base": {"ref": "release/1.0" if release else "main"},
                "head": {"sha": self.release_head if release else self.main_head},
                "merge_commit_sha": self.release_sha if release else self.main_sha}

    def get(self, path, **parameters):
        """Читает refs, PR, CI и Release без обращения к GitHub."""
        if path == "/git/ref/heads/release/1.0":
            return {"ref": "refs/heads/release/1.0", "object": {"type": "commit", "sha": self.release_sha}}
        if path == "/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.main_sha}}
        if path.startswith("/pulls/"):
            return self._pr(int(path.rsplit("/", 1)[1]))
        if path.startswith("/git/commits/"):
            sha = path.rsplit("/", 1)[1]
            head = self.release_head if sha == self.release_sha else self.main_head
            return {"parents": [{"sha": self.baseline}, {"sha": head}]}
        if path.startswith("/compare/"):
            first, last = path.removeprefix("/compare/").split("...")
            if (first, last) in ((self.release_sha, self.release_sha),
                                 (self.main_sha, self.main_sha)):
                return {"status": "identical", "base_commit": {"sha": first},
                        "head_commit": {"sha": last}}
            raise AssertionError(path)
        if path == "/git/ref/tags/v1.0.0":
            return {"ref": "refs/tags/v1.0.0", "object": {"type": "commit", "sha": self.baseline}}
        if path == "/git/ref/tags/v1.0.1":
            return {"ref": "refs/tags/v1.0.1", "object": {"type": "commit", "sha": self.release_sha}}
        if path == "/releases/tags/v1.0.1":
            return {"id": 42, "tag_name": "v1.0.1", "name": "v1.0.1",
                    "target_commitish": self.release_sha, "body": self.posts[-1][1]["body"],
                    "immutable": True, "draft": False, "prerelease": False,
                    "published_at": "2026-10-16T09:00:00Z"}
        if path == "/actions/workflows/release-full.yml":
            return {"id": 21, "path": ".github/workflows/release-full.yml"}
        if path in ("/rulesets/rule-suites/31", "/rulesets/rule-suites/32"):
            release = path.endswith("31")
            return {"id": 31 if release else 32,
                    "ref": "refs/heads/release/1.0" if release else "refs/heads/main",
                    "after_sha": self.release_sha if release else self.main_sha,
                    "result": "bypass" if release else "pass", "actor_id": 42288695,
                    "rule_evaluations": [{"rule_source": {"id": 25067104}}]}
        return super().get(path, **parameters)

    def pages(self, path, key=None, **parameters):
        """Возвращает merge history, две быстрые проверки и ожидающий full CI."""
        if path == f"/commits/{self.release_sha}/pulls":
            return [self._pr(10)]
        if path == f"/commits/{self.main_sha}/pulls":
            return [self._pr(11)]
        if path == "/actions/workflows/pr-fast.yml/runs":
            return [{"id": 100 + number, "workflow_id": 10, "event": "pull_request",
                     "repository": {"full_name": self.repository},
                     "pull_requests": [{"number": number,
                                        "head": {"sha": self._pr(number)["head"]["sha"]},
                                        "base": self._pr(number)["base"]}],
                     "created_at": "2026-10-16T07:58:00Z", "run_attempt": 1}
                    for number in (10, 11)]
        if path in ("/actions/runs/110/attempts/1/jobs", "/actions/runs/111/attempts/1/jobs"):
            return [{"name": "pr-fast", "started_at": "2026-10-16T07:58:10Z",
                     "completed_at": "2026-10-16T07:59:00Z",
                     "conclusion": "failure" if "/110/" in path else "success"}]
        if path == "/rulesets/rule-suites":
            release = parameters["ref"].endswith("release/1.0")
            return [{"id": 31 if release else 32, "ref": parameters["ref"],
                     "after_sha": self.release_sha if release else self.main_sha,
                     "pushed_at": api_tests.MERGED_AT,
                     "result": "bypass" if release else "pass"}]
        if path == "/actions/workflows/release-full.yml/runs":
            return [{"id": 120, "head_sha": self.release_sha,
                     "head_branch": "release/1.0", "workflow_id": 21, "event": "push",
                     "path": ".github/workflows/release-full.yml@release/1.0",
                     "repository": {"full_name": self.repository},
                     "created_at": "2026-10-16T08:01:00Z", "run_attempt": 1,
                     "status": "in_progress", "conclusion": None}]
        return super().pages(path, key=key, **parameters)


class EmergencyEndToEndTests(unittest.TestCase):
    """Проверяет путь от локального commit до двух fake каналов."""

    def _git(self, root: Path, *args: str) -> str:
        """Выполняет Git только в одноразовом локальном репозитории."""
        return subprocess.run(("git", *args), cwd=root, text=True, encoding="utf-8",
                              errors="replace", check=True,
                              capture_output=True).stdout.strip()

    def test_new_line_without_full_ci_and_tampering(self) -> None:
        """Новая линия проходит лишь аварийный путь; подмена и API отказ закрывают его."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            self._git(repository, "init", "-b", "main")
            self._git(repository, "config", "user.name", "Test")
            self._git(repository, "config", "user.email", "test@example.invalid")
            self._git(repository, "remote", "add", "origin",
                      "https://github.com/karateka-git/compadre-assistant.git")
            (repository / "sample.txt").write_text("base\n", encoding="utf-8")
            self._git(repository, "add", "sample.txt")
            self._git(repository, "commit", "-m", "Основа")
            old = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "checkout", "-b", "topic")
            (repository / "sample.txt").write_text("base\nfix\n", encoding="utf-8")
            self._git(repository, "commit", "-am", "Исправление")
            topic = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "checkout", "main")
            self._git(repository, "merge", "--no-ff", "topic", "-m", "Слияние PR")
            candidate = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "branch", "release/1.0")
            api = IntegratedApi()
            prior = Path.cwd()
            os.chdir(repository)
            try:
                with patch.object(api_tests, "SHA", candidate), \
                     patch.object(api_tests, "MERGE", candidate), \
                     patch.object(api_tests, "HEAD", topic), \
                     patch.object(api_tests, "OLD", old), \
                     patch.object(publish, "tag_commit", return_value=candidate), \
                     patch.object(select, "repository_from_origin", return_value=select.EXPECTED_REPOSITORY):
                    prepared = publish.prepare(api, "v1.0.0", candidate, "initial",
                                               "сбой production", "Исправление", (11,))
                    self.assertEqual(prepared.full.state, "pending")
                    publish.publish(api, prepared)
                    with self.assertRaises(select.DeploySelectionError):
                        select.select_release(api, "v1.0.0")
                    selected = select.select_emergency_release(api, "v1.0.0")
                    self.assertEqual((selected.sha, selected.published_ci_state),
                                     (candidate, "pending"))
                    with patch.object(host_tests, "NEW_SHA", candidate), \
                         patch.object(host_tests, "OLD_SHA", old):
                        host = IntegratedHost(root / "host", api)
                        patches = host_tests.DeployProdTests()._patches()
                        with ExitStack() as stack:
                            for item in patches:
                                stack.enter_context(item)
                            prod.deploy(host, "v1.0.0", emergency=True, reason="восстановить каналы")
                            state = prep.read_state(host.state_dir, host.checkout)
                            self.assertEqual(state["stage"], "success")
                            self.assertEqual(state["deployment"]["published_reason"], "сбой production")
                            self.assertEqual(state["deployment"]["reason"], "восстановить каналы")
                            self.assertIn("stop_both", host.actions)
                            self.assertIn("start_both", host.actions)
                            self.assertTrue(Path(state["last_good"]["backup_id"]).is_file())
                    original = api.posts[-1][1]["body"]
                    for forged in (original.replace("pr-fast=failure", "pr-fast=success"),
                                   original.replace("- PR #11", "- omitted #11"),
                                   original.replace("/runs/102/attempts/1", "/runs/999/attempts/1"),
                                   original.replace("Неподтверждённые или неуспешные проверки:",
                                                    "Итог отсутствует:")):
                        api.posts[-1][1]["body"] = forged
                        with self.assertRaises(select.DeploySelectionError):
                            select.select_emergency_release(api, "v1.0.0")
                    api.posts[-1][1]["body"] = original
                    reads = 0
                    original_get = api.get

                    def racing_get(path: str, **parameters):
                        nonlocal reads
                        result = original_get(path, **parameters)
                        if path == "/releases/tags/v1.0.0":
                            reads += 1
                            if reads > 1:
                                return dict(result, body="Изменено во время допуска")
                        return result

                    with patch.object(api, "get", side_effect=racing_get), \
                         self.assertRaises(select.DeploySelectionError):
                        select.select_emergency_release(api, "v1.0.0")
                    api.fail_api = True
                    with self.assertRaises(publish.ControllerError):
                        select.select_emergency_release(api, "v1.0.0")
            finally:
                os.chdir(prior)

    def test_patch_requires_both_linked_prs_and_keeps_ci_pending(self) -> None:
        """PATCH сверяет два merge PR перед fake публикацией."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            self._git(repository, "init", "-b", "main")
            self._git(repository, "config", "user.name", "Test")
            self._git(repository, "config", "user.email", "test@example.invalid")
            self._git(repository, "remote", "add", "origin",
                      "https://github.com/karateka-git/compadre-assistant.git")
            (repository / "sample.txt").write_text("base\n", encoding="utf-8")
            self._git(repository, "add", "sample.txt")
            self._git(repository, "commit", "-m", "Основа")
            baseline = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "branch", "release/1.0")
            self._git(repository, "checkout", "-b", "release-fix")
            (repository / "sample.txt").write_text("base\nfix\n", encoding="utf-8")
            self._git(repository, "commit", "-am", "Исправление release")
            release_head = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "checkout", "release/1.0")
            self._git(repository, "merge", "--no-ff", "release-fix", "-m", "PR release")
            release_sha = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "checkout", "main")
            self._git(repository, "checkout", "-b", "main-fix")
            (repository / "sample.txt").write_text("base\nfix\n", encoding="utf-8")
            self._git(repository, "commit", "-am", "Исправление main")
            main_head = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "checkout", "main")
            self._git(repository, "merge", "--no-ff", "main-fix", "-m", "PR main")
            main_sha = self._git(repository, "rev-parse", "HEAD")
            self._git(repository, "checkout", "release/1.0")
            api = PatchApi(baseline, release_sha, main_sha, release_head, main_head)
            prior = Path.cwd()
            os.chdir(repository)
            try:
                with patch.object(select, "repository_from_origin", return_value=select.EXPECTED_REPOSITORY):
                    with self.assertRaises(publish.PublishError):
                        publish.prepare(api, "v1.0.2", release_sha, "patch", "срочно",
                                        "Исправление", (10, 11))
                    with self.assertRaises(publish.EmergencyPublishError):
                        publish.prepare(api, "v1.0.1", release_sha, "patch", "срочно",
                                        "Исправление", (10,))
                    prepared = publish.prepare(api, "v1.0.1", release_sha, "patch",
                                               "сбой production", "Исправление", (10, 11))
                    self.assertEqual(prepared.pairs[0][:2], (10, 11))
                    self.assertEqual(prepared.full.state, "pending")
                    publish.publish(api, prepared)
                    with self.assertRaises(select.DeploySelectionError):
                        select.select_release(api, "v1.0.1")
                    selected = select.select_emergency_release(api, "v1.0.1")
                    self.assertEqual((selected.sha, selected.workflow, selected.kind),
                                     (release_sha, "release-full.yml", "emergency"))
                    with patch.object(host_tests, "NEW_SHA", release_sha), \
                         patch.object(host_tests, "OLD_SHA", baseline):
                        host = IntegratedHost(root / "host", api)
                        host.fail_candidate = True
                        patches = host_tests.DeployProdTests()._patches()
                        with ExitStack() as stack:
                            for item in patches:
                                stack.enter_context(item)
                            with self.assertRaises(prod.DeployError):
                                prod.deploy(host, "v1.0.1", emergency=True, reason="срочный возврат")
                        state = prep.read_state(host.state_dir, host.checkout)
                        self.assertEqual(state["stage"], "rolled_back")
                        self.assertEqual(state["deployment"]["published_reason"], "сбой production")
                        self.assertEqual(host.current_image, host_tests.OLD_IMAGE)
                        self.assertIn("stop_both", host.actions)
                    original = api.posts[-1][1]["body"]
                    api.posts[-1][1]["body"] = original.replace(
                        "/pull/11; main merge", "/pull/12; main merge")
                    with self.assertRaises(select.DeploySelectionError):
                        select.select_emergency_release(api, "v1.0.1")
                    api.posts[-1][1]["body"] = original
                    def broken_pair(number: int) -> dict:
                        pr = PatchApi._pr(api, number)
                        return dict(pr, body="Hotfix-Links:\nhttps://github.com/other/repo/pull/10") if number == 11 else pr

                    with patch.object(api, "_pr", side_effect=broken_pair), \
                         self.assertRaisesRegex(Exception, "URL"):
                        select.select_emergency_release(api, "v1.0.1")
            finally:
                os.chdir(prior)


if __name__ == "__main__":
    unittest.main()
