"""Проверяет полноту и границы технического отчёта попытки."""

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.deploy_summary import AttemptSummary, TIMING_PREFIX


class AttemptSummaryTests(unittest.TestCase):
    """Различает измеренный ноль, неполный SSH и несостоявшуюся запись отчёта."""

    def test_missing_report_and_measured_zero(self):
        """Утрата одного отчёта делает общую сумму неизвестной даже при успешном resume."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with patch.dict(os.environ, DEPLOY_SUMMARY_PATH=str(path), GITHUB_RUN_ID="12", GITHUB_RUN_ATTEMPT="1"):
                summary = AttemptSummary.start("v4.4.1", "a" * 40, "patch")
                summary.observe_ssh({"http": []})
                summary.finish([])
                self.assertEqual(json.loads(path.read_bytes())["api"], 0)
                other = AttemptSummary.start("v4.4.1", "a" * 40, "resume")
                other.observe_ssh(None)
                other.finish([{"target": "github_api", "attempts": 2}])
                self.assertIsNone(json.loads(path.read_bytes())["api"])

    def test_old_controller_and_write_failure(self):
        """Отсутствие дополнительной метки и ошибка записи не прерывают обработку выпуска."""
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()) as log:
            with patch.dict(os.environ, DEPLOY_SUMMARY_PATH=str(Path(directory) / "missing" / "report.json")):
                summary = AttemptSummary.start("v4.4.1", "a" * 40, "patch")
                summary.installation()
                summary.timing(TIMING_PREFIX + '{"sha":"foreign","downtime_ms":0}')
                self.assertIsNotNone(summary.installed_from)
                self.assertIsNone(summary.downtime_ms)
                summary.finish([])
                self.assertIn("Не удалось сохранить", log.getvalue())
