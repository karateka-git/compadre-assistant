"""Проверяет привязку и классификацию отчётов GitHub Actions."""

import json
import os
from pathlib import Path
import tempfile
import unittest

import yaml
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import ci_metrics


SHA = "a" * 40
REPO = "owner/repo"


def run(attempt=2):
    """Создаёт минимальный ответ run текущей попытки."""
    return {"id": 12, "run_attempt": attempt, "head_sha": SHA,
            "repository": {"full_name": REPO}, "path": ".github/workflows/main-full.yml",
            "head_branch": "main", "event": "push", "run_number": 12, "run_started_at": "2026-11-01T10:00:00Z"}


def job(name, conclusion, end="2026-11-01T10:01:00Z", run_id=12, attempt=2):
    """Создаёт job с одним шагом и точным SHA."""
    return {"id": len(name), "name": name, "head_sha": SHA,
            "run_id": run_id, "run_attempt": attempt,
            "conclusion": conclusion, "started_at": "2026-11-01T10:00:10Z",
            "completed_at": end, "steps": [{"name": "Check", "conclusion": conclusion,
                                            "started_at": "2026-11-01T10:00:20Z",
                                            "completed_at": end}]}


class MetricsTests(unittest.TestCase):
    """Проверяет полезные границы расчёта без сетевых запросов."""

    def test_failed_suite_is_full_and_preserved(self):
        """Отказ полного набора остаётся полным типом с измеренными шагами."""
        jobs = [job("pre-release-check-full-controller", "success"),
                job("pre-release-check-full / check-full-suite", "failure")]
        report = ci_metrics.summarize(run(), jobs, "main-full.yml", REPO, 12, 2, SHA)
        self.assertEqual((report["kind"], report["result"], report["seconds"]),
                         ("full-main", "failure", 60))
        self.assertEqual(report["jobs"][1]["steps"][0]["seconds"], 40)

    def test_skipped_suite_is_distinct(self):
        """Успешный controller с skipped suite не выглядит полным прогоном."""
        jobs = [job("pre-release-check-full-controller", "success"),
                job("pre-release-check-full", "skipped")]
        report = ci_metrics.summarize(run(), jobs, "main-full.yml", REPO, 12, 2, SHA)
        self.assertEqual((report["kind"], report["result"]), ("skipped", "success"))

    def test_wrong_attempt_or_sha_rejected(self):
        """История другой попытки и job другого SHA не загрязняют отчёт."""
        jobs = [job("pre-release-check-full-controller", "success"),
                job("pre-release-check-full / check-full-suite", "success")]
        with self.assertRaises(ci_metrics.MetricsError):
            ci_metrics.summarize(run(1), jobs, "main-full.yml", REPO, 12, 2, SHA)
        jobs[0]["head_sha"] = "b" * 40
        with self.assertRaises(ci_metrics.MetricsError):
            ci_metrics.summarize(run(), jobs, "main-full.yml", REPO, 12, 2, SHA)

    def test_missing_timestamp_is_unknown(self):
        """Отсутствующую границу нельзя заменять нулевой длительностью."""
        self.assertIsNone(ci_metrics.duration("2026-11-01T10:00:00Z", None))
        with self.assertRaises(ci_metrics.MetricsError):
            ci_metrics.duration("2026-11-01T10:00:01Z", "2026-11-01T10:00:00Z")

    def test_previous_uses_only_same_kind(self):
        """Сравнение пропускает skipped и выбирает успешный full-main."""
        current = ci_metrics.summarize(
            run(), [job("pre-release-check-full-controller", "success"),
                    job("pre-release-check-full / check-full-suite", "success")],
            "main-full.yml", REPO, 12, 2, SHA)
        with self.assertRaises(ci_metrics.MetricsError):
            ci_metrics.summarize(run(), [job("main-full / full-suite", "success")],
                                 "main-full.yml", REPO, 12, 2, SHA)

        class FakeApi:
            """Возвращает старое имя, пропущенный и полный прогоны."""

            def get(self, path, **params):
                """Возвращает историю без обращения к сети."""
                return {"workflow_runs": [
                    {**run(1), "id": 11, "run_number": 11, "status": "completed"},
                    {**run(1), "id": 10, "run_number": 10, "status": "completed"},
                    {**run(1), "id": 9, "run_number": 9, "status": "completed"}]}

            def jobs(self, run_id, attempt):
                """Возвращает jobs заданного run."""
                if run_id == 11:
                    return [job("main-full / full-suite", "success", run_id=run_id, attempt=attempt)]
                return [job("pre-release-check-full-controller", "success", run_id=run_id, attempt=attempt),
                        job("pre-release-check-full" if run_id == 10 else "pre-release-check-full / check-full-suite",
                            "skipped" if run_id == 10 else "success", run_id=run_id, attempt=attempt)]

        previous = ci_metrics.comparable_previous(FakeApi(), current)
        self.assertEqual(previous["run_id"], 9)
        self.assertIn("+0 с", ci_metrics.render(current, previous))

    def test_api_error_does_not_echo_response_or_token(self):
        """Причина API отказа содержит лишь HTTP статус."""
        api = ci_metrics.GitHub(REPO, "private-token")
        with patch.object(ci_metrics, "urlopen", side_effect=HTTPError(
                "https://example.test/private-token", 403, "secret", {}, None)):
            with self.assertRaisesRegex(ci_metrics.MetricsError, "HTTP 403") as caught:
                api.get("/actions/runs/12")
        self.assertNotIn("private-token", str(caught.exception))

    def test_previous_failure_is_not_compared_to_success(self):
        """Быстрый отказ нельзя выдавать за ускорение успешного полного набора."""
        current = ci_metrics.summarize(run(), [job("pre-release-check-full / check-full-suite", "success")],
                                       "main-full.yml", REPO, 12, 2, SHA)
        with patch.object(ci_metrics.GitHub, "get", return_value={"workflow_runs": [
            {**run(1), "id": 11, "run_number": 11, "status": "completed"},
        ]}), patch.object(ci_metrics.GitHub, "jobs", return_value=[
            job("pre-release-check-full / check-full-suite", "failure", run_id=11, attempt=1),
        ]):
            self.assertIsNone(ci_metrics.comparable_previous(ci_metrics.GitHub(REPO, "token"), current))

    def test_history_failure_preserves_current_summary(self):
        """Недоступная история не скрывает измерения и отказ текущей suite."""
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            env = {"GITHUB_REPOSITORY": REPO, "CI_WORKFLOW": "main-full.yml",
                   "GITHUB_RUN_ID": "12", "GITHUB_RUN_ATTEMPT": "2",
                   "GITHUB_SHA": SHA, "GITHUB_TOKEN": "private-token"}
            with patch.dict(os.environ, env, clear=True), patch("sys.argv", [
                "ci_metrics", "--output", str(output),
            ]), patch.object(ci_metrics.GitHub, "get", side_effect=[
                run(), ci_metrics.MetricsError("GitHub API: HTTP 403"),
            ]), patch.object(ci_metrics.GitHub, "jobs", return_value=[
                job("pre-release-check-full / check-full-suite", "failure"),
            ]):
                with self.assertRaises(SystemExit) as caught:
                    ci_metrics.main()
            self.assertEqual(caught.exception.code, 0)
            summary = (output / "ci-metrics.md").read_text(encoding="utf-8")
            self.assertIn("`failure`", summary)
            self.assertIn("Сравнение недоступно", summary)
            self.assertNotIn("private-token", summary)

    def test_pr_type_requires_pr_job(self):
        """Короткий тип не присваивается чужому набору jobs."""
        with self.assertRaises(ci_metrics.MetricsError):
            ci_metrics.classify("pr-fast.yml", [job("pr-merge-main-check", "success")])


    def test_pr_preserves_distinct_head_and_job_sha(self):
        """SHA merge не смешивается с head PR; чужая попытка отвергается."""
        pr_run = {**run(), "event": "pull_request", "path": ".github/workflows/pr-fast.yml"}
        pr_job = {**job("pr-check", "success"), "head_sha": "b" * 40}
        report = ci_metrics.summarize(pr_run, [pr_job], "pr-fast.yml", REPO, 12, 2, SHA)
        self.assertEqual(report["sha"], SHA)
        self.assertEqual(report["jobs"][0]["sha"], "b" * 40)
        for field, wrong in [("run_id", 11), ("run_attempt", 1), ("head_sha", "bad")]:
            with self.subTest(field=field), self.assertRaises(ci_metrics.MetricsError):
                ci_metrics.summarize(pr_run, [{**pr_job, field: wrong}],
                                     "pr-fast.yml", REPO, 12, 2, SHA)


    def test_skipped_negative_timestamp_and_carried_over_job(self):
        """Особенности реальных skipped/rerun ответов не ломают отчёт."""
        pr_run = {**run(), "event": "pull_request", "path": ".github/workflows/pr-fast.yml"}
        jobs = [job("pr-check", "success"),
                job("pr-merge-main-check", "skipped", end="2026-11-01T10:00:00Z")]
        report = ci_metrics.summarize(pr_run, jobs, "pr-fast.yml", REPO, 12, 2, SHA)
        self.assertIsNone(report["jobs"][1]["seconds"])
        pr_run["run_started_at"] = "2026-11-01T10:02:00Z"
        report = ci_metrics.summarize(pr_run, jobs, "pr-fast.yml", REPO, 12, 2, SHA)
        self.assertTrue(report["jobs"][0]["carried_over"])
        self.assertIsNone(report["seconds"])
        self.assertIn("из прежней попытки", ci_metrics.render(report))

    def test_cli_failed_suite_keeps_measurements(self):
        """Неуспешная suite даёт полноценный отчёт без замены её результата."""
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report"
            env = {"GITHUB_REPOSITORY": REPO, "CI_WORKFLOW": "main-full.yml",
                   "GITHUB_RUN_ID": "12", "GITHUB_RUN_ATTEMPT": "2",
                   "GITHUB_SHA": SHA, "GITHUB_TOKEN": "private-token"}
            with patch.dict(os.environ, env, clear=True), patch("sys.argv", [
                    "ci_metrics", "--output", str(output)]), patch.object(
                    ci_metrics.GitHub, "get", side_effect=[run(), {"workflow_runs": []}]) as get, patch.object(
                    ci_metrics.GitHub, "jobs", return_value=[job("pre-release-check-full / check-full-suite", "failure")]):
                with self.assertRaises(SystemExit) as caught:
                    ci_metrics.main()
            self.assertEqual(caught.exception.code, 0)
            self.assertEqual(get.call_args_list[0].args[0], "/actions/runs/12/attempts/2")
            report = json.loads((output / "ci-metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "failure")
            self.assertEqual(report["checkout_sha"], SHA)
            self.assertTrue((output / "ci-metrics.md").is_file())

    def test_cli_api_failure_leaves_safe_artifact_and_summary(self):
        """Ошибка API видна в exit code, artifact и Step Summary."""
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report"
            summary = Path(temp) / "summary.md"
            env = {"GITHUB_REPOSITORY": REPO, "CI_WORKFLOW": "main-full.yml",
                   "GITHUB_RUN_ID": "12", "GITHUB_RUN_ATTEMPT": "2",
                   "GITHUB_SHA": SHA, "GITHUB_TOKEN": "private-token",
                   "GITHUB_STEP_SUMMARY": str(summary)}
            with patch.dict(os.environ, env, clear=True), patch("sys.argv", [
                    "ci_metrics", "--output", str(output)]), patch.object(
                    ci_metrics.GitHub, "get", side_effect=ci_metrics.MetricsError("GitHub API: HTTP 403")):
                with self.assertRaises(SystemExit) as caught:
                    ci_metrics.main()
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("HTTP 403", summary.read_text(encoding="utf-8"))
            self.assertNotIn("private-token", summary.read_text(encoding="utf-8"))
            self.assertEqual(json.loads((output / "ci-metrics-error.json").read_text(
                encoding="utf-8"))["error"], "GitHub API: HTTP 403")

    def test_workflows_collect_after_all_checks_even_on_failure(self):
        """Реальные YAML сохраняют отчёт после failed/skipped проверок."""
        root = Path(__file__).resolve().parents[1]
        workflows = root / ".github/workflows"
        suite = yaml.safe_load((workflows / "full-suite.yml").read_text(encoding="utf-8"))
        self.assertEqual(suite["name"], "check-full-suite")
        self.assertEqual(suite["jobs"]["full-suite"]["name"], "check-full-suite")
        self.assertEqual(suite["jobs"]["repository-compatibility"]["name"],
                         "check-full-repository-compatibility-check")
        for workflow, label, job_names in (
            ("main-full", "pre-release-check-full", {"controller": "pre-release-check-full-controller",
                                                      "full-suite": "pre-release-check-full"}),
            ("release-full", "release-check-full", {"controller": "release-check-full-controller",
                                                    "full-suite": "release-check-full"}),
            ("pr-fast", "pr-check", {"pr-fast": "pr-check", "hotfix-deps": "pr-merge-main-check"}),
        ):
            with self.subTest(workflow=workflow):
                data = yaml.safe_load((workflows / (workflow + ".yml")).read_text(encoding="utf-8"))
                self.assertEqual(data["name"], label)
                self.assertEqual({job: data["jobs"][job]["name"] for job in job_names}, job_names)
                metrics = data["jobs"]["ci-metrics"]
                self.assertEqual(metrics["needs"], list(job_names))
                self.assertEqual(metrics["if"], "${{ always() }}")
                self.assertEqual(metrics["permissions"], {"actions": "read", "contents": "read"})
                steps = {step["name"]: step for step in metrics["steps"]}
                self.assertEqual(steps["Upload CI metrics"]["if"], "${{ always() }}")
                self.assertEqual(steps["Preserve collection failure"]["if"], "${{ always() }}")
                self.assertEqual(steps["Collect CI metrics"]["env"]["CI_WORKFLOW"], workflow + ".yml")
                if workflow == "pr-fast":
                    self.assertEqual(steps["Checkout metrics collector"]["with"]["ref"],
                                     "${{ github.event.pull_request.base.sha }}")
                    self.assertEqual(steps["Collect CI metrics"]["if"],
                                     "steps.collector.outputs.available == 'true'")
                    availability = steps["Check trusted collector availability"]
                    self.assertNotIn("GITHUB_TOKEN", availability.get("env", {}))
                    self.assertIn("collector_missing_in_base", availability["run"])


if __name__ == "__main__":
    unittest.main()
