"""Тесты smoke-сценария LLM-анализа."""

import unittest

from compadre_assistant.capabilities.obsidian.smoke import run_analysis_smoke


class AnalysisSmokeTest(unittest.TestCase):
    """Проверяет analysis pipeline на временной базе и fake LLM."""

    def test_analysis_smoke_succeeds(self) -> None:
        """Smoke-сценарий анализа завершается без ошибок."""
        run_analysis_smoke()


if __name__ == "__main__":
    unittest.main()
