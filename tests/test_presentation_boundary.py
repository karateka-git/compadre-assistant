"""Проверки зависимости общего входа и каналов от публичного контракта."""

import ast
from pathlib import Path
import unittest

from compadre_assistant.capabilities.obsidian.presentation.processing import should_acknowledge


ROOT = Path(__file__).resolve().parents[1] / "compadre_assistant"


class PresentationBoundaryTest(unittest.TestCase):
    """Проверяет разделение предметных и транспортных обязанностей."""

    def test_domain_imports_only_standard_library_and_domain(self) -> None:
        """Чистые сценарии и ports не импортируют SDK, SQL или конкретные слои."""
        import sys

        for legacy in (ROOT / "application", ROOT / "domain", ROOT / "data", ROOT / "capabilities" / "obsidian" / "application"):
            self.assertFalse(legacy.exists(), str(legacy))

        for directory in (ROOT / "core" / "domain", ROOT / "capabilities" / "obsidian" / "domain"):
            for path in directory.rglob("*.py"):
                with self.subTest(path=str(path.relative_to(ROOT))):
                    syntax = ast.parse(path.read_text(encoding="utf-8"))
                    for node in ast.walk(syntax):
                        modules = []
                        if isinstance(node, ast.ImportFrom) and node.module:
                            modules = [node.module]
                        elif isinstance(node, ast.Import):
                            modules = [alias.name for alias in node.names]
                        for module in modules:
                            if module.startswith("compadre_assistant."):
                                self.assertIn("domain", module.split("."))
                                self.assertNotIn("data", module.split("."))
                                self.assertNotIn("presentation", module.split("."))
                            elif not isinstance(node, ast.ImportFrom) or node.level == 0:
                                self.assertIn(module.split(".")[0], sys.stdlib_module_names)
                                self.assertNotEqual(module.split(".")[0], "sqlite3")

    def test_all_core_layers_are_independent_from_capabilities(self) -> None:
        """Ядро не знает конкретную возможность, даже при выборе режима."""
        for path in (ROOT / "core").rglob("*.py"):
            with self.subTest(path=str(path.relative_to(ROOT))):
                syntax = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(syntax):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse(node.module and node.module.startswith("compadre_assistant.capabilities"))
                    elif isinstance(node, ast.Import):
                        self.assertFalse(any(alias.name.startswith("compadre_assistant.capabilities") for alias in node.names))

    def test_core_and_channels_do_not_import_obsidian(self) -> None:
        """Общий вход и адаптеры не зависят от внутренних модулей Obsidian."""
        paths = (
            ROOT / "core" / "presentation" / "incoming" / "dispatcher.py",
            ROOT / "core" / "presentation" / "incoming" / "commands.py",
            ROOT / "core" / "presentation" / "responses.py",
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
                    any(module and module.startswith("compadre_assistant.capabilities") for module in imports)
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
            any(module and module.startswith("compadre_assistant.capabilities") for module in modules)
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
            and node.module.startswith("compadre_assistant.capabilities.obsidian")
        }
        self.assertEqual(
            modules,
            {
                "compadre_assistant.capabilities.obsidian.bootstrap",
                "compadre_assistant.capabilities.obsidian.smoke",
            },
        )


if __name__ == "__main__":
    unittest.main()
