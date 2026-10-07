"""Тесты CLI-команды обработки URL статьи."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from obs_chat_bot.capabilities.obsidian.domain.articles.analyzing import (
    AnalyzeArticleCommand,
    AnalyzeArticleResult,
)
from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.capabilities.obsidian.domain.articles.processing import (
    ProcessArticleUrlCommand,
    ProcessArticleUrlError,
    ProcessArticleUrlResult,
)
from obs_chat_bot.core.presentation.incoming.dispatcher import IncomingMessageResultType
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from obs_chat_bot.core.data.config import AppConfig, GitHubAppConfig
from obs_chat_bot.bootstrap import process_channel_incoming_message
from obs_chat_bot.capabilities.obsidian.bootstrap import ObsidianProcessRuntime
from test_process_incoming_message import FakeVaultSelectionManager, _test_vault
from obs_chat_bot.presentation.cli.main import (
    check_github_config,
    check_llm_config,
    check_telegram_config,
    check_vk_config,
    configure_debug_logging,
    configure_logging,
    parse_args,
    initialize_database,
    run_healthcheck,
    run_process_url_command,
    run_telegram_bot_command,
    run_vk_bot_command,
)
from obs_chat_bot.presentation.telegram.bot import TelegramBotError
from obs_chat_bot.presentation.vk.bot import VkBotError


class SilentLogger:
    """Logger-заглушка для CLI-тестов без вывода в консоль."""

    def info(self, *_args: object, **_kwargs: object) -> None:
        """Игнорирует info-сообщение."""

    def error(self, *_args: object, **_kwargs: object) -> None:
        """Игнорирует error-сообщение."""


class FakeProcessArticleUrlUseCase:
    """Fake use case для проверки CLI без сети и extractor."""

    def __init__(
        self,
        *,
        error: ProcessArticleUrlError | None = None,
        article_id: int | None = 1,
    ) -> None:
        self.commands: list[ProcessArticleUrlCommand] = []
        self._error = error
        self._article_id = article_id

    def execute(self, command: ProcessArticleUrlCommand) -> ProcessArticleUrlResult:
        """Возвращает успешный результат или выбрасывает заданную ошибку."""
        self.commands.append(command)
        if self._error is not None:
            raise self._error

        return ProcessArticleUrlResult(
            article=Article(
                id=self._article_id,
                source_url=command.source_url,
                normalized_url="https://example.com/article",
                title="Article title",
                cleaned_text="Clean text",
                text_hash="hash",
                status=ArticleStatus.EXTRACTED,
            ),
            created=True,
            extracted=True,
        )


class FakeAnalyzeArticleUseCase:
    """Fake use case анализа статьи для Telegram CLI-тестов."""

    def __init__(self) -> None:
        self.commands: list[AnalyzeArticleCommand] = []

    def execute(self, command: AnalyzeArticleCommand) -> AnalyzeArticleResult:
        """Возвращает успешный fake-анализ."""
        self.commands.append(command)
        article = Article(
            id=command.article_id,
            app_user_id=command.app_user_id,
            source_url="https://example.com/article",
            normalized_url="https://example.com/article",
            title="Article title",
            cleaned_text="Clean text",
            text_hash="hash",
            status=ArticleStatus.ANALYZED,
        )
        return AnalyzeArticleResult(
            article=article,
            analysis=ArticleAnalysisResult(
                id=1,
                app_user_id=command.app_user_id,
                article_id=command.article_id,
                llm_model="fake-llm",
                prompt_version="article-summary-v1",
                result_text="## Кратко\nГотово.",
            ),
            created=True,
        )


class ProcessUrlCliTest(unittest.TestCase):
    """Проверяет CLI-команду `--process-url`."""

    def test_parse_args_reads_process_url(self) -> None:
        """CLI принимает URL как отдельный режим запуска."""
        with patch(
            "sys.argv",
            ["obs-chat-bot", "--process-url", "https://example.com/article"],
        ):
            args = parse_args()

        self.assertEqual(args.process_url, "https://example.com/article")
        self.assertFalse(args.healthcheck)
        self.assertFalse(args.sqlite_smoke)

    def test_parse_args_reads_pipeline_smoke(self) -> None:
        """CLI принимает pipeline smoke как отдельный режим запуска."""
        with patch("sys.argv", ["obs-chat-bot", "--pipeline-smoke"]):
            args = parse_args()

        self.assertTrue(args.pipeline_smoke)
        self.assertFalse(args.healthcheck)
        self.assertFalse(args.sqlite_smoke)
        self.assertIsNone(args.process_url)

    def test_parse_args_reads_analysis_smoke(self) -> None:
        """CLI принимает analysis smoke как отдельный режим запуска."""
        with patch("sys.argv", ["obs-chat-bot", "--analysis-smoke"]):
            args = parse_args()

        self.assertTrue(args.analysis_smoke)
        self.assertFalse(args.healthcheck)
        self.assertFalse(args.sqlite_smoke)
        self.assertIsNone(args.process_url)

    def test_parse_args_reads_telegram_bot(self) -> None:
        """CLI принимает запуск Telegram-бота как отдельный режим."""
        with patch("sys.argv", ["obs-chat-bot", "--telegram-bot"]):
            args = parse_args()

        self.assertTrue(args.telegram_bot)
        self.assertFalse(args.healthcheck)
        self.assertFalse(args.sqlite_smoke)
        self.assertIsNone(args.process_url)

    def test_parse_args_reads_vk_bot(self) -> None:
        """CLI принимает запуск VK-бота как отдельный режим."""
        with patch("sys.argv", ["obs-chat-bot", "--vk-bot"]):
            args = parse_args()

        self.assertTrue(args.vk_bot)
        self.assertFalse(args.healthcheck)
        self.assertFalse(args.sqlite_smoke)
        self.assertIsNone(args.process_url)

    def test_run_healthcheck_validates_runtime_config(self) -> None:
        """Healthcheck проверяет SQLite, Telegram и LLM-конфигурацию."""
        with TemporaryDirectory(prefix="obs-chat-bot-health-") as temporary_directory:
            config = AppConfig(
                app_env="test",
                database_path=Path(temporary_directory) / "test.db",
                telegram_bot_token="123456789:abcdefghijklmnopqrstuvwxyz",
                openai_base_url="https://llm.example/v1",
                openai_api_key="token",
                openai_model="fake-model",
            )

            exit_code = run_healthcheck(config, SilentLogger())

        self.assertEqual(exit_code, 0)

    def test_check_telegram_config_rejects_bad_token(self) -> None:
        """Healthcheck отклоняет токен Telegram неожиданной формы."""
        self.assertFalse(check_telegram_config("token", SilentLogger()))

    def test_check_llm_config_rejects_local_base_url(self) -> None:
        """Healthcheck отклоняет LLM endpoint на локальном адресе."""
        self.assertFalse(
            check_llm_config(
                base_url="http://localhost/v1",
                api_key="token",
                model="fake-model",
                logger=SilentLogger(),
            )
        )

    def test_check_vk_config_rejects_missing_values(self) -> None:
        """VK adapter требует token и group id."""
        self.assertFalse(
            check_vk_config(token="", group_id=123, logger=SilentLogger())
        )
        self.assertFalse(
            check_vk_config(token="123456789:abcdefghijklmnopqrstuvwxyz", group_id=None, logger=SilentLogger())
        )

    def test_check_github_config_validates_private_key_file(self) -> None:
        """Healthcheck принимает PEM и отклоняет отсутствующий private key."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        with TemporaryDirectory(prefix="obs-chat-bot-github-health-") as directory:
            key_path = Path(directory) / "app.pem"
            config = GitHubAppConfig(
                app_id=123,
                client_id="Iv1.client",
                app_slug="obs-chat-bot",
                private_key_path=key_path,
            )
            self.assertFalse(check_github_config(config, SilentLogger()))
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            key_path.write_bytes(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            self.assertTrue(check_github_config(config, SilentLogger()))

    def test_configure_debug_logging_enables_debug_level(self) -> None:
        """APP_DEBUG включает DEBUG для приложения."""
        import logging

        root_logger = logging.getLogger()
        app_logger = logging.getLogger("obs_chat_bot")
        old_root_level = root_logger.level
        old_app_level = app_logger.level
        try:
            configure_debug_logging(True)

            self.assertEqual(root_logger.level, logging.DEBUG)
            self.assertEqual(app_logger.level, logging.DEBUG)
        finally:
            root_logger.setLevel(old_root_level)
            app_logger.setLevel(old_app_level)

    def test_configure_logging_suppresses_http_client_urls(self) -> None:
        """Временный codeload token не попадает в INFO/DEBUG-логи библиотек."""
        import logging

        root_logger = logging.getLogger()
        app_logger = logging.getLogger("obs_chat_bot")
        httpx_logger = logging.getLogger("httpx")
        httpcore_logger = logging.getLogger("httpcore")
        old_root_level = root_logger.level
        old_app_level = app_logger.level
        old_httpx_level = httpx_logger.level
        old_httpcore_level = httpcore_logger.level
        try:
            configure_logging()
            configure_debug_logging(True)

            self.assertEqual(httpx_logger.level, logging.WARNING)
            self.assertEqual(httpcore_logger.level, logging.WARNING)
        finally:
            root_logger.setLevel(old_root_level)
            app_logger.setLevel(old_app_level)
            httpx_logger.setLevel(old_httpx_level)
            httpcore_logger.setLevel(old_httpcore_level)

    def test_run_process_url_command_returns_zero_on_success(self) -> None:
        """Успешный pipeline возвращает нулевой exit code."""
        fake_use_case = FakeProcessArticleUrlUseCase()

        with TemporaryDirectory(prefix="obs-chat-bot-cli-") as temporary_directory:
            exit_code = run_process_url_command(
                database_path=Path(temporary_directory) / "test.db",
                source_url="https://example.com/article",
                logger=SilentLogger(),
                use_case_factory=lambda _connection: fake_use_case,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_use_case.commands[0].source_url, "https://example.com/article")

    def test_run_process_url_command_returns_one_on_pipeline_error(self) -> None:
        """Ошибка use case превращается в ненулевой exit code."""
        fake_use_case = FakeProcessArticleUrlUseCase(
            error=ProcessArticleUrlError("failed")
        )

        with TemporaryDirectory(prefix="obs-chat-bot-cli-") as temporary_directory:
            exit_code = run_process_url_command(
                database_path=Path(temporary_directory) / "test.db",
                source_url="https://example.com/article",
                logger=SilentLogger(),
                use_case_factory=lambda _connection: fake_use_case,
            )

        self.assertEqual(exit_code, 1)

    def test_run_telegram_bot_command_returns_zero_on_stop(self) -> None:
        """Штатное завершение Telegram adapter возвращает нулевой exit code."""
        fake_use_case = FakeProcessArticleUrlUseCase()
        with patch("obs_chat_bot.presentation.cli.main.run_telegram_bot") as runner:
            with TemporaryDirectory(prefix="obs-chat-bot-telegram-") as temporary_directory:
                exit_code = run_telegram_bot_command(
                    database_path=Path(temporary_directory) / "test.db",
                    token="123456789:abcdefghijklmnopqrstuvwxyz",
                    logger=SilentLogger(),
                    use_case_factory=lambda _connection: fake_use_case,
                )

        runner.assert_called_once()
        self.assertEqual(exit_code, 0)

    def test_run_telegram_bot_command_returns_one_on_adapter_error(self) -> None:
        """Ошибка Telegram adapter превращается в ненулевой exit code."""
        fake_use_case = FakeProcessArticleUrlUseCase()
        with patch(
            "obs_chat_bot.presentation.cli.main.run_telegram_bot",
            side_effect=TelegramBotError("failed"),
        ):
            with TemporaryDirectory(prefix="obs-chat-bot-telegram-") as temporary_directory:
                exit_code = run_telegram_bot_command(
                    database_path=Path(temporary_directory) / "test.db",
                    token="123456789:abcdefghijklmnopqrstuvwxyz",
                    logger=SilentLogger(),
                    use_case_factory=lambda _connection: fake_use_case,
                )

        self.assertEqual(exit_code, 1)

    def test_run_telegram_bot_command_passes_processor(self) -> None:
        """Telegram adapter получает processor вместо готового SQLite use case."""
        fake_use_case = FakeProcessArticleUrlUseCase()
        fake_analysis_use_case = FakeAnalyzeArticleUseCase()
        with patch("obs_chat_bot.presentation.cli.main.run_telegram_bot") as runner:
            with TemporaryDirectory(prefix="obs-chat-bot-telegram-") as temporary_directory:
                exit_code = run_telegram_bot_command(
                    database_path=Path(temporary_directory) / "test.db",
                    token="123456789:abcdefghijklmnopqrstuvwxyz",
                    logger=SilentLogger(),
                    use_case_factory=lambda _connection: fake_use_case,
                    analysis_use_case_factory=lambda _connection: fake_analysis_use_case,
                )

        processor = runner.call_args.kwargs["incoming_message_processor"]
        self.assertTrue(callable(processor))
        self.assertEqual(exit_code, 0)

    def test_run_telegram_bot_command_passes_proxy_to_adapter(self) -> None:
        """CLI передаёт Telegram-прокси только соответствующему adapter."""
        fake_use_case = FakeProcessArticleUrlUseCase()
        with patch("obs_chat_bot.presentation.cli.main.run_telegram_bot") as runner:
            with TemporaryDirectory(prefix="obs-chat-bot-telegram-") as directory:
                exit_code = run_telegram_bot_command(
                    database_path=Path(directory) / "test.db",
                    token="123456789:abcdefghijklmnopqrstuvwxyz",
                    proxy_url="socks5://10.77.77.1:1080",
                    logger=SilentLogger(),
                    use_case_factory=lambda _connection: fake_use_case,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            runner.call_args.kwargs["proxy_url"],
            "socks5://10.77.77.1:1080",
        )

    def test_run_telegram_bot_builds_one_process_github_coordinator(self) -> None:
        """Telegram runtime не пересоздаёт in-memory Device Flow на сообщение."""
        fake_use_case = FakeProcessArticleUrlUseCase()
        github_config = GitHubAppConfig(
            app_id=123,
            client_id="Iv1.client",
            app_slug="obs-chat-bot",
            private_key_path=Path("data/github-app.pem"),
        )
        with patch("obs_chat_bot.presentation.cli.main.validate_github_app_config", return_value=None), patch("obs_chat_bot.presentation.cli.main.run_telegram_bot"), patch(
            "obs_chat_bot.capabilities.obsidian.bootstrap.create_github_connection_coordinator"
        ) as coordinator_factory:
            with TemporaryDirectory(prefix="obs-chat-bot-telegram-") as directory:
                exit_code = run_telegram_bot_command(
                    database_path=Path(directory) / "test.db",
                    token="123456789:abcdefghijklmnopqrstuvwxyz",
                    github_app_config=github_config,
                    logger=SilentLogger(),
                    use_case_factory=lambda _connection: fake_use_case,
                )

        coordinator_factory.assert_called_once()
        self.assertEqual(exit_code, 0)

    def test_run_vk_bot_command_passes_processor(self) -> None:
        """VK adapter получает processor вместо готового SQLite use case."""
        fake_use_case = FakeProcessArticleUrlUseCase()
        fake_analysis_use_case = FakeAnalyzeArticleUseCase()
        with patch("obs_chat_bot.presentation.cli.main.run_vk_bot") as runner:
            with TemporaryDirectory(prefix="obs-chat-bot-vk-") as temporary_directory:
                exit_code = run_vk_bot_command(
                    database_path=Path(temporary_directory) / "test.db",
                    token="vk-token",
                    group_id=123,
                    logger=SilentLogger(),
                    use_case_factory=lambda _connection: fake_use_case,
                    analysis_use_case_factory=lambda _connection: fake_analysis_use_case,
                )

        processor = runner.call_args.kwargs["incoming_message_processor"]
        self.assertTrue(callable(processor))
        self.assertEqual(exit_code, 0)

    def test_run_vk_bot_command_returns_one_on_adapter_error(self) -> None:
        """Ошибка VK adapter превращается в ненулевой exit code."""
        with patch(
            "obs_chat_bot.presentation.cli.main.run_vk_bot",
            side_effect=VkBotError("failed"),
        ):
            with TemporaryDirectory(prefix="obs-chat-bot-vk-") as temporary_directory:
                exit_code = run_vk_bot_command(
                    database_path=Path(temporary_directory) / "test.db",
                    token="vk-token",
                    group_id=123,
                    logger=SilentLogger(),
                )

        self.assertEqual(exit_code, 1)

    def test_telegram_processor_opens_fresh_sqlite_connection(self) -> None:
        """Telegram processor собирает dependencies внутри обработки сообщения."""
        fake_use_case = FakeProcessArticleUrlUseCase(article_id=None)
        fake_analysis_use_case = FakeAnalyzeArticleUseCase()

        with TemporaryDirectory(prefix="obs-chat-bot-telegram-") as temporary_directory:
            database_path = Path(temporary_directory) / "test.db"
            initialize_database(database_path, SilentLogger())
            process_channel_incoming_message(
                database_path=database_path,
                incoming_message=_telegram_message("/register"),
                openai_base_url="https://llm.example/v1",
                openai_api_key="token",
                openai_model="fake-model",
                use_case_factory=lambda _connection: fake_use_case,
                analysis_use_case_factory=lambda _connection: fake_analysis_use_case,
            )
            process_channel_incoming_message(
                database_path=database_path,
                incoming_message=_telegram_message("Test User", "msg-name"),
                openai_base_url="https://llm.example/v1",
                openai_api_key="token",
                openai_model="fake-model",
                use_case_factory=lambda _connection: fake_use_case,
                analysis_use_case_factory=lambda _connection: fake_analysis_use_case,
            )
            with patch("obs_chat_bot.capabilities.obsidian.bootstrap.create_vault_selection_manager", return_value=FakeVaultSelectionManager(selected=_test_vault(1))):
                result = process_channel_incoming_message(
                    runtime=ObsidianProcessRuntime(None, object()),
                    database_path=database_path,
                    incoming_message=_telegram_message("/obsidian https://example.com/article", "msg-2"),
                    openai_base_url="https://llm.example/v1",
                    openai_api_key="token",
                    openai_model="fake-model",
                    use_case_factory=lambda _connection: fake_use_case,
                    analysis_use_case_factory=lambda _connection: fake_analysis_use_case,
                )

        self.assertIn("Готово: статья сохранена", result.text)
        self.assertEqual(fake_use_case.commands[0].source_url, "https://example.com/article")
        self.assertEqual(fake_analysis_use_case.commands, [])


def _telegram_message(text: str, message_id: str = "msg-1") -> IncomingEvent:
    """Создаёт Telegram incoming message для CLI processor-тестов."""
    return IncomingEvent(
        channel="telegram",
        chat_id="chat-1",
        message_id=message_id,
        text=text,
        external_user_id="user-1",
    )


if __name__ == "__main__":
    unittest.main()
