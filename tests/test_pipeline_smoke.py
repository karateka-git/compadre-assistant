"""Тесты постоянного smoke-сценария article pipeline."""

import unittest

from compadre_assistant.capabilities.obsidian.smoke import run_pipeline_smoke


class PipelineSmokeTest(unittest.TestCase):
    """Проверяет pipeline на временной базе без сети и `trafilatura`."""

    def test_pipeline_smoke_succeeds(self) -> None:
        """Smoke-сценарий article pipeline завершается без ошибок."""
        run_pipeline_smoke()


if __name__ == "__main__":
    unittest.main()
