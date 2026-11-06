"""Проверяет подготовку изолированной среды при hosted system Python."""

from pathlib import Path
import unittest
from unittest.mock import patch

from tests import compatibility_test_environment as environment


class CompatibilityEnvironmentTests(unittest.TestCase):
    """Не позволяет worker-тестам молча использовать hosted site-packages."""

    def test_system_python_creates_and_reuses_isolated_environment(self):
        """Создаёт один venv без system-site и ставит requirements/pip check."""
        with patch.object(environment, "_python", None), patch.object(environment, "_temporary", None), patch.object(
                environment.sys, "prefix", environment.sys.base_prefix), patch.object(environment.subprocess, "run") as run:
            first = environment.worker_python()
            self.assertEqual(first, environment.worker_python())
            self.assertEqual(run.call_count, 3)
            creation, install, check = [call.args[0] for call in run.call_args_list]
            self.assertIn("venv", creation)
            self.assertNotIn("--system-site-packages", creation)
            self.assertEqual(install[0], str(first))
            self.assertIn("--isolated", install)
            self.assertEqual(Path(install[-1]).name, "requirements.txt")
            self.assertEqual(check[-1], "check")
            self.assertTrue(all("-I" in args and "-B" in args for args in (creation, install, check)))
