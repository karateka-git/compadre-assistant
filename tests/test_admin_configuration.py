"""Проверяет разбор операторского списка доверенных AppUser."""

import unittest

from compadre_assistant.capabilities.admin.data.configuration import AdminConfigError, load_admin_trusted_app_user_ids


class AdminConfigurationTest(unittest.TestCase):
    """Покрывает пустой список, допустимые и неверные ID."""

    def test_empty_or_absent_value_grants_no_profile(self) -> None:
        """Пустая настройка и отсутствующая переменная не дают доступ владельцу по умолчанию."""
        self.assertEqual(load_admin_trusted_app_user_ids({}), frozenset())
        self.assertEqual(load_admin_trusted_app_user_ids({"ADMIN_TRUSTED_APP_USER_IDS": "   "}), frozenset())

    def test_positive_internal_ids_with_whitespace(self) -> None:
        """Учитывает окружающие пробелы и верхнюю границу ID SQLite."""
        self.assertEqual(load_admin_trusted_app_user_ids({"ADMIN_TRUSTED_APP_USER_IDS": "18, 9223372036854775807 "}),
                         frozenset((18, 2**63 - 1)))

    def test_invalid_values_reject_whole_setting(self) -> None:
        """При неверной записи не применяет даже корректную часть списка."""
        for invalid in ("0", "-1", "01", "١", "1,", ",7", "1,,2", "+2", "2 3", "1,1", "1,9223372036854775808", "9" * 100):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(AdminConfigError, "ADMIN_TRUSTED_APP_USER_IDS"):
                load_admin_trusted_app_user_ids({"ADMIN_TRUSTED_APP_USER_IDS": invalid})
