"""Проверяет разбор операторского списка доверенных AppUser."""

import unittest
from uuid import UUID

from compadre_assistant.capabilities.admin.data.configuration import (
    AdminConfigError, AdminTimewebConfig, load_admin_timeweb_config, load_admin_trusted_app_user_ids,
)


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


class AdminTimewebConfigurationTest(unittest.TestCase):
    """Проверяет отдельную настройку источника отчёта."""

    def setUp(self) -> None:
        """Готовит синтетическую пару токена и ID ключа."""
        self.key = "550e8400-e29b-41d4-a716-446655440000"
        self.environment = {"ADMIN_TIMEWEB_API_TOKEN": "account.token", "ADMIN_TIMEWEB_KEY_ID": self.key}

    def test_empty_source_keeps_report_unconfigured(self) -> None:
        """Валюту можно заполнить до выдачи токена и выбора ключа."""
        self.assertIsNone(load_admin_timeweb_config({}))
        self.assertIsNone(load_admin_timeweb_config({"ADMIN_TIMEWEB_CURRENCY": "EUR"}))

    def test_full_source_uses_explicit_currency_or_rubles(self) -> None:
        """UUID приводится к канонической форме, токен скрыт в repr."""
        self.assertEqual(load_admin_timeweb_config(self.environment),
                         AdminTimewebConfig("account.token", UUID(self.key), "RUB"))
        config = load_admin_timeweb_config({
            **self.environment, "ADMIN_TIMEWEB_KEY_ID": self.key.upper(), "ADMIN_TIMEWEB_CURRENCY": " eur ",
        })
        self.assertEqual(config, AdminTimewebConfig("account.token", UUID(self.key), "EUR"))
        self.assertNotIn("account.token", repr(config))

    def test_invalid_source_does_not_disclose_values(self) -> None:
        """Неполная пара и неверные форматы отклоняются без вывода значений."""
        for override in (
            {"ADMIN_TIMEWEB_API_TOKEN": ""},
            {"ADMIN_TIMEWEB_KEY_ID": ""},
            {"ADMIN_TIMEWEB_KEY_ID": "incorrect-id-value"},
            {"ADMIN_TIMEWEB_KEY_ID": self.key.replace("-", "")},
            {"ADMIN_TIMEWEB_API_TOKEN": "unsafe\r\nsecret"},
            {"ADMIN_TIMEWEB_CURRENCY": "₽"},
        ):
            with self.subTest(setting=next(iter(override))), self.assertRaises(AdminConfigError) as raised:
                load_admin_timeweb_config({**self.environment, **override})
            for private_value in ("account.token", "incorrect-id-value", "unsafe", "secret"):
                self.assertNotIn(private_value, str(raised.exception))
