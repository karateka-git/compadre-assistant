"""Тесты загрузки конфигурации приложения."""

import os
import unittest
from dataclasses import fields
from unittest.mock import patch

from obs_chat_bot.core.data.config import AppConfig, ConfigError, load_config
from obs_chat_bot.capabilities.obsidian.data.configuration.config import (
    ObsidianConfigError, load_obsidian_config,
)


class ConfigTest(unittest.TestCase):
    """Проверяет чтение `.env`-настроек."""

    def setUp(self) -> None:
        """Изолирует окружение теста от credentials локального `.env`."""
        self.dotenv = patch("obs_chat_bot.core.data.config.load_dotenv", None)
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)

    def test_enabled_capabilities_default_and_explicit_disable(self) -> None:
        """Список входов имеет явное значение по умолчанию и допускает пустую сборку."""
        with patch.dict(os.environ, _env(), clear=True):
            self.assertEqual(load_config().enabled_capabilities, ("obsidian",))
        with patch.dict(os.environ, _env(ENABLED_CAPABILITIES=""), clear=True):
            self.assertEqual(load_config().enabled_capabilities, ())

    def test_enabled_capabilities_reject_duplicate_and_malformed_ids(self) -> None:
        """Core проверяет форму ID, а доступные конкретные сборки знает composition root."""
        for identifiers in ("obsidian,obsidian", "???"):
            with self.subTest(identifiers=identifiers), patch.dict(os.environ, _env(ENABLED_CAPABILITIES=identifiers), clear=True):
                with self.assertRaisesRegex(ConfigError, "ENABLED_CAPABILITIES"):
                    load_config()

    def test_core_config_accepts_arbitrary_capability_id(self) -> None:
        """Новый ID добавляется в конфигурацию без изменений core."""
        with patch.dict(os.environ, _env(ENABLED_CAPABILITIES="fake"), clear=True):
            self.assertEqual(load_config().enabled_capabilities, ("fake",))

    def test_core_profile_has_no_obsidian_fields_for_future_ask(self) -> None:
        """Новый профиль Ask не требует предметных полей или разбора Obsidian в core."""
        field_names = {item.name for item in fields(AppConfig)}
        self.assertTrue({"enabled_capabilities", "selected_channels"} <= field_names)
        self.assertFalse(field_names & {
            "openai_base_url", "openai_api_key", "openai_model", "github_app",
            "embedding", "chunking", "obsidian_created_note_tag",
        })
        with patch.dict(os.environ, _env(
            ENABLED_CAPABILITIES="ask", OPENAI_API_KEY="partial",
            GITHUB_APP_ID="bad", EMBEDDING_PRICE_PER_MILLION_TOKENS="bad",
        ), clear=True):
            self.assertEqual(load_config().enabled_capabilities, ("ask",))

    def test_load_config_reads_optional_debug_flag(self) -> None:
        """APP_DEBUG включает debug-режим, но остаётся опциональным."""
        with patch.dict(os.environ, _env(APP_DEBUG="true"), clear=True):
            config = load_config()

        self.assertTrue(config.app_debug)
        self.assertEqual(config.safe_summary()["app_debug"], "true")

    def test_load_config_reads_optional_vk_settings(self) -> None:
        """VK token и group id остаются опциональными, но читаются из окружения."""
        with patch.dict(
            os.environ,
            _env(ENABLED_CHANNELS="vk", VK_BOT_TOKEN="vk-token", VK_GROUP_ID="123"),
            clear=True,
        ):
            config = load_config()

        self.assertEqual(config.vk_bot_token, "vk-token")
        self.assertEqual(config.vk_group_id, 123)
        self.assertEqual(config.safe_summary()["vk_bot_token"], "set")

    def test_load_config_reads_telegram_proxy_without_logging_address(self) -> None:
        """Прокси настраивается отдельно и не раскрывается в safe summary."""
        proxy_url = "socks5://user:secret@10.77.77.1:1080"
        with patch.dict(
            os.environ,
            _env(TELEGRAM_PROXY_URL=proxy_url),
            clear=True,
        ):
            config = load_config()

        self.assertEqual(config.telegram_proxy_url, proxy_url)
        self.assertEqual(config.safe_summary()["telegram_proxy_url"], "set")
        self.assertNotIn(proxy_url, config.safe_summary().values())

    def test_load_config_rejects_invalid_telegram_proxy_without_echoing_it(self) -> None:
        """Ошибочный URL прокси не выводит учётные данные в исключении."""
        with patch.dict(
            os.environ,
            _env(TELEGRAM_PROXY_URL="ftp://user:secret@example.com:1080"),
            clear=True,
        ):
            with self.assertRaises(ConfigError) as raised:
                load_config()

        self.assertNotIn("secret", str(raised.exception))

    def test_load_config_defaults_debug_to_false(self) -> None:
        """Без APP_DEBUG приложение работает в обычном logging-режиме."""
        with patch.dict(os.environ, _env(), clear=True):
            config = load_config()

        self.assertFalse(config.app_debug)

    def test_load_obsidian_config_defaults_created_note_tag(self) -> None:
        """Без env-настройки заметки бота получают согласованный тег."""
        with patch.dict(os.environ, _env(), clear=True):
            config = load_obsidian_config()

        self.assertEqual(config.obsidian_created_note_tag, "knowledge-catcher")
        self.assertEqual(
            config.safe_summary()["obsidian_created_note_tag"],
            "knowledge-catcher",
        )

    def test_load_obsidian_config_reads_hierarchical_created_note_tag(self) -> None:
        """Тег может использовать поддерживаемую Obsidian иерархию через `/`."""
        with patch.dict(
            os.environ,
            _env(OBSIDIAN_CREATED_NOTE_TAG="topic/ai"),
            clear=True,
        ):
            config = load_obsidian_config()

        self.assertEqual(config.obsidian_created_note_tag, "topic/ai")

    def test_load_obsidian_config_disables_created_note_tag_case_insensitively(self) -> None:
        """Специальное значение `off` отключает маркер без учёта регистра."""
        with patch.dict(
            os.environ,
            _env(OBSIDIAN_CREATED_NOTE_TAG="OfF"),
            clear=True,
        ):
            config = load_obsidian_config()

        self.assertIsNone(config.obsidian_created_note_tag)
        self.assertEqual(
            config.safe_summary()["obsidian_created_note_tag"],
            "disabled",
        )

    def test_load_obsidian_config_rejects_empty_created_note_tag(self) -> None:
        """Явно заданная пустая строка не интерпретируется как отключение."""
        with patch.dict(
            os.environ,
            _env(OBSIDIAN_CREATED_NOTE_TAG=""),
            clear=True,
        ):
            with self.assertRaises(ObsidianConfigError):
                load_obsidian_config()

    def test_load_obsidian_config_rejects_invalid_created_note_tags(self) -> None:
        """Маркер соответствует синтаксису Obsidian и текущего parser Markdown."""
        invalid_values = (
            "#knowledge-catcher",
            "knowledge catcher",
            "topic//ai",
            "/topic",
            "topic/",
            "123",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    _env(OBSIDIAN_CREATED_NOTE_TAG=value),
                    clear=True,
                ):
                    with self.assertRaises(ObsidianConfigError):
                        load_obsidian_config()

    def test_load_obsidian_config_reads_optional_chunking_settings(self) -> None:
        """Технические размеры chunks читаются отдельно от правил vault."""
        with patch.dict(
            os.environ,
            _env(
                CHUNK_MINIMUM_SIZE_CHARS="100",
                CHUNK_TARGET_SIZE_CHARS="1000",
                CHUNK_MAXIMUM_SIZE_CHARS="2000",
            ),
            clear=True,
        ):
            config = load_obsidian_config()

        self.assertEqual(config.chunking.minimum_size_chars, 100)
        self.assertEqual(config.chunking.target_size_chars, 1000)
        self.assertEqual(config.chunking.maximum_size_chars, 2000)

    def test_load_obsidian_config_uses_default_chunking_settings(self) -> None:
        """Без CHUNK_* приложение получает согласованную безопасную policy."""
        with patch.dict(os.environ, _env(), clear=True):
            config = load_obsidian_config()

        self.assertEqual(config.chunking.minimum_size_chars, 300)
        self.assertEqual(config.chunking.target_size_chars, 3000)
        self.assertEqual(config.chunking.maximum_size_chars, 6000)

    def test_load_obsidian_config_rejects_inconsistent_chunking_settings(self) -> None:
        """Minimum, target и maximum должны сохранять допустимый порядок."""
        with patch.dict(
            os.environ,
            _env(
                CHUNK_MINIMUM_SIZE_CHARS="1000",
                CHUNK_TARGET_SIZE_CHARS="500",
                CHUNK_MAXIMUM_SIZE_CHARS="2000",
            ),
            clear=True,
        ):
            with self.assertRaises(ObsidianConfigError):
                load_obsidian_config()

    def test_load_obsidian_config_reads_optional_github_app_group(self) -> None:
        """GitHub App включается только полным набором безопасных настроек."""
        with patch.dict(
            os.environ,
            _env(
                GITHUB_APP_ID="12345",
                GITHUB_CLIENT_ID="Iv1.client",
                GITHUB_APP_SLUG="obs-chat-bot",
                GITHUB_PRIVATE_KEY_PATH="data/github-app.pem",
            ),
            clear=True,
        ):
            config = load_obsidian_config()

        self.assertEqual(config.github_app.app_id, 12345)
        self.assertEqual(config.github_app.client_id, "Iv1.client")
        self.assertEqual(
            config.github_app.installation_url,
            "https://github.com/apps/obs-chat-bot/installations/new",
        )
        self.assertNotIn("Iv1.client", config.safe_summary().values())

    def test_load_obsidian_config_leaves_github_disabled_when_group_is_empty(self) -> None:
        """Пустая GitHub-группа не мешает запускать ещё не настроенный connector."""
        with patch.dict(os.environ, _env(), clear=True):
            config = load_obsidian_config()

        self.assertIsNone(config.github_app)

    def test_load_obsidian_config_rejects_partial_github_app_group(self) -> None:
        """Частичная GitHub-конфигурация не допускает неоднозначный runtime."""
        with patch.dict(
            os.environ,
            _env(GITHUB_APP_ID="12345", GITHUB_CLIENT_ID="Iv1.client"),
            clear=True,
        ):
            with self.assertRaises(ObsidianConfigError):
                load_obsidian_config()

    def test_load_obsidian_config_reads_separate_embedding_group(self) -> None:
        """Embedding provider не переиспользует credentials LLM-агента."""
        with patch.dict(
            os.environ,
            _env(
                EMBEDDING_BASE_URL="https://api.timeweb.ai/v1/",
                EMBEDDING_API_KEY="embedding-secret",
                EMBEDDING_DOCUMENT_MODEL="openai/text-embedding-3-large",
                EMBEDDING_QUERY_MODEL="openai/text-embedding-3-large",
            ),
            clear=True,
        ):
            config = load_obsidian_config()

        self.assertEqual(
            config.embedding.base_url,
            "https://api.timeweb.ai/v1",
        )
        self.assertEqual(
            config.embedding.document_model,
            "openai/text-embedding-3-large",
        )
        self.assertEqual(
            config.embedding.query_model,
            "openai/text-embedding-3-large",
        )
        self.assertEqual(config.safe_summary()["embedding_api_key"], "set")
        self.assertNotIn("embedding-secret", config.safe_summary().values())

    def test_load_obsidian_config_leaves_embeddings_disabled_when_group_is_empty(self) -> None:
        """До настройки semantic search группа остаётся опциональной."""
        with patch.dict(os.environ, _env(), clear=True):
            config = load_obsidian_config()

        self.assertIsNone(config.embedding)

    def test_load_obsidian_config_reads_complete_embedding_pricing_group(self) -> None:
        """Тариф загружается только целиком и безопасно виден в summary."""
        with patch.dict(
            os.environ,
            _env(
                EMBEDDING_BASE_URL="https://api.timeweb.ai/v1",
                EMBEDDING_API_KEY="embedding-secret",
                EMBEDDING_DOCUMENT_MODEL="openai/text-embedding-3-large",
                EMBEDDING_QUERY_MODEL="openai/text-embedding-3-large",
                EMBEDDING_PRICE_PER_MILLION_TOKENS="3.25",
                EMBEDDING_PRICE_CURRENCY="RUB",
                EMBEDDING_TARIFF_VERSION="2026-08-04",
            ),
            clear=True,
        ):
            config = load_obsidian_config()

        self.assertEqual(str(config.embedding.price_per_million_tokens), "3.25")
        self.assertEqual(config.embedding.price_currency, "RUB")
        self.assertEqual(config.embedding.tariff_version, "2026-08-04")
        self.assertEqual(
            config.safe_summary()["embedding_price_per_million_tokens"],
            "3.25",
        )

    def test_load_obsidian_config_rejects_partial_embedding_pricing_group(self) -> None:
        """Одна цена без валюты и версии тарифа не создаёт ложную оценку cost."""
        with patch.dict(
            os.environ,
            _env(
                EMBEDDING_BASE_URL="https://api.timeweb.ai/v1",
                EMBEDDING_API_KEY="embedding-secret",
                EMBEDDING_DOCUMENT_MODEL="openai/text-embedding-3-large",
                EMBEDDING_QUERY_MODEL="openai/text-embedding-3-large",
                EMBEDDING_PRICE_PER_MILLION_TOKENS="3.25",
            ),
            clear=True,
        ):
            with self.assertRaises(ObsidianConfigError):
                load_obsidian_config()

    def test_load_obsidian_config_rejects_partial_embedding_group(self) -> None:
        """Ключ без URL и model не создаёт неоднозначный runtime."""
        with patch.dict(
            os.environ,
            _env(EMBEDDING_API_KEY="embedding-secret"),
            clear=True,
        ):
            with self.assertRaises(ObsidianConfigError):
                load_obsidian_config()

    def test_load_config_rejects_invalid_debug_flag(self) -> None:
        """Некорректное boolean-значение APP_DEBUG считается ошибкой конфига."""
        with patch.dict(os.environ, _env(APP_DEBUG="maybe"), clear=True):
            with self.assertRaises(ConfigError):
                load_config()

    def test_load_config_rejects_invalid_vk_group_id(self) -> None:
        """Некорректный VK_GROUP_ID считается ошибкой конфига."""
        with patch.dict(os.environ, _env(ENABLED_CHANNELS="vk", VK_BOT_TOKEN="vk-token", VK_GROUP_ID="bad"), clear=True):
            with self.assertRaises(ConfigError):
                load_config()


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "APP_ENV": "test",
        "ENABLED_CHANNELS": "telegram",
        "DATABASE_PATH": "data/test.db",
        "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz",
        "OPENAI_BASE_URL": "https://llm.example/v1",
        "OPENAI_API_KEY": "token",
        "OPENAI_MODEL": "fake-model",
        "GITHUB_APP_ID": "",
        "GITHUB_CLIENT_ID": "",
        "GITHUB_APP_SLUG": "",
        "GITHUB_PRIVATE_KEY_PATH": "",
        "EMBEDDING_BASE_URL": "",
        "EMBEDDING_API_KEY": "",
        "EMBEDDING_DOCUMENT_MODEL": "",
        "EMBEDDING_QUERY_MODEL": "",
        "EMBEDDING_PRICE_PER_MILLION_TOKENS": "",
        "EMBEDDING_PRICE_CURRENCY": "",
        "EMBEDDING_TARIFF_VERSION": "",
    }
    values.update(overrides)
    return values


if __name__ == "__main__":
    unittest.main()
