"""Проверяет публичный каталог команд и границы его регистрации."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from compadre_assistant.capabilities.ask.presentation.handler import AskCapability
from compadre_assistant.capabilities.obsidian.presentation.commands import obsidian_descriptor
from compadre_assistant.core.presentation.incoming.commands import ChatCommand
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CommandParameter, HelpSection,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.incoming.help import format_contextual_help
from compadre_assistant.core.presentation.incoming.ports import ConfirmationQueryResult, IncomingResponse
from compadre_assistant.core.presentation.message_content import text_content
from tests.response_assertions import content_text


def _descriptor(identifier: str, command: str) -> CapabilityDescriptor:
    """Собирает независимый каталог для проверки коллизий."""
    return CapabilityDescriptor(
        identifier, CapabilityCommand(f"/{identifier}", (), "вход"),
        CapabilityHelp((HelpSection("Раздел", (CapabilityCommand(command, (), "действие"),)),)),
        enabled_by_default=True,
    )


class CommandCatalogTest(unittest.TestCase):
    """Проверяет метаданные независимо от предметных обработчиков."""

    def test_conflicts_cover_enabled_and_disabled_subject_commands(self) -> None:
        """Конфликт предметных команд запрещён при любом состоянии сборки."""
        for first_enabled in (True, False):
            for second_enabled in (True, False):
                for conflict in ("/shared", "/first"):
                    with self.subTest(first=first_enabled, second=second_enabled, conflict=conflict):
                        descriptors = (_descriptor("first", "/shared"), _descriptor("second", conflict))
                        flags = (first_enabled, second_enabled)
                        with self.assertRaisesRegex(ValueError, "Конфликт команды"):
                            CapabilityRegistry(
                                tuple(SimpleNamespace(descriptor=d) for d, enabled in zip(descriptors, flags) if enabled),
                                tuple(d for d, enabled in zip(descriptors, flags) if not enabled),
                            )

    def test_reserved_and_invalid_subject_commands_are_rejected(self) -> None:
        """Предметные команды проверяются так же строго, как входы."""
        for name in (*[command.value for command in ChatCommand], "invalid", "/Bad", "/bad argument", "/example"):
            for enabled in (True, False):
                with self.subTest(name=name, enabled=enabled):
                    descriptor = _descriptor("example", name)
                    with self.assertRaises(ValueError):
                        CapabilityRegistry(
                            (SimpleNamespace(descriptor=descriptor),) if enabled else (),
                            () if enabled else (descriptor,),
                        )

    def test_subject_description_cannot_be_empty(self) -> None:
        """Пустое описание предметной команды останавливает сборку."""
        descriptor = _descriptor("example", "/action")
        descriptor = replace(descriptor, help=CapabilityHelp((
            HelpSection("Раздел", (CapabilityCommand("/action", (), " "),)),
        )))
        with self.assertRaisesRegex(ValueError, "Нет описания команды"):
            CapabilityRegistry(disabled_capabilities=(descriptor,))

    def test_help_renders_explicit_parameters_without_parsing_description(self) -> None:
        """Синтаксис берётся из параметров, а описание остаётся свободным текстом."""
        descriptor = replace(_descriptor("example", "/action"), entry=CapabilityCommand(
            "/example", (CommandParameter("первый", "<первый>", True),
                         CommandParameter("второй", "[второй]", False)),
            "описание с /псевдокомандой — и разделителем",
        ))
        registry = CapabilityRegistry((SimpleNamespace(descriptor=descriptor),))
        def render(mode):
            return content_text(format_contextual_help(
                registry.for_user(()), mode, None, ConfirmationQueryResult("complete"),
                IncomingResponse(text_content("")),
            ).content)
        self.assertIn("/example <первый> [второй] — описание с /псевдокомандой — и разделителем", render(None))
        self.assertNotIn("/action — действие", render(None))
        self.assertIn("/action — действие", render("example"))
        self.assertIsNone(registry.by_command("/action"))
        self.assertIsNotNone(registry.by_command("/example значение"))

    def test_builtin_catalog_has_parameters_and_complete_disabled_metadata(self) -> None:
        """Отключённая сборка сохраняет полный каталог без создания клиентов."""
        obsidian = obsidian_descriptor()
        ask = AskCapability.descriptor
        self.assertEqual(ask.entry.parameters, (CommandParameter("вопрос", "<question>", False),))
        self.assertEqual(obsidian.entry.parameters[0].name, "текст или URL")
        commands = [command for section in obsidian.help.sections for command in section.commands]
        self.assertEqual([command.name for command in commands],
                         ["/vault_status", "/vault_sync", "/vault_disconnect", "/reanalyze"])
        self.assertEqual(commands[-1].parameters, (CommandParameter("ID статьи", "<article_id>", False),))
        disabled = CapabilityRegistry(disabled_capabilities=(ask, obsidian))
        self.assertEqual(disabled.disabled_by_command("/obsidian URL"), obsidian)
        self.assertFalse(any(item.effective for item in disabled.for_user(()).access))
