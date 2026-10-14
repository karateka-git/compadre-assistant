"""Проверяет подготовку релизной ветки без GitHub и изменений Git."""

from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import subprocess
import unittest
from unittest.mock import patch

from scripts import create_release_branch as creator
from tests.test_full_ci_controller import SHA_C, SHA_D, admission_jobs, admission_run


class FakeApi:
    """Отдаёт управляемые ответы для истории main и полного CI."""

    repository = creator.EXPECTED_REPOSITORY

    def __init__(self) -> None:
        """Создаёт один успешный полный запуск для SHA кандидата."""
        self.main_head = SHA_D
        self.status = "identical"
        self.base_sha = SHA_D
        self.branches: list[dict] = []
        self.runs = [admission_run(SHA_D)]
        self.jobs = admission_jobs(SHA_D)
        self.runs[0]["repository"]["full_name"] = self.repository
        self.runs[0]["head_repository"]["full_name"] = self.repository
        self.main_reads = 0
        self.remote_sha: str | None = None
        self.post_error: Exception | None = None
        self.post_response: dict | None = None
        self.posts: list[tuple[str, dict]] = []

    def get(self, path, **parameters):
        """Возвращает metadata workflow, ref main или сравнение commit."""
        if path == "/git/ref/heads/main":
            self.main_reads += 1
            return {"object": {"sha": self.main_head}}
        if path.startswith("/compare/"):
            return {"status": self.status, "base_commit": {"sha": self.base_sha}}
        if path == "/actions/workflows/main-full.yml":
            return {"id": 1, "path": ".github/workflows/main-full.yml"}
        if path == "/git/ref/heads/release/1.0":
            return {"ref": "refs/heads/release/1.0", "object": {"sha": self.remote_sha}}
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Возвращает только подготовленные ветки, runs и jobs."""
        if path == "/branches":
            return self.branches
        if path == "/actions/workflows/main-full.yml/runs":
            return self.runs
        if path == "/actions/runs/1/attempts/1/jobs":
            return self.jobs
        raise AssertionError(path)

    def post(self, path, payload):
        """Имитирует атомарное создание удалённого Git ref."""
        self.posts.append((path, payload))
        if self.post_error:
            raise self.post_error
        if self.branches:
            raise creator.PublishingError("HTTP 422: ref exists")
        self.branches = [{"name": payload["ref"].removeprefix("refs/heads/")}]
        self.remote_sha = payload["sha"]
        return self.post_response if self.post_response is not None else {
            "ref": payload["ref"], "object": {"sha": payload["sha"]},
        }


class ReleaseBranchTests(unittest.TestCase):
    """Проверяет отказ при неподтверждённом SHA и атомарное создание ref."""

    def setUp(self) -> None:
        """Изолирует ответ API и команды локального Git."""
        self.api = FakeApi()
        self.commands: list[tuple[str, ...]] = []
        self.local_sha: str | None = None
        self.origin = patch.object(creator, "repository_from_origin", return_value=creator.EXPECTED_REPOSITORY)
        self.origin.start()
        self.addCleanup(self.origin.stop)
        self.git_patch = patch.object(creator, "git", side_effect=self.fake_git)
        self.git_patch.start()
        self.addCleanup(self.git_patch.stop)

    def fake_git(self, *arguments, **kwargs):
        """Запоминает команды и изображает свободный локальный ref."""
        self.commands.append(arguments)
        if arguments[:2] == ("cat-file", "-t"):
            return subprocess.CompletedProcess(arguments, 0, stdout="commit\n")
        if arguments[:2] == ("show-ref", "--verify"):
            return subprocess.CompletedProcess(arguments, 0 if self.local_sha else 1)
        if arguments[:2] == ("rev-parse", "--verify"):
            return subprocess.CompletedProcess(arguments, 0, stdout=f"{self.local_sha}\n")
        if arguments[0] == "update-ref":
            return subprocess.CompletedProcess(arguments, 0)
        raise AssertionError(arguments)

    def test_exact_success_prepares_without_creating_ref(self) -> None:
        """Успешная подготовка сообщает run и дважды читает историю main."""
        result = creator.prepare(self.api, "release/1.0", SHA_D)
        self.assertEqual((result.sha, result.run_id, result.run_attempt), (SHA_D, 1, 1))
        self.assertEqual(self.api.main_reads, 2)
        self.assertFalse(any(command[0] == "update-ref" for command in self.commands))

    def test_invalid_name_sha_repo_or_existing_branch_is_rejected(self) -> None:
        """Неверный ввод и занятое имя отвергаются до создания ref."""
        for branch, sha in (("release/1.0.0", SHA_D), ("release/1.0", SHA_D[:8])):
            with self.subTest(branch=branch, sha=sha):
                with self.assertRaises(creator.ReleaseBranchError):
                    creator.prepare(self.api, branch, sha)
        self.api.branches = [{"name": "release/1.0"}]
        with self.assertRaisesRegex(creator.ReleaseBranchError, "Удалённая"):
            creator.prepare(self.api, "release/1.0", SHA_D)
        self.api.branches = []
        self.api.repository = "other/repo"
        with self.assertRaisesRegex(creator.ReleaseBranchError, "Репозиторий"):
            creator.prepare(self.api, "release/1.0", SHA_D)
        self.assertFalse(any(command[0] == "update-ref" for command in self.commands))

    def test_non_ancestor_and_moved_main_are_rejected(self) -> None:
        """Чужой commit и force move main не дают создать ветку."""
        self.api.status = "diverged"
        with self.assertRaisesRegex(creator.ReleaseBranchError, "предком"):
            creator.prepare(self.api, "release/1.0", SHA_D)
        self.api.status = "ahead"
        self.api.base_sha = SHA_C
        with self.assertRaisesRegex(creator.ReleaseBranchError, "предком"):
            creator.prepare(self.api, "release/1.0", SHA_D)
        self.api.base_sha = SHA_D
        original_get = self.api.get

        def moving_get(path, **parameters):
            if path == "/compare/" + SHA_D + "..." + SHA_C:
                return {"status": "diverged", "base_commit": {"sha": SHA_C}}
            if path == "/git/ref/heads/main" and self.api.main_reads >= 1:
                self.api.main_head = SHA_C
            return original_get(path, **parameters)

        self.api.get = moving_get
        self.api.main_reads = 0
        with self.assertRaisesRegex(creator.ReleaseBranchError, "предком"):
            creator.prepare(self.api, "release/1.0", SHA_D)

    def test_wrong_workflow_or_partial_success_is_rejected(self) -> None:
        """Чужой workflow и skipped шаг полного набора не дают допуска."""
        original = deepcopy(self.api.runs)
        self.api.runs[0]["workflow_id"] = 99
        with self.assertRaisesRegex(creator.ReleaseBranchError, "Нет успешного"):
            creator.prepare(self.api, "release/1.0", SHA_D)
        self.api.runs = original
        self.api.jobs[1]["steps"][0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(creator.ReleaseBranchError, "Нет успешного"):
            creator.prepare(self.api, "release/1.0", SHA_D)

    def test_remote_branch_created_during_ci_check_is_rejected(self) -> None:
        """Второе чтение имени обнаруживает параллельное создание ветки."""
        original_pages = self.api.pages
        reads = 0

        def changing_pages(path, key=None, **parameters):
            nonlocal reads
            if path == "/branches":
                reads += 1
                if reads == 2:
                    return [{"name": "release/1.0"}]
            return original_pages(path, key=key, **parameters)

        self.api.pages = changing_pages
        with self.assertRaisesRegex(creator.ReleaseBranchError, "Удалённая"):
            creator.prepare(self.api, "release/1.0", SHA_D)
        self.assertEqual(reads, 2)

    def test_api_failure_is_not_treated_as_absent_branch(self) -> None:
        """Недоступный API останавливает подготовку и не создаёт ref."""
        self.api.pages = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API down"))
        with self.assertRaisesRegex(RuntimeError, "API down"):
            creator.prepare(self.api, "release/1.0", SHA_D)
        self.assertFalse(any(command[0] == "update-ref" for command in self.commands))

    def test_creation_uses_absent_ref_as_atomic_precondition(self) -> None:
        """update-ref требует нулевой прежний SHA и не переключает checkout."""
        creator.create_local_branch("release/1.0", SHA_D)
        self.assertEqual(self.commands, [
            ("update-ref", "refs/heads/release/1.0", SHA_D, creator.ZERO_SHA),
        ])

    def test_remote_publish_rechecks_and_confirms_exact_ref(self) -> None:
        """POST получает точный SHA только после допуска и read-back."""
        self.local_sha = SHA_D
        result = creator.publish_remote_branch(self.api, "release/1.0", SHA_D)
        self.assertEqual(result.sha, SHA_D)
        self.assertEqual(self.api.posts, [("/git/refs", {
            "ref": "refs/heads/release/1.0", "sha": SHA_D,
        })])
        self.assertEqual(self.api.remote_sha, SHA_D)
        self.assertEqual(self.api.main_reads, 2)

    def test_remote_publish_requires_exact_local_ref(self) -> None:
        """Отсутствующий или иной локальный SHA не вызывает POST."""
        for local_sha in (None, SHA_C):
            with self.subTest(local_sha=local_sha):
                self.local_sha = local_sha
                with self.assertRaisesRegex(creator.ReleaseBranchError, "Локальная"):
                    creator.publish_remote_branch(self.api, "release/1.0", SHA_D)
        self.assertEqual(self.api.posts, [])

    def test_remote_race_is_rejected_by_atomic_post(self) -> None:
        """GitHub 422 при параллельном создании не переиспользует ref."""
        self.local_sha = SHA_D
        self.api.post_error = creator.PublishingError("HTTP 422")
        with self.assertRaisesRegex(creator.PublishingError, "422"):
            creator.publish_remote_branch(self.api, "release/1.0", SHA_D)
        self.assertEqual(len(self.api.posts), 1)

    def test_remote_response_and_readback_mismatch_fail_closed(self) -> None:
        """Ошибочный ответ POST и сдвинутый read-back не подтверждают ветку."""
        self.local_sha = SHA_D
        self.api.post_response = {"ref": "refs/heads/release/1.0", "object": {"sha": SHA_C}}
        with self.assertRaisesRegex(creator.ReleaseBranchError, "Ответ создания"):
            creator.publish_remote_branch(self.api, "release/1.0", SHA_D)
        self.api.branches = []
        self.api.post_response = None
        original_get = self.api.get

        def moved_ref(path, **parameters):
            if path == "/git/ref/heads/release/1.0":
                return {"ref": "refs/heads/release/1.0", "object": {"sha": SHA_C}}
            return original_get(path, **parameters)

        self.api.get = moved_ref
        with self.assertRaisesRegex(creator.ReleaseBranchError, "повторном чтении"):
            creator.publish_remote_branch(self.api, "release/1.0", SHA_D)

    def test_check_only_and_local_mode_do_not_post(self) -> None:
        """Обычный запуск и --check-only не обращаются к POST."""
        output = StringIO()
        with patch.object(creator, "PublishingGitHub", return_value=self.api), \
             patch.object(creator.sys, "argv", ["create_release_branch", "release/1.0", SHA_D, "--check-only"]), \
             redirect_stdout(output):
            creator.main()
        self.assertIn("Проверено", output.getvalue())
        with patch.object(creator, "PublishingGitHub", return_value=self.api), \
             patch.object(creator.sys, "argv", ["create_release_branch", "release/1.0", SHA_D]), \
             redirect_stdout(StringIO()):
            creator.main()
        self.assertEqual(self.api.posts, [])

    def test_publish_remote_cli_uses_explicit_post_mode(self) -> None:
        """Флаг публикации вызывает POST и подтверждает удалённую ветку."""
        self.local_sha = SHA_D
        output = StringIO()
        with patch.object(creator, "PublishingGitHub", return_value=self.api), \
             patch.object(creator.sys, "argv", ["create_release_branch", "release/1.0", SHA_D,
                                               "--publish-remote"]), \
             redirect_stdout(output):
            creator.main()
        self.assertIn("Создана удалённая ветка", output.getvalue())
        self.assertEqual(len(self.api.posts), 1)


if __name__ == "__main__":
    unittest.main()
