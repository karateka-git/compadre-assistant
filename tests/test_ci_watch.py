"""Проверяет живое предупреждение CI без подмены исхода команды."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts import ci_watch


class WatchTests(unittest.TestCase):
    """Проверяет реальные дочерние процессы и неизменность контрактов CI."""

    def test_warning_precedes_completion_and_failure_code_survives(self):
        """Команда завершается только после предупреждения, сохраняя код отказа."""
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "warning"
            output = io.StringIO()
            emit = ci_watch._warn

            def warn(stage, elapsed):
                """Разрешает дочернему процессу завершиться после живого сообщения."""
                emit(stage, elapsed)
                marker.touch()

            command = [sys.executable, "-c",
                       "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                       "end=time.monotonic()+5\n"
                       "while not p.exists() and time.monotonic()<end: time.sleep(.01)\n"
                       "sys.exit(37 if p.exists() else 99)", str(marker), "SECRET-ARG"]
            with patch.object(ci_watch, "WARNING_SECONDS", 0.02), patch.object(
                ci_watch, "_warn", side_effect=warn,
            ), patch.dict(os.environ, {"PRIVATE_TOKEN": "SECRET-ENV"}), redirect_stdout(output):
                result = ci_watch.run(command, "unit")
            self.assertEqual(result, 37)
            self.assertIn("reason=long_stage", output.getvalue())
            self.assertNotIn("SECRET", output.getvalue())
            self.assertNotIn(str(marker), output.getvalue())

    def test_fast_success_is_quiet_and_handlers_restored(self):
        """Быстрый успех не создаёт предупреждение и не оставляет signal handlers."""
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(ci_watch, "_warn") as warn:
            self.assertEqual(ci_watch.run([sys.executable, "-c", "pass"], "unit"), 0)
        warn.assert_not_called()
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})

    def test_broken_warning_output_does_not_fail_command(self):
        """Закрытый stdout наблюдателя не меняет успешный исход команды."""
        with patch.object(ci_watch, "WARNING_SECONDS", 0.01), patch(
            "builtins.print", side_effect=BrokenPipeError,
        ) as emit:
            result = ci_watch.run([sys.executable, "-c", "import time; time.sleep(.1)"], "unit")
        self.assertEqual(result, 0)
        self.assertTrue(emit.called)

    def test_cli_preserves_exit_code(self):
        """Реальная CLI-обёртка возвращает shell исходный код команды."""
        result = subprocess.run([sys.executable, "-m", "scripts.ci_watch", "unit", "--",
                                 sys.executable, "-c", "raise SystemExit(23)"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_signal_is_forwarded_and_shell_status_preserved(self):
        """Отмена пересылается дочернему процессу, отрицательный код не теряется."""
        handlers = {}

        def register(sig, handler):
            """Перехватывает регистрацию, позволяя имитировать сигнал runner."""
            handlers[sig] = handler
            return signal.SIG_DFL

        with patch.object(ci_watch.subprocess, "Popen") as popen, patch.object(
            ci_watch.signal, "signal", side_effect=register,
        ):
            process = popen.return_value.__enter__.return_value

            def wait(**kwargs):
                """Доставляет сигнал во время ожидания дочернего процесса."""
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                return -signal.SIGTERM

            process.wait.side_effect = wait
            self.assertEqual(ci_watch.run(["fixed-command"], "unit"), -signal.SIGTERM)
            process.send_signal.assert_called_once_with(signal.SIGTERM)
        with patch.object(ci_watch, "run", return_value=-15), patch(
            "sys.argv", ["ci_watch", "unit", "--", "fixed-command"],
        ), self.assertRaises(SystemExit) as caught:
            ci_watch.main()
        self.assertEqual(caught.exception.code, 143)

    def test_workflows_keep_admission_ids_and_job_timeouts(self):
        """Наблюдение охватывает долгие команды, сохраняя ids и deadlines допуска."""
        root = Path(__file__).resolve().parents[1]
        full = yaml.safe_load((root / ".github/workflows/full-suite.yml").read_text(encoding="utf-8"))
        pr = yaml.safe_load((root / ".github/workflows/pr-fast.yml").read_text(encoding="utf-8"))
        self.assertEqual(full["jobs"]["full-suite"]["timeout-minutes"], 45)
        self.assertEqual(pr["jobs"]["pr-fast"]["timeout-minutes"], 10)
        steps = {step.get("id"): step for step in full["jobs"]["full-suite"]["steps"]}
        for step_id in ("unit", "smokes", "migrations", "candidate"):
            self.assertIn("python -m scripts.ci_watch", steps[step_id]["run"])
            self.assertNotIn("continue-on-error", steps[step_id])
        for job in (full["jobs"]["full-suite"], pr["jobs"]["pr-fast"]):
            install = next(step for step in job["steps"] if step["name"] == "Install runtime dependencies")
            self.assertIn("ci_watch dependencies", install["run"])
        fixed = next(step for step in pr["jobs"]["pr-fast"]["steps"] if step["name"] == "Fixed unit subset")
        self.assertEqual(fixed["run"].count("ci_watch unit"), 3)


if __name__ == "__main__":
    unittest.main()
