"""Проверяет чистый персональный снимок общего реестра."""

from dataclasses import FrozenInstanceError
import unittest

from compadre_assistant.core.domain.capability_preferences import UserCapabilitySelection
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityDescriptor,
    CapabilityResult,
    HandlerContext,
    IncomingCapabilityPort,
)
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry


class FakeCapability(IncomingCapabilityPort):
    """Представляет произвольную возможность вне каталога приложения."""

    descriptor = CapabilityDescriptor("example", "/example", "пример", enabled_by_default=True)

    def enter(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> CapabilityResult:
        """Не используется при проверке метаданных."""
        raise AssertionError("unexpected entry")

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Не используется при проверке метаданных."""
        raise AssertionError("unexpected handle")


class PersonalCapabilityRegistryTest(unittest.TestCase):
    """Проверяет пересечение server и user без специальных ID в ядре."""

    def test_active_and_disabled_descriptors_keep_defaults(self) -> None:
        """Одинаковый ID получает одинаковый default в обоих состояниях сборки."""
        active = CapabilityRegistry((FakeCapability(),))
        disabled = CapabilityRegistry((), (FakeCapability.descriptor,))
        self.assertEqual(active.descriptors(), disabled.descriptors())
        active_view = active.for_user(())
        disabled_view = disabled.for_user(())
        self.assertTrue(active_view.status("example").effective)
        self.assertTrue(disabled_view.status("example").user_enabled)
        self.assertFalse(disabled_view.status("example").effective)
        self.assertIsNone(disabled_view.by_command("/example"))
        self.assertIsNotNone(disabled_view.blocked_by_command("/example argument"))
        self.assertIsNone(disabled_view.status("future"))

    def test_personal_snapshot_does_not_change_shared_registry(self) -> None:
        """Личное выключение скрывает обработчик и справку только в своём снимке."""
        registry = CapabilityRegistry((FakeCapability(),))
        first = registry.for_user((UserCapabilitySelection("example", False, True),))
        second = registry.for_user(())
        self.assertIsNone(first.by_id("example"))
        self.assertIsNotNone(first.blocked_by_command("/example"))
        self.assertNotIn("/example", first.entry_hint())
        self.assertEqual(first.help().sections, ())
        self.assertIsNotNone(second.by_command("/example"))
        self.assertIn("/example", second.entry_hint())
        self.assertIsNotNone(registry.by_id("example"))
        with self.assertRaises(FrozenInstanceError):
            first.access = ()


if __name__ == "__main__":
    unittest.main()
