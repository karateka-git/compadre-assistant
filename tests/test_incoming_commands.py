"""Тесты единого реестра пользовательских chat-команд."""

import unittest

from compadre_assistant.core.presentation.incoming.commands import (
    ChatCommand,
    CommandSection,
    ParsedChatCommand,
)
from compadre_assistant.capabilities.obsidian.presentation.commands import (
    ObsidianCommand,
    ParsedObsidianCommand,
    help_sections,
)


class IncomingCommandsTest(unittest.TestCase):
    """Проверяет metadata команд и точный общий parser."""

    def test_each_command_has_help_metadata_and_markdown_representation(self) -> None:
        """Каждая команда знает раздел, usage и пользовательское описание."""
        for command in ChatCommand:
            with self.subTest(command=command.value):
                self.assertIsInstance(command.section, CommandSection)
                self.assertTrue(command.description)
                self.assertIn(command.value, command.usage)
                self.assertEqual(
                    str(command),
                    f"`{command.usage}` — {command.description}",
                )

    def test_parser_returns_typed_command_and_raw_arguments(self) -> None:
        """Parser отделяет точное имя команды от строки аргументов."""
        parsed = ParsedChatCommand.parse("/link ABC123")

        self.assertEqual(parsed.command, ChatCommand.LINK)
        self.assertEqual(parsed.arguments, "ABC123")

        name = ParsedChatCommand.parse("/name Влад Ерофеев")
        self.assertEqual(name.command, ChatCommand.NAME)
        self.assertEqual(name.arguments, "Влад Ерофеев")

    def test_parser_does_not_confuse_commands_with_common_prefix(self) -> None:
        """`/link_code` не распознаётся как `/link`, а неизвестный prefix отвергается."""
        self.assertEqual(
            ParsedChatCommand.parse("/link_code").command,
            ChatCommand.LINK_CODE,
        )
        self.assertIsNone(ParsedChatCommand.parse("/link_extra ABC123"))
        self.assertIsNone(ParsedChatCommand.parse("/github_connect"))
        self.assertIsNone(
            ParsedChatCommand.parse(
                "/github_vault https://github.com/octocat/notes"
            )
        )
        self.assertIsNone(ParsedChatCommand.parse("обычный текст"))

    def test_obsidian_commands_do_not_enter_common_parser(self) -> None:
        """Предметные команды распознаёт только parser возможности."""
        for command in ObsidianCommand:
            with self.subTest(command=command.value):
                self.assertIsNone(ParsedChatCommand.parse(command.value))
                self.assertEqual(
                    ParsedObsidianCommand.parse(command.value).command, command
                )
        parsed = ParsedObsidianCommand.parse("/reanalyze 42")
        self.assertEqual(parsed.arguments, "42")
        self.assertIsNone(ParsedObsidianCommand.parse("/reanalyze_extra 42"))

    def test_obsidian_help_sections_keep_existing_order(self) -> None:
        """Разделы vault и статей идут после общих разделов в прежнем порядке."""
        sections = help_sections()
        self.assertEqual([section[0] for section in sections], ["Obsidian vault", "Статьи"])
        self.assertEqual(
            [line.split("`")[1] for _, lines in sections for line in lines],
            ["/github_status", "/github_sync", "/github_disconnect", "/reanalyze <ID статьи>"],
        )


if __name__ == "__main__":
    unittest.main()
