"""Проверки зависимости общего входа и каналов от публичного контракта."""

import ast
from pathlib import Path
import unittest

from obs_chat_bot.capabilities.obsidian.presentation.processing import should_acknowledge


ROOT = Path(__file__).resolve().parents[1] / "obs_chat_bot"


class PresentationBoundaryTest(unittest.TestCase):
    """Проверяет разделение предметных и транспортных обязанностей."""

    def test_core_and_channels_do_not_import_obsidian(self) -> None:
        """Общий вход и адаптеры не зависят от внутренних модулей Obsidian."""
        paths = (
            ROOT / "application" / "incoming" / "processing.py",
            ROOT / "application" / "incoming" / "commands.py",
            ROOT / "presentation" / "shared" / "responses.py",
            ROOT / "presentation" / "telegram" / "bot.py",
            ROOT / "presentation" / "vk" / "bot.py",
        )
        for path in paths:
            with self.subTest(path=path.name):
                syntax = ast.parse(path.read_text(encoding="utf-8"))
                imports = (
                    node.module
                    for node in ast.walk(syntax)
                    if isinstance(node, ast.ImportFrom)
                )
                self.assertFalse(
                    any(module and module.startswith("obs_chat_bot.capabilities") for module in imports)
                )

    def test_obsidian_ack_predicate_preserves_existing_triggers(self) -> None:
        """URL и повторный анализ по-прежнему дают ранний ответ канала."""
        self.assertTrue(should_acknowledge(" https://example.com/article "))
        self.assertTrue(should_acknowledge("/reanalyze 42"))
        self.assertFalse(should_acknowledge("/help"))
        self.assertFalse(should_acknowledge("обычный текст"))

    def test_cli_uses_composition_root_for_obsidian(self) -> None:
        """CLI не импортирует внутренние реализации Obsidian."""
        path = ROOT / "presentation" / "cli" / "main.py"
        syntax = ast.parse(path.read_text(encoding="utf-8"))
        modules = (
            node.module
            for node in ast.walk(syntax)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            any(module and module.startswith("obs_chat_bot.capabilities") for module in modules)
        )

    def test_root_imports_only_obsidian_assembly(self) -> None:
        """Общий composition root видит сборку возможности, а не её слои."""
        path = ROOT / "bootstrap.py"
        syntax = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(syntax)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("obs_chat_bot.capabilities.obsidian")
        }
        self.assertEqual(
            modules,
            {
                "obs_chat_bot.capabilities.obsidian.bootstrap",
                "obs_chat_bot.capabilities.obsidian.smoke",
            },
        )


if __name__ == "__main__":
    unittest.main()
