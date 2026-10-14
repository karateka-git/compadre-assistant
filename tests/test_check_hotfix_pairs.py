"""Проверяет связь PR и запрет публикации неподтверждённого hotfix."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import subprocess
import unittest
from unittest.mock import patch

from scripts import check_hotfix_pairs as hotfix


REAL_PATCH_DIGEST = hotfix.patch_digest
BASE = "a" * 40
RELEASE_SHA = "b" * 40
MAIN_SHA = "c" * 40


class FakeApi:
    """Возвращает управляемые PR, refs и комментарии GitHub."""

    repository = hotfix.EXPECTED_REPOSITORY

    def __init__(self) -> None:
        """Создаёт взаимную пару уже слитых PR."""
        self.release = {
            "number": 10, "body": "Hotfix-Pair: #11", "base": {"ref": "release/1.0"},
            "merged_at": "2026-10-14T00:00:00Z", "merge_commit_sha": RELEASE_SHA,
        }
        self.main = {
            "number": 11, "body": "Hotfix-Pair: #10", "base": {"ref": "main"},
            "merged_at": "2026-10-14T00:01:00Z", "merge_commit_sha": MAIN_SHA,
        }
        self.comments: list[dict] = []
        self.releases: list[dict] = []
        self.release_head = RELEASE_SHA
        self.main_head = MAIN_SHA

    def get(self, path, **parameters):
        """Читает PR, ref или сравнение коммитов."""
        if path == "/pulls/10":
            return deepcopy(self.release)
        if path == "/pulls/11":
            return deepcopy(self.main)
        if path == "/git/ref/heads/release/1.0":
            return {"object": {"sha": self.release_head}}
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.main_head}}
        if path == f"/compare/{MAIN_SHA}...{self.main_head}":
            return {"status": "identical", "base_commit": {"sha": MAIN_SHA}}
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Читает PR merge commit и подтверждение владельца."""
        if path == "/releases":
            return deepcopy(self.releases)
        if path == f"/commits/{RELEASE_SHA}/pulls":
            return [deepcopy(self.release)]
        if path == f"/commits/{MAIN_SHA}/pulls":
            return [deepcopy(self.main)]
        if path == "/issues/10/comments":
            return deepcopy(self.comments)
        raise AssertionError(path)


class HotfixTests(unittest.TestCase):
    """Проверяет успешную пару и отказ при неполном доказательстве."""

    def setUp(self) -> None:
        """Изолирует API, историю Git и расчёт patch-id."""
        self.api = FakeApi()
        origin = patch.object(hotfix, "repository_from_origin", return_value=hotfix.EXPECTED_REPOSITORY)
        origin.start()
        self.addCleanup(origin.stop)
        git_command = patch.object(hotfix, "git", side_effect=self.fake_git)
        git_command.start()
        self.addCleanup(git_command.stop)
        patches = patch.object(hotfix, "patch_digest", return_value="same")
        self.patch_ids = patches.start()
        self.addCleanup(patches.stop)
        admission = patch.object(hotfix, "select_successful_run", return_value=object())
        self.admission = admission.start()
        self.addCleanup(admission.stop)
        tag = patch.object(hotfix, "tag_commit", return_value=BASE)
        tag.start()
        self.addCleanup(tag.stop)

    def fake_git(self, *args, **kwargs):
        """Изображает первую родительскую линию с одним PR merge."""
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return RELEASE_SHA
        if args == ("rev-list", "--first-parent", RELEASE_SHA):
            return f"{RELEASE_SHA}\n{BASE}"
        if args == ("rev-list", "--parents", "-n", "1", RELEASE_SHA):
            return f"{RELEASE_SHA} {BASE} {'d' * 40}"
        raise AssertionError(args)

    def test_recipient_pr_requires_reciprocal_pair(self) -> None:
        """Ранний check отвергает отсутствующую обратную ссылку."""
        self.assertEqual(hotfix.check_pr(self.api, 10, "release/1.0"), (10, 11))
        self.api.main["body"] = "Hotfix-Pair: #99"
        with self.assertRaisesRegex(hotfix.HotfixError, "взаимные"):
            hotfix.check_pr(self.api, 10, "release/1.0")

    def test_identical_patch_admits_both_merged_prs(self) -> None:
        """Одинаковый точный Git diff допускает слитую пару."""
        result = hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.assertEqual(result, [(10, 11, "точный Git diff SHA-256 same")])

    def test_pending_main_or_moved_release_fails(self) -> None:
        """Публикация ждёт merge в main и точный tip релизной ветки."""
        self.api.main["merged_at"] = None
        with self.assertRaisesRegex(hotfix.HotfixError, "ещё не слит"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.api.release_head = BASE
        with self.assertRaisesRegex(hotfix.HotfixError, "текущим SHA"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)

    def test_different_patch_requires_owner_comment_with_exact_merges(self) -> None:
        """Разную адаптацию подтверждает только запись владельца с SHA."""
        self.patch_ids.side_effect = lambda sha: "release-patch" if sha == RELEASE_SHA else "main-patch"
        with self.assertRaisesRegex(hotfix.HotfixError, "патчи различаются"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.api.comments = [{"user": {"login": "other"}, "body":
                              f"Hotfix-Adaptation-Approved: #11 release={RELEASE_SHA} main={MAIN_SHA}"}]
        with self.assertRaisesRegex(hotfix.HotfixError, "патчи различаются"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.api.comments[0]["user"]["login"] = "karateka-git"
        result = hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.assertEqual(result[0][2], "ручное подтверждение владельца")

    def test_unreviewed_release_commit_fails_closed(self) -> None:
        """Прямое изменение release не проходит проверку всей линии."""
        with patch.object(hotfix, "git", side_effect=lambda *a, **k:
                          f"{RELEASE_SHA} {BASE}" if a[:2] == ("rev-list", "--parents")
                          else self.fake_git(*a, **k)):
            with self.assertRaisesRegex(hotfix.HotfixError, "без PR merge"):
                hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)

    def test_patch_digest_uses_exact_diff_bytes(self) -> None:
        """Различие байтов diff меняет доказательство одинакового патча."""
        with patch.object(hotfix.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b"diff\r\n", stderr=b"")
            self.assertEqual(REAL_PATCH_DIGEST(RELEASE_SHA),
                             hashlib.sha256(b"diff\r\n").hexdigest())
            self.assertIn("--binary", run.call_args.args[0])
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b"diff\n", stderr=b"")
            self.assertNotEqual(REAL_PATCH_DIGEST(RELEASE_SHA),
                                hashlib.sha256(b"diff\r\n").hexdigest())

    def test_full_ci_and_baseline_are_required(self) -> None:
        """Без успешного полного CI или допустимой базовой точки допуска нет."""
        self.admission.return_value = None
        with self.assertRaisesRegex(hotfix.HotfixError, "baseline"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.admission.side_effect = lambda api, repo, workflow, sha, branch: (
            object() if workflow == hotfix.MAIN_WORKFLOW else None
        )
        with self.assertRaisesRegex(hotfix.HotfixError, "release-full"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)

    def test_baseline_must_be_last_published_release(self) -> None:
        """Произвольный старый baseline не может скрыть последующий выпуск."""
        self.api.releases = [{"tag_name": "v1.0.1", "draft": False, "prerelease": False}]
        with patch.object(hotfix, "tag_commit", return_value="d" * 40):
            with self.assertRaisesRegex(hotfix.HotfixError, "последним опубликованным"):
                hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)

    def test_dirty_checkout_is_rejected(self) -> None:
        """Локальные изменения запрещены при проверке точного candidate."""
        with patch.object(hotfix, "git", side_effect=lambda *a, **k:
                          " M app.py" if a[:1] == ("status",) else self.fake_git(*a, **k)):
            with self.assertRaisesRegex(hotfix.HotfixError, "чистый checkout"):
                hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)


if __name__ == "__main__":
    unittest.main()
