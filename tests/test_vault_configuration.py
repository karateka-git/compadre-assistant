"""Тесты обязательной пользовательской конфигурации правил vault."""

import unittest

from compadre_assistant.capabilities.obsidian.data.configuration.vault_configuration import parse_vault_configuration
from compadre_assistant.capabilities.obsidian.presentation.vault_configuration import load_vault_configuration_example
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_configuration import (
    VaultConfigurationError,
    VaultConfigurationErrorCode,
)


class VaultConfigurationTest(unittest.TestCase):
    """Проверяет строгий и безопасный контракт `.knowledge-catcher.yml`."""

    def test_project_example_contains_expected_instruction_files(self) -> None:
        """Поставляемый пример является валидной конфигурацией для текущего vault."""
        configuration = parse_vault_configuration(
            load_vault_configuration_example()
        )

        self.assertEqual(
            configuration.instruction_paths,
            (
                "memory-bank/AGENTS.md.txt",
                "memory-bank/docs/agent-preflight.md.txt",
                "memory-bank/docs/content-conventions.md.txt",
                "memory-bank/docs/note-types.md.txt",
                "memory-bank/docs/project-structure.md.txt",
                "memory-bank/docs/workflows.md.txt",
            ),
        )

    def test_parser_rejects_path_outside_vault(self) -> None:
        """Instruction-файл не может выйти за настроенный корень vault."""
        content = "version: 1\ninstructions:\n  - ../AGENTS.md\n"

        with self.assertRaises(VaultConfigurationError) as context:
            parse_vault_configuration(content)

        self.assertEqual(
            context.exception.code,
            VaultConfigurationErrorCode.INVALID,
        )

    def test_parser_rejects_unknown_fields_and_empty_instructions(self) -> None:
        """Опечатки и пустой набор правил не принимаются молча."""
        invalid_documents = (
            "version: 1\ninstructions: []\n",
            "version: 1\ninstruction:\n  - rules.txt\n",
            "version: 2\ninstructions:\n  - rules.txt\n",
        )

        for content in invalid_documents:
            with self.subTest(content=content):
                with self.assertRaises(VaultConfigurationError):
                    parse_vault_configuration(content)


if __name__ == "__main__":
    unittest.main()
