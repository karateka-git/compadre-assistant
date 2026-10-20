"""Проверяет адресный rerun только актуального hotfix-deps."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import rerun_hotfix_deps as rerun
from tests.test_check_hotfix_pairs import (
    BASE, FakeApi as GroupApi, MAIN_SHA, NEXT_SHA, RELEASE_SHA, links, main_commit,
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
        self.pulls[10]["base"]["sha"] = BASE
        self.pulls[11].update({"state": "open", "head": {"sha": "2" * 40}})
        self.runs = [self.run(10, HEAD)]
        self.jobs = [{"id": 77, "name": "hotfix-deps", "status": "completed",
                      "conclusion": "failure"}]

    def run(self, number: int, head: str) -> dict:
        """Строит run с точной связью с release PR."""
        return {"id": 55, "event": "pull_request", "status": "completed",
                "head_sha": head, "pull_requests": [{"number": number,
                    "head": {"sha": head}, "base": {"ref": self.pulls[number]["base"]["ref"],
                                              "sha": self.pulls[number]["base"].get("sha", BASE)}}]}

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

    def test_workflow_checks_out_exact_merged_commit(self) -> None:
        """Первый merge запускает код из merge SHA, а не прежнего base SHA."""
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
                    "hotfix-deps-rerun.yml").read_text(encoding="utf-8")
        self.assertIn("ref: ${{ github.event.pull_request.merge_commit_sha }}", workflow)
        self.assertNotIn("ref: ${{ github.event.pull_request.base.sha }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("ref: main", workflow)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha }}", workflow)

    def test_main_merge_selects_single_release_failed_job(self) -> None:
        """После main merge выбирается единственная линия и её failed job."""
        self.api.merge(11, MAIN_SHA)
        selected, = rerun.target_prs(self.api, event(self.api, 11))
        self.assertEqual(selected["number"], 10)
        self.assertEqual(rerun.current_failed_job(self.api, selected), 77)

    def test_main_merge_selects_all_open_lines(self) -> None:
        """После main выбираются все открытые линии без ожидания друг друга."""
        self.api.add_second_line()
        self.api.pulls[12].update({"state": "open", "head": {"sha": HEAD},
                                   "merge_commit_sha": TEST_MERGE})
        self.api.merge(11, MAIN_SHA)
        self.assertEqual(tuple(pr["number"] for pr in rerun.target_prs(
            self.api, event(self.api, 11))), (10, 12))

    def test_release_merge_does_not_trigger_other_lines(self) -> None:
        """Слияние release PR не запускает проверку независимой линии."""
        self.api.add_second_line()
        self.api.pulls[10]["body"] = main_commit() + links(12)
        self.api.pulls[12]["body"] = main_commit() + links(10)
        self.api.pulls[12]["state"] = "open"
        self.api.pulls[10]["merge_commit_sha"] = RELEASE_SHA
        self.api.merge(10, RELEASE_SHA)
        self.assertEqual(rerun.target_prs(self.api, event(self.api, 10)), ())

    def test_one_line_rerun_failure_does_not_block_another(self) -> None:
        """После main merge отказ одной линии не отменяет адресный rerun другой."""
        self.api.add_second_line()
        self.api.pulls[12].update({"state": "open", "head": {"sha": HEAD},
                                   "merge_commit_sha": TEST_MERGE})
        self.api.pulls[12]["base"]["sha"] = BASE
        self.api.merge(11, MAIN_SHA)

        def job(api, pr):
            """Имитирует отсутствие пригодного run только в первой линии."""
            if pr["number"] == 10:
                raise rerun.ControllerError("API первой линии недоступен")
            return 88

        with patch.object(rerun, "GitHub", return_value=self.api), patch.object(
            rerun, "require_repository"), patch.object(
            Path, "read_text", return_value=json.dumps(event(self.api, 11))), patch.object(
            rerun, "current_failed_job", side_effect=job), patch.object(
            rerun, "rerun_job") as submit:
            with self.assertRaises(SystemExit):
                rerun.main()
        submit.assert_called_once_with(self.api, 88)

    def test_mixed_green_failed_and_changed_targets(self) -> None:
        """Успех пропускается, failed перезапускается, изменившийся PR отклоняется."""
        targets = []
        for number in (10, 12, 13):
            pr = deepcopy(self.api.pulls[10])
            pr["number"] = number
            targets.append(pr)

        def fresh(api, number):
            """Меняет SHA только у третьей линии после первоначального снимка."""
            pr = deepcopy(next(item for item in targets if item["number"] == number))
            if number == 13:
                pr["head"]["sha"] = "9" * 40
            return pr

        with patch.object(rerun, "GitHub", return_value=self.api), patch.object(
            rerun, "require_repository"), patch.object(Path, "read_text", return_value="{}"), \
             patch.object(rerun, "target_prs", return_value=tuple(targets)), patch.object(
            rerun, "current_failed_job", side_effect=(None, 77, 77)), patch.object(
            rerun, "pull", side_effect=fresh), patch.object(rerun, "check_pr"), patch.object(
            rerun, "rerun_job") as submit:
            with self.assertRaises(SystemExit):
                rerun.main()
        submit.assert_called_once_with(self.api, 77)

    def test_pending_run_and_delayed_job_visibility_are_retryable(self) -> None:
        """Отсутствующий run/job и выполняющаяся проверка требуют ожидания."""
        pr = self.api.pulls[10]
        self.api.runs = []
        with self.assertRaises(rerun.PendingCheck):
            rerun.current_failed_job(self.api, pr)
        self.api.runs = [self.api.run(10, HEAD)]
        self.api.runs[0]["status"] = "in_progress"
        with self.assertRaises(rerun.PendingCheck):
            rerun.current_failed_job(self.api, pr)
        self.api.runs[0]["status"] = "completed"
        self.api.jobs = []
        with self.assertRaises(rerun.PendingCheck):
            rerun.current_failed_job(self.api, pr)
        self.api.jobs = [{"name": "hotfix-deps", "status": "in_progress"}]
        with self.assertRaises(rerun.PendingCheck):
            rerun.current_failed_job(self.api, pr)

    def test_pending_first_line_does_not_delay_ready_line(self) -> None:
        """Готовая линия перезапускается до паузы, pending — после завершения run."""
        targets = [deepcopy(self.api.pulls[10]) for _ in range(2)]
        targets[1]["number"] = 12
        operations = []
        checks = {10: 0}

        def job(api, pr):
            """Первый run появляется только после опроса второй линии."""
            if pr["number"] == 10:
                checks[10] += 1
                if checks[10] == 1:
                    raise rerun.PendingCheck("Run ещё не виден")
            return pr["number"]

        with patch.object(rerun, "GitHub", return_value=self.api), patch.object(
            rerun, "require_repository"), patch.object(Path, "read_text", return_value="{}"), \
             patch.object(rerun, "target_prs", return_value=tuple(targets)), patch.object(
            rerun, "pull", side_effect=lambda api, n: deepcopy(
                next(pr for pr in targets if pr["number"] == n))), patch.object(
            rerun, "check_pr"), patch.object(rerun, "current_failed_job", side_effect=job), \
             patch.object(rerun, "rerun_job", side_effect=lambda api, n: operations.append(n)), \
             patch.object(rerun.time, "sleep", side_effect=lambda _: operations.append("wait")):
            rerun.main()
        self.assertEqual(operations, [12, "wait", 10])

    def test_wait_timeout_is_bounded_and_never_posts(self) -> None:
        """Общий deadline завершает ожидание отсутствующего актуального run."""
        with patch.object(rerun, "GitHub", return_value=self.api), patch.object(
            rerun, "require_repository"), patch.object(Path, "read_text", return_value="{}"), \
             patch.object(rerun, "target_prs", return_value=(deepcopy(self.api.pulls[10]),)), \
             patch.object(rerun, "current_failed_job", side_effect=rerun.PendingCheck("Нет run")), \
             patch.object(rerun.time, "monotonic", side_effect=(0, 239, 240)), \
             patch.object(rerun.time, "sleep") as sleep, patch.object(rerun, "rerun_job") as submit:
            with self.assertRaises(SystemExit):
                rerun.main()
        sleep.assert_called_once_with(1)
        submit.assert_not_called()

    def test_pending_run_finishing_green_never_posts(self) -> None:
        """Завершившийся успешно pending run больше не требует rerun."""
        with patch.object(rerun, "GitHub", return_value=self.api), patch.object(
            rerun, "require_repository"), patch.object(Path, "read_text", return_value="{}"), \
             patch.object(rerun, "target_prs", return_value=(deepcopy(self.api.pulls[10]),)), \
             patch.object(rerun, "current_failed_job", side_effect=(rerun.PendingCheck("Pending"), None)), \
             patch.object(rerun.time, "sleep") as sleep, patch.object(rerun, "rerun_job") as submit:
            rerun.main()
        sleep.assert_called_once()
        submit.assert_not_called()

    def test_latest_job_change_is_rechecked_before_post(self) -> None:
        """Новый latest job исключает POST для уже выбранного устаревшего job."""
        with patch.object(rerun, "GitHub", return_value=self.api), patch.object(
            rerun, "require_repository"), patch.object(Path, "read_text", return_value="{}"), \
             patch.object(rerun, "target_prs", return_value=(deepcopy(self.api.pulls[10]),)), \
             patch.object(rerun, "check_pr"), patch.object(
            rerun, "current_failed_job", side_effect=(77, 88, 88, 88)), \
             patch.object(rerun.time, "sleep"), patch.object(rerun, "rerun_job") as submit:
            rerun.main()
        submit.assert_called_once_with(self.api, 88)

    def test_pr_mutation_during_wait_never_posts(self) -> None:
        """Изменение head/base/ref/merge/описания или закрытие отвергает снимок."""
        original = deepcopy(self.api.pulls[10])
        for field in ("head", "base", "ref", "merge_commit_sha", "body", "state", "merged_at"):
            with self.subTest(field=field):
                self.api.pulls[10] = deepcopy(original)

                def mutate(_):
                    """Меняет снимок PR в промежутке между двумя опросами."""
                    if field in ("head", "base"):
                        self.api.pulls[10][field]["sha"] = "9" * 40
                    elif field == "ref":
                        self.api.pulls[10]["base"]["ref"] = "release/99.0"
                    else:
                        self.api.pulls[10][field] = "changed"

                with patch.object(rerun, "GitHub", return_value=self.api), patch.object(
                    rerun, "require_repository"), patch.object(Path, "read_text", return_value="{}"), \
                     patch.object(rerun, "target_prs", return_value=(deepcopy(original),)), \
                     patch.object(rerun, "current_failed_job", side_effect=rerun.PendingCheck("Pending")), \
                     patch.object(rerun.time, "sleep", side_effect=mutate), \
                     patch.object(rerun, "rerun_job") as submit:
                    with self.assertRaises(SystemExit):
                        rerun.main()
                submit.assert_not_called()

    def test_stale_run_or_nonfailure_never_reruns(self) -> None:
        """Чужой head, старый base и success job не выбираются для POST."""
        pr = self.api.pulls[10]
        self.api.runs[0]["pull_requests"][0]["head"]["sha"] = "3" * 40
        with self.assertRaisesRegex(rerun.RerunError, "текущих head/base"):
            rerun.current_failed_job(self.api, pr)
        self.api.runs = [self.api.run(10, HEAD)]
        self.api.runs[0]["pull_requests"][0]["base"]["sha"] = "4" * 40
        with self.assertRaisesRegex(rerun.RerunError, "head/base"):
            rerun.current_failed_job(self.api, pr)
        self.api.runs = [self.api.run(10, HEAD)]
        self.api.jobs[0]["conclusion"] = "success"
        self.assertIsNone(rerun.current_failed_job(self.api, pr))

    def test_event_and_api_failure_close_selection(self) -> None:
        """Чужой repo и неподтверждённое слияние не выбирают job."""
        self.api.merge(11, MAIN_SHA)
        wrong = event(self.api, 11)
        wrong["repository"]["full_name"] = "other/repo"
        with self.assertRaisesRegex(rerun.RerunError, "Неверное событие"):
            rerun.target_prs(self.api, wrong)
        bad = event(self.api, 11)
        bad["pull_request"]["merge_commit_sha"] = NEXT_SHA
        with self.assertRaisesRegex(rerun.RerunError, "не совпадает"):
            rerun.target_prs(self.api, bad)
        self.api.error_path = "/pulls/11"
        with self.assertRaises(rerun.HotfixError):
            rerun.target_prs(self.api, event(self.api, 11))

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
