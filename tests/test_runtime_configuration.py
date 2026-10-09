"""Проверки конфигурации шага 2.5 через реальные CLI и bootstrap без live API."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from obs_chat_bot.capabilities.obsidian.bootstrap import (
    ObsidianProcessRuntime,
    create_prepare_obsidian_review_use_case,
)
from obs_chat_bot.capabilities.obsidian.data.configuration.config import (
    ObsidianConfigError, load_obsidian_config, ObsidianConfig,
)
from obs_chat_bot.core.data.config import load_config
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.presentation.cli.main import main, run_healthcheck
from obs_chat_bot.capabilities.obsidian.domain.search.hybrid import VaultFtsFallbackSearchService
from test_process_incoming_message import FakeVaultSelectionManager


TOKEN = "123456789:abcdefghijklmnopqrstuvwxyz"


class RuntimeConfigurationTest(unittest.TestCase):
    """Проверяет выбранные каналы, независимые провайдеры и startup-ошибки."""

    def setUp(self) -> None:
        """Создаёт временные БД/PEM и запрещает любые внешние HTTP-запросы."""
        self.directory = TemporaryDirectory(prefix="runtime-config-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key_path = self.root / "app.pem"
        self.key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        for target, options in (
            ("obs_chat_bot.core.data.config.load_dotenv", {"new": None}),
            ("httpx.Client.send", {"side_effect": AssertionError("HTTP запрещён в автономной проверке")}),
            ("urllib.request.urlopen", {"side_effect": AssertionError("HTTP запрещён в автономной проверке")}),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def env(self, channel: str, enabled: bool, state: str) -> dict[str, str]:
        """Задаёт выбранную комбинацию канала и состояния Obsidian."""
        values = {
            "APP_ENV": "test", "DATABASE_PATH": str(self.root / "app.db"),
            "ENABLED_CHANNELS": channel,
            "ENABLED_CAPABILITIES": "obsidian" if enabled else "",
            "TELEGRAM_BOT_TOKEN": TOKEN if channel == "telegram" else "",
            "VK_BOT_TOKEN": "vk-test" if channel == "vk" else "",
            "VK_GROUP_ID": "42" if channel == "vk" else "",
        }
        if state == "valid":
            values.update(
                OPENAI_BASE_URL="https://llm.example/v1", OPENAI_API_KEY="llm-secret",
                OPENAI_MODEL="llm-model", GITHUB_APP_ID="42", GITHUB_CLIENT_ID="Iv1.test",
                GITHUB_APP_SLUG="test-app", GITHUB_PRIVATE_KEY_PATH=str(self.key_path),
                EMBEDDING_BASE_URL="https://embedding.example/v1", EMBEDDING_API_KEY="embedding-secret",
                EMBEDDING_DOCUMENT_MODEL="doc", EMBEDDING_QUERY_MODEL="query",
            )
        elif state == "invalid":
            values.update(
                OPENAI_API_KEY="secret", GITHUB_APP_ID="bad", EMBEDDING_API_KEY="secret",
                EMBEDDING_PRICE_PER_MILLION_TOKENS="bad", CHUNK_MINIMUM_SIZE_CHARS="bad",
                OBSIDIAN_CREATED_NOTE_TAG="#bad",
            )
        return values

    def run_cli(self, args: list[str], env: dict[str, str]) -> tuple[int, Mock]:
        """Выполняет настоящий main с окружением теста и безопасным logger."""
        logger = Mock()
        with patch.dict(os.environ, env, clear=True), patch("sys.argv", ["obs-chat-bot", *args]), patch(
            "obs_chat_bot.presentation.cli.main.configure_logging"
        ), patch("obs_chat_bot.presentation.cli.main.logging.getLogger", return_value=logger):
            code = main()
        return code, logger

    def test_bot_matrix_and_core_commands_without_providers(self) -> None:
        """TG/VK стартуют только с выбранными настройками и обслуживают профиль без LLM."""
        for channel in ("telegram", "vk"):
            for enabled in (False, True):
                for state in ("absent", "valid", "invalid"):
                    with self.subTest(channel=channel, enabled=enabled, state=state):
                        env = self.env(channel, enabled, state)
                        # Ошибки невыбранного канала не должны даже разбираться.
                        if channel == "telegram":
                            env.update(VK_GROUP_ID="bad", VK_BOT_TOKEN="")
                        else:
                            env.update(TELEGRAM_BOT_TOKEN="bad", TELEGRAM_PROXY_URL="secret://bad")
                        with patch(f"obs_chat_bot.presentation.cli.main.run_{channel}_bot") as runner:
                            code, _logger = self.run_cli([f"--{channel}-bot"], env)
                        if enabled and state == "invalid":
                            self.assertEqual(code, 2)
                            runner.assert_not_called()
                            continue
                        self.assertEqual(code, 0)
                        runner.assert_called_once()
                        processor = runner.call_args.kwargs["incoming_message_processor"]
                        if state == "absent" or not enabled:
                            with patch("obs_chat_bot.capabilities.obsidian.bootstrap.OpenAIArticleAnalyzer") as analyzer, patch(
                                "obs_chat_bot.capabilities.obsidian.bootstrap.OpenAIObsidianProposalGenerator"
                            ) as proposal, patch("obs_chat_bot.capabilities.obsidian.bootstrap.OpenAICompatibleEmbeddingProvider") as embedding:
                                for text in ("/register", "/name Имя", "/help", "/status", "/exit", "/obsidian"):
                                    reply = processor(IncomingEvent(channel, channel, text, text, channel), None)
                                    if text == "/obsidian":
                                        expected = "Провайдер GitHub не настроен" if enabled else "отключена"
                                        self.assertIn(expected, reply.text)
                                analyzer.assert_not_called()
                                proposal.assert_not_called()
                                embedding.assert_not_called()

    def test_disabled_obsidian_never_builds_process_runtime(self) -> None:
        """Неверный выключенный провайдер не создаёт GitHub, embedding или LLM-клиенты."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel), patch(
                "obs_chat_bot.presentation.cli.main.create_obsidian_process_runtime",
                side_effect=AssertionError("Отключённая возможность не собирается"),
            ), patch(f"obs_chat_bot.presentation.cli.main.run_{channel}_bot"):
                code, _ = self.run_cli([f"--{channel}-bot"], self.env(channel, False, "invalid"))
                self.assertEqual(code, 0)

    def test_healthcheck_matrix_uses_explicit_channel(self) -> None:
        """Healthcheck выбранного канала проходит без чужих токенов и выключенных провайдеров."""
        for channel in ("telegram", "vk"):
            for enabled in (False, True):
                for state in ("absent", "valid", "invalid"):
                    with self.subTest(channel=channel, enabled=enabled, state=state):
                        env = self.env(channel, enabled, state)
                        env["ENABLED_CHANNELS"] = "telegram,vk"
                        code, logger = self.run_cli(["--healthcheck", "--channels", channel], env)
                        self.assertEqual(code, 2 if enabled and state == "invalid" else 0)
                        diagnostic = str(logger.method_calls)
                        self.assertNotIn("llm-secret", diagnostic)
                        self.assertNotIn("embedding-secret", diagnostic)

    def test_selected_channel_missing_settings_blocks_polling(self) -> None:
        """Незаполненный выбранный канал не открывает polling или рабочую SQLite."""
        for channel, missing in (("telegram", "TELEGRAM_BOT_TOKEN"), ("vk", "VK_BOT_TOKEN"), ("vk", "VK_GROUP_ID")):
            with self.subTest(channel=channel, missing=missing), patch(f"obs_chat_bot.presentation.cli.main.run_{channel}_bot") as runner:
                env = self.env(channel, False, "absent")
                env[missing] = " "
                code, logger = self.run_cli([f"--{channel}-bot"], env)
                self.assertEqual(code, 2)
                self.assertIn(missing, str(logger.method_calls))
                self.assertFalse((self.root / "app.db").exists())
                runner.assert_not_called()

    def test_invalid_selected_channel_or_provider_blocks_start(self) -> None:
        """Некорректный токен, unsafe endpoint и невалидный PEM обнаруживаются до polling."""
        cases = (
            ("telegram", {"TELEGRAM_BOT_TOKEN": "bad"}, "Telegram"),
            ("vk", {"VK_GROUP_ID": "bad"}, "VK_GROUP_ID"),
            ("telegram", {"OPENAI_BASE_URL": "http://localhost/v1"}, "LLM"),
            ("vk", {"EMBEDDING_BASE_URL": "http://127.0.0.1/v1"}, "Embedding"),
            ("vk", {"GITHUB_PRIVATE_KEY_PATH": str(self.root / "missing.pem")}, "GitHub"),
        )
        for channel, override, diagnostic in cases:
            with self.subTest(override=override), patch(f"obs_chat_bot.presentation.cli.main.run_{channel}_bot") as runner:
                env = self.env(channel, True, "valid")
                env.update(override)
                code, logger = self.run_cli([f"--{channel}-bot"], env)
                self.assertEqual(code, 2)
                self.assertIn(diagnostic, str(logger.method_calls))
                runner.assert_not_called()

    def test_partial_groups_name_missing_settings(self) -> None:
        """Ошибки LLM, GitHub и embeddings называют конкретные поля без секретов."""
        for group, values, missing in (
            ("LLM", {"OPENAI_API_KEY": "secret"}, "OPENAI_BASE_URL"),
            ("GitHub", {"GITHUB_APP_ID": "42"}, "GITHUB_CLIENT_ID"),
            ("Embedding", {"EMBEDDING_API_KEY": "secret"}, "EMBEDDING_DOCUMENT_MODEL"),
        ):
            with self.subTest(group=group):
                env = self.env("telegram", True, "absent")
                env.update(values)
                with patch.dict(os.environ, env, clear=True), self.assertRaises(ObsidianConfigError) as error:
                    load_obsidian_config()
                self.assertIn(missing, str(error.exception))
                self.assertNotIn("secret", str(error.exception))

    def test_healthcheck_reports_all_independent_provider_failures(self) -> None:
        """Невалидные endpoint и PEM дают отдельные результаты проверки."""
        with patch.dict(os.environ, self.env("vk", True, "valid"), clear=True):
            config = load_config()
            obsidian_config = load_obsidian_config()
        obsidian_config = replace(
            obsidian_config, openai_base_url="http://localhost/v1",
            github_app=replace(obsidian_config.github_app, private_key_path=self.root / "missing.pem"),
            embedding=replace(obsidian_config.embedding, base_url="http://localhost/v1"),
        )
        logger = Mock()
        self.assertEqual(run_healthcheck(config, logger, obsidian_config=obsidian_config), 1)
        for diagnostic in ("LLM", "GitHub", "Embedding", "Database connection is ready"):
            self.assertIn(diagnostic, str(logger.method_calls))
        self.assertNotIn("vault не подключён", str(logger.method_calls))

    def test_shared_healthcheck_checks_both_selected_channels(self) -> None:
        """Общий healthcheck не пропускает VK при корректном Telegram."""
        env = self.env("telegram", False, "absent")
        env.update(ENABLED_CHANNELS="telegram,vk", VK_BOT_TOKEN="vk-test", VK_GROUP_ID="42")
        code, logger = self.run_cli(["--healthcheck"], env)
        self.assertEqual(code, 0)
        self.assertIn("Telegram configuration is ready", str(logger.method_calls))
        self.assertIn("VK configuration is ready", str(logger.method_calls))
        env["VK_GROUP_ID"] = ""
        code, logger = self.run_cli(["--healthcheck"], env)
        self.assertEqual(code, 2)
        self.assertIn("VK_GROUP_ID", str(logger.method_calls))

    def test_process_url_requires_only_core_config(self) -> None:
        """CLI extraction не требует токенов каналов или провайдеров анализа."""
        env = self.env("telegram", True, "invalid")
        env["TELEGRAM_BOT_TOKEN"] = ""
        with patch("obs_chat_bot.presentation.cli.main.run_process_url_command", return_value=0) as pipeline:
            code, _ = self.run_cli(["--process-url", "https://article.example/page"], env)
        self.assertEqual(code, 0)
        pipeline.assert_called_once()

    def test_invalid_channel_selection_and_unknown_capability(self) -> None:
        """Неизвестный канал или capability ID отклоняется до сборки."""
        for selection in ("", "telegram,telegram", "unknown"):
            with self.subTest(selection=selection):
                env = self.env("telegram", False, "absent")
                env["ENABLED_CHANNELS"] = selection
                code, _ = self.run_cli(["--healthcheck"], env)
                self.assertEqual(code, 2)
        env = self.env("telegram", False, "absent")
        env["ENABLED_CAPABILITIES"] = "unknown"
        code, logger = self.run_cli(["--telegram-bot"], env)
        self.assertEqual(code, 2)
        self.assertIn("unknown", str(logger.method_calls))

    def test_fts_composition_without_embedding_provider(self) -> None:
        """Незаполненный embedding provider собирает настоящий FTS fallback review."""
        with connect_database(self.root / "fts.db") as connection, patch(
            "obs_chat_bot.capabilities.obsidian.bootstrap.create_embedding_provider",
            side_effect=AssertionError("FTS не создаёт embedding client"),
        ):
            apply_migrations(connection)
            review = create_prepare_obsidian_review_use_case(
                connection, openai_base_url="https://llm.example/v1", openai_api_key="test",
                openai_model="test", embedding_config=None,
            )
            self.assertIsInstance(review._search, VaultFtsFallbackSearchService)

    def test_llm_unavailable_is_reported_before_missing_user_vault(self) -> None:
        """Отсутствующий LLM не маскируется требованием выбрать vault."""
        with patch("obs_chat_bot.presentation.cli.main.run_telegram_bot") as runner:
            code, _ = self.run_cli(["--telegram-bot"], self.env("telegram", True, "absent"))
        self.assertEqual(code, 0)
        processor = runner.call_args.kwargs["incoming_message_processor"]
        for text in ("/register", "/name Имя"):
            processor(IncomingEvent("telegram", "chat", text, text, "user"), None)
        # Gateway есть, но пользователь ещё не выбрал vault; LLM отсутствует.
        runtime = ObsidianProcessRuntime(None, object())
        from channel_helpers import process_channel_incoming_message
        with patch("obs_chat_bot.capabilities.obsidian.bootstrap.create_vault_selection_manager", return_value=FakeVaultSelectionManager()):
            reply = process_channel_incoming_message(
                database_path=self.root / "app.db", incoming_message=IncomingEvent(
                    "telegram", "chat", "article", "/obsidian https://article.example/page", "user"
                ), obsidian_config=ObsidianConfig(openai_base_url="", openai_api_key="", openai_model=""), runtime=runtime,
            )
        self.assertIn("Провайдер LLM не настроен", reply.text)
        self.assertNotIn("vault не подключён", reply.text)

    def test_ask_healthcheck_and_startup_matrix(self) -> None:
        """Все сочетания проверяют ASK_* только при включении и не вызывают API."""
        for channel in ("telegram", "vk"):
            for enabled in ("", "obsidian", "ask", "obsidian,ask"):
                for valid in (False, True):
                    env = self.env(channel, "obsidian" in enabled, "absent")
                    env["ENABLED_CAPABILITIES"] = enabled
                    env.update(ASK_BASE_URL="https://ask.example/v1", ASK_API_KEY="ask-secret", ASK_MODEL="model" if valid else "")
                    for health in (False, True):
                        with self.subTest(channel=channel, enabled=enabled, valid=valid, health=health), patch("obs_chat_bot.capabilities.ask.bootstrap.OpenAIAskModel") as model_type, patch(f"obs_chat_bot.presentation.cli.main.run_{channel}_bot") as runner:
                            code, logger = self.run_cli(["--healthcheck"] if health else [f"--{channel}-bot"], env)
                            invalid = "ask" in enabled and not valid
                            self.assertEqual(code, (1 if health else 2) if invalid else 0)
                            self.assertNotIn("ask-secret", str(logger.method_calls))
                            if health or invalid or "ask" not in enabled:
                                model_type.assert_not_called()
                            if invalid:
                                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
