"""Проверяет read-only допуск CI по искусственным ответам GitHub API."""

from __future__ import annotations

from copy import deepcopy
import unittest

from scripts import full_ci_admission as admission
from tests.test_full_ci_controller import SHA_D, admission_jobs, admission_run


class FakeApi:
    """Отдаёт точные ответы workflow, runs и jobs выбранной попытки."""

    repository = "owner/repo"

    def __init__(self) -> None:
        """Создаёт один полный успешный run main-full."""
        self.workflow = {"id": 1, "path": ".github/workflows/main-full.yml"}
        self.runs = [admission_run(SHA_D)]
        self.jobs = admission_jobs(SHA_D)
        self.requested_jobs = []
        self.error = None

    def get(self, path, **parameters):
        """Возвращает workflow metadata или имитирует ошибку API."""
        if self.error:
            raise self.error
        if path.endswith("/main-full.yml") or path.endswith("/release-full.yml"):
            return self.workflow
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Возвращает только запрошенный список API."""
        if self.error:
            raise self.error
        if path.endswith("/runs"):
            return self.runs
        if path.endswith("/jobs"):
            self.requested_jobs.append(path)
            return self.jobs
        raise AssertionError(path)


class AdmissionTests(unittest.TestCase):
    """Проверяет точность идентичности и полный успех одной попытки."""

    def setUp(self) -> None:
        """Изолирует изменяемые ответы API для каждого сценария."""
        self.api = FakeApi()

    def select(self):
        """Выбирает полный main-full для стандартного SHA."""
        return admission.select_successful_run(
            self.api, "owner/repo", admission.MAIN_WORKFLOW, SHA_D, "main")

    def test_exact_success_returns_run_and_attempt(self) -> None:
        """Успех фиксирует полный SHA и номер текущей попытки."""
        result = self.select()
        self.assertEqual((result.sha, result.run_id, result.run_attempt), (SHA_D, 1, 1))
        self.assertEqual(self.api.requested_jobs, ["/actions/runs/1/attempts/1/jobs"])

    def test_github_run_path_without_ref_is_accepted(self) -> None:
        """GitHub может вернуть только путь файла workflow без @ref."""
        self.api.runs[0]["path"] = ".github/workflows/main-full.yml"
        self.assertIsNotNone(self.select())

    def test_release_branch_requires_own_full_run(self) -> None:
        """Изменённая release ветка допускается собственным release-full."""
        self.api.workflow = {"id": 2, "path": ".github/workflows/release-full.yml"}
        run = self.api.runs[0]
        run.update({"workflow_id": 2, "head_branch": "release/1.2",
                    "path": ".github/workflows/release-full.yml@refs/heads/release/1.2"})
        self.api.jobs[0]["name"] = "release-full-controller"
        self.api.jobs[0]["steps"][0]["name"] = "Checkout exact release commit"
        self.api.jobs[1]["name"] = "release-full / full-suite"
        self.assertIsNotNone(admission.select_successful_run(
            self.api, "owner/repo", admission.RELEASE_WORKFLOW, SHA_D, "release/1.2"))
        self.assertIsNone(admission.select_successful_run(
            self.api, "owner/repo", admission.RELEASE_WORKFLOW, SHA_D, "release/1.3"))

    def test_wrong_identity_or_incomplete_result_is_denied(self) -> None:
        """Чужие repo, workflow, ref и частичный результат не создают допуск."""
        original_run = deepcopy(self.api.runs[0])
        original_jobs = deepcopy(self.api.jobs)
        changes = [
            ("head_sha", "a" * 40), ("head_branch", "release/1.2"),
            ("workflow_id", 9), ("path", ".github/workflows/other.yml@main"),
            ("event", "pull_request"), ("status", "queued"),
            ("conclusion", "cancelled"),
        ]
        for key, value in changes:
            with self.subTest(key=key):
                self.api.runs = [dict(original_run, **{key: value})]
                self.assertIsNone(self.select())
        for key in ("repository", "head_repository"):
            with self.subTest(key=key):
                self.api.runs = [dict(original_run, **{key: {"full_name": "other/repo"}})]
                self.assertIsNone(self.select())
        self.api.runs = [original_run]
        for job_index, key, value in (
            (0, "conclusion", "failure"), (1, "status", "in_progress"),
            (1, "head_sha", "a" * 40), (1, "name", "other / full-suite"),
        ):
            with self.subTest(job=job_index, key=key):
                self.api.jobs = deepcopy(original_jobs)
                self.api.jobs[job_index][key] = value
                self.assertIsNone(self.select())
        for conclusion in ("failure", "skipped", "cancelled", None):
            with self.subTest(step=conclusion):
                self.api.jobs = deepcopy(original_jobs)
                self.api.jobs[1]["steps"][0]["conclusion"] = conclusion
                self.assertIsNone(self.select())

    def test_rerun_reads_only_current_attempt(self) -> None:
        """После rerun selector не принимает jobs из первой попытки."""
        self.api.runs[0]["run_attempt"] = 2
        self.api.jobs[1]["conclusion"] = "failure"
        self.assertIsNone(self.select())
        self.assertEqual(self.api.requested_jobs, ["/actions/runs/1/attempts/2/jobs"])
        self.api.jobs = admission_jobs(SHA_D)
        self.assertEqual(self.select().run_attempt, 2)

    def test_pinned_run_and_attempt_do_not_select_another_success(self) -> None:
        """Манифест выпуска нельзя заменить другим успешным запуском того же SHA."""
        self.assertIsNotNone(admission.select_successful_run(
            self.api, "owner/repo", admission.MAIN_WORKFLOW, SHA_D, "main", 1, 1))
        self.assertIsNone(admission.select_successful_run(
            self.api, "owner/repo", admission.MAIN_WORKFLOW, SHA_D, "main", 2, 1))
        self.assertIsNone(admission.select_successful_run(
            self.api, "owner/repo", admission.MAIN_WORKFLOW, SHA_D, "main", 1, 2))

    def test_invalid_api_response_fails_closed(self) -> None:
        """Неверная структура API вызывает ошибку и не выдаёт допуск."""
        self.api.workflow["path"] = ".github/workflows/other.yml"
        with self.assertRaises(admission.AdmissionError):
            self.select()
        self.api.workflow["path"] = ".github/workflows/main-full.yml"
        self.api.jobs[1]["steps"] = None
        with self.assertRaises(admission.AdmissionError):
            self.select()
        self.api.error = admission.AdmissionError("GitHub API недоступен")
        with self.assertRaises(admission.AdmissionError):
            self.select()

    def test_invalid_request_fails_closed(self) -> None:
        """Короткий SHA и посторонняя ветка отвергаются до API."""
        with self.assertRaises(admission.AdmissionError):
            admission.select_successful_run(self.api, "owner/repo", admission.MAIN_WORKFLOW, SHA_D[:8], "main")
        with self.assertRaises(admission.AdmissionError):
            admission.select_successful_run(self.api, "owner/repo", admission.MAIN_WORKFLOW, SHA_D, "feature/test")
        with self.assertRaises(admission.AdmissionError):
            admission.select_successful_run(self.api, "other/repo", admission.MAIN_WORKFLOW, SHA_D, "main")


if __name__ == "__main__":
    unittest.main()
