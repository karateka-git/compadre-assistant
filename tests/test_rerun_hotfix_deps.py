"""Проверяет адресный rerun только актуального hotfix-deps."""

from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from scripts import rerun_hotfix_deps as rerun
from tests.test_check_hotfix_pairs import (
    FakeApi as GroupApi, MAIN_SHA, NEXT_SHA, RELEASE_SHA, links, main_commit,
)


HEAD = "f" * 40
TEST_MERGE = "1" * 40


class FakeApi(GroupApi):
    """Представляет группу PR и привязанные к их head Actions runs."""

    root = "https://api.github.com/repos/karateka-git/compadre-assistant"
    token = "test-token"

    def __init__(self) -> None:
        """Создаёт открытую старшую линию с failed hotfix-deps."""
        super().__init__()
        self.pulls[10].update({"state": "open", "head": {"sha": HEAD},
                               "merge_commit_sha": TEST_MERGE})
        self.pulls[11].update({"state": "open", "head": {"sha": "2" * 40}})
        self.runs = [self.run(10, HEAD, TEST_MERGE)]
        self.jobs = [{"id": 77, "name": "hotfix-deps", "status": "completed",
                      "conclusion": "failure"}]

    def run(self, number: int, head: str, merge: str) -> dict:
        """Строит run с точной связью с release PR."""
        return {"id": 55, "event": "pull_request", "status": "completed",
                "head_sha": merge, "pull_requests": [{"number": number,
                    "head": {"sha": head}, "base": {"ref": self.pulls[number]["base"]["ref"]}}]}

    def pages(self, path, key=None, **parameters):
        """Возвращает Actions run/job либо историю PR родителя."""
        if path == "/actions/workflows/pr-fast.yml/runs":
            return deepcopy(self.runs)
        if path == "/actions/runs/55/jobs":
            return deepcopy(self.jobs)
        return super().pages(path, key=key, **parameters)


def event(api: FakeApi, number: int) -> dict:
    """Строит закрытое событие из состояния fake API."""
    pr = api.pulls[number]
    return {"action": "closed", "repository": {"full_name": api.repository},
            "pull_request": {"number": number, "merged": True,
                             "merge_commit_sha": pr["merge_commit_sha"],
                             "base": {"ref": pr["base"]["ref"]}}}


class RerunTests(unittest.TestCase):
    """Проверяет порядок, точную привязку run и закрытые отказы."""

    def setUp(self) -> None:
        """Изолирует данные GitHub для каждого сценария."""
        self.api = FakeApi()

    def test_main_merge_selects_first_release_failed_job(self) -> None:
        """После main merge выбирается старшая линия и её один failed job."""
        self.api.merge(11, MAIN_SHA)
        selected = rerun.target_pr(self.api, event(self.api, 11))
        self.assertEqual(selected["number"], 10)
        self.assertEqual(rerun.current_failed_job(self.api, selected), 77)

    def test_release_merge_selects_next_line(self) -> None:
        """После слияния старшей линии выбирается следующая выбранная."""
        self.api.add_second_line()
        self.api.pulls[12].update({"state": "open", "head": {"sha": HEAD},
                                   "merge_commit_sha": TEST_MERGE})
        self.api.pulls[10]["merge_commit_sha"] = RELEASE_SHA
        self.api.merge(11, MAIN_SHA)
        self.api.merge(10, RELEASE_SHA)
        self.assertEqual(rerun.target_pr(self.api, event(self.api, 10))["number"], 12)

    def test_existing_main_commit_uses_release_chain(self) -> None:
        """Группа без main PR начинает rerun после первой release линии."""
        self.api.add_second_line()
        self.api.pulls[10]["body"] = main_commit() + links(12)
        self.api.pulls[12]["body"] = main_commit() + links(10)
        self.api.pulls[12]["state"] = "open"
        self.api.pulls[10]["merge_commit_sha"] = RELEASE_SHA
        self.api.merge(10, RELEASE_SHA)
        self.assertEqual(rerun.target_pr(self.api, event(self.api, 10))["number"], 12)

    def test_stale_run_or_nonfailure_never_reruns(self) -> None:
        """Чужой head, test merge и success job не выбираются для POST."""
        pr = self.api.pulls[10]
        self.api.runs[0]["pull_requests"][0]["head"]["sha"] = "3" * 40
        with self.assertRaisesRegex(rerun.RerunError, "текущего head"):
            rerun.current_failed_job(self.api, pr)
        self.api.runs = [self.api.run(10, HEAD, "4" * 40)]
        with self.assertRaisesRegex(rerun.RerunError, "test merge"):
            rerun.current_failed_job(self.api, pr)
        self.api.runs = [self.api.run(10, HEAD, TEST_MERGE)]
        self.api.jobs[0]["conclusion"] = "success"
        with self.assertRaisesRegex(rerun.RerunError, "failed hotfix-deps"):
            rerun.current_failed_job(self.api, pr)

    def test_event_and_api_failure_close_selection(self) -> None:
        """Чужой repo и неподтверждённое слияние не выбирают job."""
        self.api.merge(11, MAIN_SHA)
        wrong = event(self.api, 11)
        wrong["repository"]["full_name"] = "other/repo"
        with self.assertRaisesRegex(rerun.RerunError, "Неверное событие"):
            rerun.target_pr(self.api, wrong)
        bad = event(self.api, 11)
        bad["pull_request"]["merge_commit_sha"] = NEXT_SHA
        with self.assertRaisesRegex(rerun.RerunError, "не совпадает"):
            rerun.target_pr(self.api, bad)
        self.api.error_path = "/pulls/11"
        with self.assertRaises(rerun.HotfixError):
            rerun.target_pr(self.api, event(self.api, 11))

    def test_rerun_sends_only_selected_job(self) -> None:
        """POST адресуется одному job и требует HTTP 201."""
        class Response:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        with patch.object(rerun, "urlopen", return_value=Response()) as request:
            rerun.rerun_job(self.api, 77)
        self.assertEqual(request.call_args.args[0].full_url,
                         self.api.root + "/actions/jobs/77/rerun")


if __name__ == "__main__":
    unittest.main()
