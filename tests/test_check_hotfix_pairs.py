"""Проверяет связи hotfix PR и фактическое слияние выбранных линий."""

from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from scripts import check_hotfix_pairs as hotfix


BASE = "a" * 40
RELEASE_SHA = "b" * 40
MAIN_SHA = "c" * 40
NEXT_SHA = "d" * 40
OTHER_HEAD = "e" * 40
URL = f"https://github.com/{hotfix.EXPECTED_REPOSITORY}/pull/"


def links(*numbers: int) -> str:
    """Строит блок полных URL для тестового PR."""
    return "Hotfix-Links:\n" + "\n".join(f"{URL}{number}" for number in numbers) + "\n"


def main_commit(sha: str = MAIN_SHA) -> str:
    """Строит ссылку на уже существующий commit в main."""
    return f"Hotfix-Main-Commit: https://github.com/{hotfix.EXPECTED_REPOSITORY}/commit/{sha}\n"


class FakeApi:
    """Возвращает управляемые PR, refs и результаты GitHub compare."""

    repository = hotfix.EXPECTED_REPOSITORY

    def __init__(self) -> None:
        """Создаёт группу main и одной релизной линии."""
        self.pulls = {
            10: {"number": 10, "body": links(11), "base": {"ref": "release/1.0"},
                 "merged_at": None, "merge_commit_sha": None},
            11: {"number": 11, "body": links(10), "base": {"ref": "main"},
                 "merged_at": None, "merge_commit_sha": None},
        }
        self.heads = {"main": MAIN_SHA, "release/1.0": RELEASE_SHA}
        self.releases = []
        self.compare_status = "ahead"
        self.error_path = None

    def add_second_line(self) -> None:
        """Выбирает release/1.1 после release/1.0."""
        self.pulls[11]["body"] = links(10, 12)
        self.pulls[10]["body"] = links(11, 12)
        self.pulls[12] = {"number": 12, "body": links(11, 10),
                          "base": {"ref": "release/1.1"},
                          "merged_at": None, "merge_commit_sha": None}
        self.heads["release/1.1"] = NEXT_SHA

    def merge(self, number: int, sha: str) -> None:
        """Фиксирует merge commit выбранного PR."""
        self.pulls[number]["merged_at"] = "2026-10-18T00:00:00Z"
        self.pulls[number]["merge_commit_sha"] = sha

    def get(self, path, **parameters):
        """Читает PR, ref или сравнение; может имитировать отказ API."""
        if path == self.error_path:
            raise hotfix.HotfixError("API недоступен")
        if path.startswith("/pulls/"):
            return deepcopy(self.pulls[int(path.rsplit("/", 1)[1])])
        if path.startswith("/git/ref/heads/"):
            branch = path.removeprefix("/git/ref/heads/")
            return {"ref": f"refs/heads/{branch}", "object": {"sha": self.heads[branch]}}
        if path.startswith("/compare/"):
            base, head = path.removeprefix("/compare/").split("...")
            return {"status": self.compare_status, "base_commit": {"sha": base},
                    "head_commit": {"sha": head}}
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Читает список Release или PR, связанный с merge commit."""
        if path == self.error_path:
            raise hotfix.HotfixError("API недоступен")
        if path == "/releases":
            return deepcopy(self.releases)
        if path.startswith("/commits/") and path.endswith("/pulls"):
            sha = path.split("/")[2]
            return [deepcopy(pr) for pr in self.pulls.values()
                    if pr["merge_commit_sha"] == sha]
        raise AssertionError(path)


class HotfixTests(unittest.TestCase):
    """Проверяет живой набор выбранных PR и публикационный допуск."""

    def setUp(self) -> None:
        """Изолирует API и локальную историю Git."""
        self.api = FakeApi()
        self.origin = patch.object(hotfix, "repository_from_origin",
                                   return_value=hotfix.EXPECTED_REPOSITORY)
        self.origin.start()
        self.addCleanup(self.origin.stop)
        self.git = patch.object(hotfix, "git", side_effect=self.fake_git)
        self.git.start()
        self.addCleanup(self.git.stop)
        admission = patch.object(hotfix, "select_successful_run", return_value=object())
        self.admission = admission.start()
        self.addCleanup(admission.stop)
        self.tag = patch.object(hotfix, "tag_commit", return_value=BASE)
        self.tag.start()
        self.addCleanup(self.tag.stop)

    def fake_git(self, *args):
        """Представляет первую родительскую линию с одним merge PR."""
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return RELEASE_SHA
        if args == ("rev-list", "--first-parent", RELEASE_SHA):
            return f"{RELEASE_SHA}\n{BASE}"
        if args == ("rev-list", "--parents", "-n", "1", RELEASE_SHA):
            return f"{RELEASE_SHA} {BASE} {'f' * 40}"
        raise AssertionError(args)

    def test_main_pr_can_pass_before_release_merge(self) -> None:
        """Main PR проверяет ссылки, но не ждёт слияние релизного PR."""
        self.assertEqual(hotfix.check_links(self.api, 11, "main"), (11, (10,)))
        self.assertEqual(hotfix.check_links(self.api, 10, "release/1.0"), (11, (10,)))
        self.assertEqual(hotfix.check_pr(self.api, 11, "main"), (11, (10,)))
        with self.assertRaisesRegex(hotfix.HotfixError, "ещё не слит"):
            hotfix.check_pr(self.api, 10, "release/1.0")

    def test_existing_main_commit_one_release_needs_no_links(self) -> None:
        """Один release PR может ссылаться только на достижимый main commit."""
        self.api.pulls[10]["body"] = main_commit()
        self.assertEqual(hotfix.check_links(self.api, 10, "release/1.0"), (None, (10,)))
        self.assertEqual(hotfix.check_pr(self.api, 10, "release/1.0"), (None, (10,)))
        self.api.merge(10, RELEASE_SHA)
        self.assertEqual(hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE),
                         [(10, None, MAIN_SHA)])

    def test_existing_main_commit_multiple_lines_need_mutual_links(self) -> None:
        """Несколько выбранных релизных PR делят SHA и ждут старшую линию."""
        self.api.add_second_line()
        self.api.pulls[10]["body"] = main_commit() + links(12)
        self.api.pulls[12]["body"] = main_commit() + links(10)
        self.assertEqual(hotfix.check_pr(self.api, 10, "release/1.0"), (None, (10, 12)))
        with self.assertRaisesRegex(hotfix.HotfixError, "ещё не слит"):
            hotfix.check_pr(self.api, 12, "release/1.1")
        self.api.merge(10, RELEASE_SHA)
        self.assertEqual(hotfix.check_pr(self.api, 12, "release/1.1"), (None, (10, 12)))
        self.api.pulls[12]["body"] = main_commit(OTHER_HEAD) + links(10)
        with self.assertRaisesRegex(hotfix.HotfixError, "основание main"):
            hotfix.check_pr(self.api, 12, "release/1.1")

    def test_existing_main_commit_bad_url_and_unreachable_fail(self) -> None:
        """Чужой URL и commit вне текущего main закрывают допуск."""
        self.api.pulls[10]["body"] = "Hotfix-Main-Commit: https://github.com/other/repo/commit/" + MAIN_SHA
        with self.assertRaisesRegex(hotfix.HotfixError, "неверный URL"):
            hotfix.check_links(self.api, 10, "release/1.0")
        self.api.pulls[10]["body"] = main_commit()
        self.api.heads["main"] = OTHER_HEAD
        self.api.compare_status = "diverged"
        with self.assertRaisesRegex(hotfix.HotfixError, "недостижим"):
            hotfix.check_pr(self.api, 10, "release/1.0")

    def test_release_requires_main_merge_and_reachability(self) -> None:
        """Флаг merged без достижимости merge commit в main недостаточен."""
        self.api.merge(11, MAIN_SHA)
        self.assertEqual(hotfix.check_pr(self.api, 10, "release/1.0"), (11, (10,)))
        self.api.heads["main"] = OTHER_HEAD
        self.api.compare_status = "diverged"
        with self.assertRaisesRegex(hotfix.HotfixError, "не подтверждён"):
            hotfix.check_pr(self.api, 10, "release/1.0")

    def test_selected_lines_merge_old_to_new(self) -> None:
        """Новая выбранная линия ждёт предыдущую; main не ждёт ни одну."""
        self.api.add_second_line()
        self.api.merge(11, MAIN_SHA)
        self.assertEqual(hotfix.check_pr(self.api, 11, "main"), (11, (10, 12)))
        self.assertEqual(hotfix.check_pr(self.api, 10, "release/1.0"), (11, (10, 12)))
        with self.assertRaisesRegex(hotfix.HotfixError, "ещё не слит"):
            hotfix.check_pr(self.api, 12, "release/1.1")
        self.api.merge(10, RELEASE_SHA)
        self.assertEqual(hotfix.check_pr(self.api, 12, "release/1.1"), (11, (10, 12)))
        self.api.heads["release/1.0"] = OTHER_HEAD
        self.api.compare_status = "behind"
        with self.assertRaisesRegex(hotfix.HotfixError, "не подтверждён"):
            hotfix.check_pr(self.api, 12, "release/1.1")

    def test_wrong_repo_missing_reciprocal_and_stale_body_fail(self) -> None:
        """Свежая проверка читает текущие описания и точные URL репозитория."""
        self.api.pulls[10]["body"] = "Hotfix-Links:\nhttps://github.com/other/repo/pull/11"
        with self.assertRaisesRegex(hotfix.HotfixError, "Неверный полный URL"):
            hotfix.check_pr(self.api, 11, "main")
        self.api.pulls[10]["body"] = links(12)
        with self.assertRaisesRegex(hotfix.HotfixError, "взаимных URL"):
            hotfix.check_pr(self.api, 11, "main")
        self.api.pulls[10]["body"] = links(11)
        self.assertEqual(hotfix.check_pr(self.api, 11, "main"), (11, (10,)))
        self.api.pulls[11]["body"] = ""
        with self.assertRaisesRegex(hotfix.HotfixError, "Hotfix-Links"):
            hotfix.check_pr(self.api, 10, "release/1.0")

    def test_duplicate_branch_and_api_failure_close_gate(self) -> None:
        """Выбор двух PR в одну линию и отказ API дают закрытую ошибку."""
        self.api.add_second_line()
        self.api.pulls[12]["base"]["ref"] = "release/1.0"
        with self.assertRaisesRegex(hotfix.HotfixError, "однозначную группу"):
            hotfix.check_pr(self.api, 11, "main")
        self.api = FakeApi()
        self.api.error_path = "/pulls/10"
        with self.assertRaisesRegex(hotfix.HotfixError, "API недоступен"):
            hotfix.check_pr(self.api, 11, "main")

    def test_dependency_compare_requires_exact_sha(self) -> None:
        """Достижимость не выводится из одного статуса ahead."""
        self.api.merge(11, MAIN_SHA)
        self.api.heads["main"] = OTHER_HEAD
        original_get = self.api.get

        def malformed_compare(path, **kwargs):
            if path.startswith("/compare/"):
                return {"status": "ahead", "base_commit": {"sha": MAIN_SHA}}
            return original_get(path, **kwargs)

        with patch.object(self.api, "get", side_effect=malformed_compare):
            with self.assertRaisesRegex(hotfix.HotfixError, "не подтверждён"):
                hotfix.check_pr(self.api, 10, "release/1.0")

    def test_stale_links_fail_even_after_prior_success(self) -> None:
        """Повторный вызов читает новое описание main PR."""
        self.api.merge(11, MAIN_SHA)
        self.assertEqual(hotfix.check_pr(self.api, 10, "release/1.0"), (11, (10,)))
        self.api.pulls[11]["body"] = ""
        with self.assertRaises(hotfix.HotfixError):
            hotfix.check_pr(self.api, 10, "release/1.0")

    def test_publication_requires_current_release_merge_and_main(self) -> None:
        """Публикация проверяет всю первую родительскую линию и main merge."""
        self.api.merge(11, MAIN_SHA)
        self.api.merge(10, RELEASE_SHA)
        self.assertEqual(hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE),
                         [(10, 11, MAIN_SHA)])
        self.api.pulls[11]["merged_at"] = None
        with self.assertRaisesRegex(hotfix.HotfixError, "ещё не слит"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)

    def test_emergency_skips_only_full_ci(self) -> None:
        """Аварийный путь сохраняет ссылки и merge main."""
        self.api.merge(11, MAIN_SHA)
        self.api.merge(10, RELEASE_SHA)
        self.admission.side_effect = lambda api, repo, workflow, sha, branch: (
            object() if workflow == hotfix.MAIN_WORKFLOW else None
        )
        with self.assertRaisesRegex(hotfix.HotfixError, "release-full"):
            hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.assertEqual(hotfix.check_release(self.api, "release/1.0", RELEASE_SHA,
                                              BASE, require_full_ci=False), [(10, 11, MAIN_SHA)])

    def test_direct_change_dirty_checkout_and_baseline_fail(self) -> None:
        """Нет допуска при прямом commit, грязном checkout или старом baseline."""
        self.api.merge(11, MAIN_SHA)
        self.api.merge(10, RELEASE_SHA)
        with patch.object(hotfix, "git", side_effect=lambda *a:
                          f"{RELEASE_SHA} {BASE}" if a[:2] == ("rev-list", "--parents")
                          else self.fake_git(*a)):
            with self.assertRaisesRegex(hotfix.HotfixError, "без PR merge"):
                hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        with patch.object(hotfix, "git", side_effect=lambda *a:
                          " M file.py" if a[:1] == ("status",) else self.fake_git(*a)):
            with self.assertRaisesRegex(hotfix.HotfixError, "чистый checkout"):
                hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)
        self.api.releases = [{"tag_name": "v1.0.1", "draft": False, "prerelease": False}]
        with patch.object(hotfix, "tag_commit", return_value=OTHER_HEAD):
            with self.assertRaisesRegex(hotfix.HotfixError, "последним опубликованным"):
                hotfix.check_release(self.api, "release/1.0", RELEASE_SHA, BASE)


if __name__ == "__main__":
    unittest.main()
