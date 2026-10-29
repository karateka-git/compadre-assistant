"""Проверяет личный выбор без SQLite и конкретных возможностей."""

import unittest

from compadre_assistant.capabilities.ask.presentation.handler import AskCapability
from compadre_assistant.core.domain.capability_preferences import (
    CapabilityDefault,
    GetUserCapabilitySelections,
    SetUserCapabilityPreference,
    UserCapabilityPreference,
    UserCapabilityPreferenceRepository,
)
from compadre_assistant.core.presentation.incoming.ports import CapabilityCommand, CapabilityDescriptor


class FakeRepository(UserCapabilityPreferenceRepository):
    """Хранит явные настройки только для проверки domain-контракта."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], bool] = {}

    def get_for_user(self, app_user_id: int) -> tuple[UserCapabilityPreference, ...]:
        """Возвращает все строки одного пользователя."""
        return tuple(
            UserCapabilityPreference(owner, capability_id, enabled)
            for (owner, capability_id), enabled in self.rows.items()
            if owner == app_user_id
        )

    def upsert(self, preference: UserCapabilityPreference) -> bool | None:
        """Возвращает прежнюю строку и меняет только заданный ключ."""
        key = (preference.app_user_id, preference.capability_id)
        previous = self.rows.get(key)
        self.rows[key] = preference.enabled
        return previous


class CapabilityPreferencesDomainTest(unittest.TestCase):
    """Проверяет defaults, переопределение и изоляцию ID и пользователей."""

    def test_descriptors_define_defaults(self) -> None:
        """Ask включён, Obsidian и будущий встроенный ID выключены."""
        self.assertTrue(AskCapability.descriptor.enabled_by_default)
        self.assertFalse(CapabilityDescriptor("obsidian", CapabilityCommand("/obsidian", (), "Vault")).enabled_by_default)
        self.assertFalse(CapabilityDescriptor("new", CapabilityCommand("/new", (), "New")).enabled_by_default)

    def test_defaults_and_explicit_choices_are_independent(self) -> None:
        """Отсутствующие строки получают default, неизвестная строка не открывает маршрут."""
        repository = FakeRepository()
        defaults = (CapabilityDefault("alpha", True), CapabilityDefault("beta"))
        reader = GetUserCapabilitySelections(repository, defaults)
        writer = SetUserCapabilityPreference(repository, defaults)
        self.assertEqual(
            [(item.capability_id, item.enabled, item.explicit) for item in reader.execute(1)],
            [("alpha", True, False), ("beta", False, False)],
        )
        repository.rows[(1, "future")] = True
        self.assertFalse(writer.execute(1, "alpha", True))
        self.assertTrue(writer.execute(1, "beta", True))
        self.assertTrue(writer.execute(1, "alpha", False))
        self.assertFalse(writer.execute(1, "alpha", False))
        self.assertEqual(
            [(item.capability_id, item.enabled, item.explicit) for item in reader.execute(1)],
            [("alpha", False, True), ("beta", True, True)],
        )
        self.assertEqual(repository.rows[(1, "future")], True)
        self.assertTrue(reader.execute(2)[0].enabled)

    def test_unknown_id_is_rejected_without_write(self) -> None:
        """Изменение неизвестного ID не создаёт строку."""
        repository = FakeRepository()
        writer = SetUserCapabilityPreference(repository, (CapabilityDefault("alpha"),))
        with self.assertRaises(ValueError):
            writer.execute(1, "future", True)
        self.assertEqual(repository.rows, {})


if __name__ == "__main__":
    unittest.main()
