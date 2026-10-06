from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import tempfile
from pathlib import Path

from obs_chat_bot import __version__
import obs_chat_bot.bootstrap as app_bootstrap
from obs_chat_bot.bootstrap import (
    AnalyzeArticleUseCaseFactory,
    GitHubConnectionStarter,
    ProcessArticleUrlUseCaseFactory,
    create_obsidian_process_runtime,
    empty_obsidian_message_reply,
    process_url_for_cli,
    should_acknowledge_obsidian,
    validate_external_http_url,
    validate_github_app_config,
    AnalysisSmokeError,
    SQLiteSmokeError,
    PipelineSmokeError,
    run_analysis_smoke,
    run_sqlite_smoke,
    run_pipeline_smoke,
)
from obs_chat_bot.core.data.config import (
    AppConfig,
    ChunkingConfig,
    ConfigError,
    EmbeddingConfig,
    GitHubAppConfig,
    load_config,
)
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import MigrationError, apply_migrations
from obs_chat_bot.presentation.telegram.bot import TelegramBotError, run_telegram_bot
from obs_chat_bot.presentation.vk.bot import VkBotError, run_vk_bot


def configure_logging() -> None:
    """Настраивает единый формат логов приложения."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx пишет redirect URL целиком, а временная codeload-ссылка приватного
    # archive содержит credential в query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Читает аргументы командной строки приложения."""
    parser = argparse.ArgumentParser(prog="obs-chat-bot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--healthcheck",
        action="store_true",
        help="Validate configuration and writable data directory, then exit.",
    )
    mode.add_argument(
        "--sqlite-smoke",
        action="store_true",
        help="Run SQLite migrations and repository smoke scenario, then exit.",
    )
    mode.add_argument(
        "--pipeline-smoke",
        action="store_true",
        help="Run article pipeline smoke scenario without network, then exit.",
    )
    mode.add_argument(
        "--analysis-smoke",
        action="store_true",
        help="Run article analysis smoke scenario without LLM, then exit.",
    )
    mode.add_argument(
        "--process-url",
        metavar="URL",
        help="Run article pipeline for one URL, then exit.",
    )
    mode.add_argument(
        "--telegram-bot",
        action="store_true",
        help="Start Telegram bot polling.",
    )
    mode.add_argument(
        "--vk-bot",
        action="store_true",
        help="Start VK bot long polling.",
    )
    return parser.parse_args()


def main() -> int:
    """Запускает выбранный режим приложения и возвращает exit code."""
    args = parse_args()
    configure_logging()
    logger = logging.getLogger("obs_chat_bot")

    logger.info("Starting obsChatBot %s", __version__)

    if args.sqlite_smoke:
        return run_sqlite_smoke_command(logger)

    if args.pipeline_smoke:
        return run_pipeline_smoke_command(logger)

    if args.analysis_smoke:
        return run_analysis_smoke_command(logger)

    try:
        config = load_config()
    except ConfigError as error:
        logger.error("Configuration error: %s", error)
        return 2
    configure_debug_logging(config.app_debug)

    config.data_dir.mkdir(parents=True, exist_ok=True)

    if args.healthcheck:
        return run_healthcheck(config, logger)

    for key, value in config.safe_summary().items():
        logger.info("Config %s: %s", key, value)

    logger.info("Data directory: %s", config.data_dir.resolve())

    if not initialize_database(config.database_path, logger):
        return 1

    if args.process_url:
        return run_process_url_command(
            database_path=config.database_path,
            source_url=args.process_url,
            logger=logger,
        )

    if args.telegram_bot:
        return run_telegram_bot_command(
            database_path=config.database_path,
            token=config.telegram_bot_token,
            proxy_url=config.telegram_proxy_url,
            openai_base_url=config.openai_base_url,
            openai_api_key=config.openai_api_key,
            openai_model=config.openai_model,
            github_app_config=config.github_app,
            chunking_config=config.chunking,
            embedding_config=config.embedding,
            obsidian_created_note_tag=config.obsidian_created_note_tag,
            logger=logger,
        )

    if args.vk_bot:
        return run_vk_bot_command(
            database_path=config.database_path,
            token=config.vk_bot_token,
            group_id=config.vk_group_id,
            openai_base_url=config.openai_base_url,
            openai_api_key=config.openai_api_key,
            openai_model=config.openai_model,
            github_app_config=config.github_app,
            chunking_config=config.chunking,
            embedding_config=config.embedding,
            obsidian_created_note_tag=config.obsidian_created_note_tag,
            logger=logger,
        )

    logger.info("Configuration is ready")

    return 0


def configure_debug_logging(enabled: bool) -> None:
    """Включает расширенный debug-лог приложения по конфигурации."""
    if not enabled:
        return
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("obs_chat_bot").setLevel(logging.DEBUG)


TELEGRAM_TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


def run_healthcheck(config: AppConfig, logger: logging.Logger) -> int:
    """Проверяет runtime-конфигурацию, каталог данных и SQLite.

    Args:
        config: Загруженная конфигурация приложения.
        logger: Logger для диагностических сообщений.

    Returns:
        Ноль при успехе, иначе единицу.
    """
    try:
        with tempfile.NamedTemporaryFile(
            dir=config.database_path.parent,
            prefix="health-",
            delete=True,
        ):
            pass
    except OSError as error:
        logger.error("Health check failed: data directory is not writable: %s", error)
        return 1

    if not check_database(config.database_path, logger):
        return 1
    if not check_telegram_config(config.telegram_bot_token, logger):
        return 1
    if not check_llm_config(
        base_url=config.openai_base_url,
        api_key=config.openai_api_key,
        model=config.openai_model,
        logger=logger,
    ):
        return 1
    if not check_github_config(config.github_app, logger):
        return 1
    if not check_embedding_config(config.embedding, logger):
        return 1

    logger.info("Health check passed")
    return 0


def check_telegram_config(token: str, logger: logging.Logger) -> bool:
    """Проверяет базовую форму Telegram Bot API token без сетевого запроса."""
    if not TELEGRAM_TOKEN_PATTERN.fullmatch(token):
        logger.error("Telegram token has unexpected format")
        return False
    logger.info("Telegram configuration is ready")
    return True


def check_llm_config(
    *,
    base_url: str,
    api_key: str,
    model: str,
    logger: logging.Logger,
) -> bool:
    """Проверяет базовую LLM-конфигурацию без реального запроса к модели."""
    if not api_key.strip():
        logger.error("LLM API key is empty")
        return False
    if not model.strip():
        logger.error("LLM model is empty")
        return False
    error = validate_external_http_url(base_url)
    if error is not None:
        logger.error("LLM base URL is not safe: %s", error)
        return False
    logger.info("LLM configuration is ready")
    return True


def check_vk_config(
    *,
    token: str,
    group_id: int | None,
    logger: logging.Logger,
) -> bool:
    """Проверяет VK-конфигурацию для запуска VK adapter."""
    if not token.strip():
        logger.error("VK bot token is empty")
        return False
    if group_id is None or group_id <= 0:
        logger.error("VK group id is missing or invalid")
        return False
    logger.info("VK configuration is ready")
    return True


def check_github_config(
    config: GitHubAppConfig | None,
    logger: logging.Logger,
) -> bool:
    """Проверяет наличие PEM GitHub App без вывода ключа или пути."""
    if config is None:
        logger.info("GitHub App configuration is disabled")
        return True
    error = validate_github_app_config(config)
    if error is not None:
        logger.error("GitHub App configuration is invalid: %s", error)
        return False
    logger.info("GitHub App configuration is ready")
    return True


def check_embedding_config(
    config: EmbeddingConfig | None,
    logger: logging.Logger,
) -> bool:
    """Проверяет embedding URL и наличие credentials без сетевого запроса.

    Args:
        config: Настройки provider либо `None`, если semantic search отключён.
        logger: Logger безопасного результата проверки.

    Returns:
        `True`, если группа отключена или полностью корректна.
    """
    if config is None:
        logger.info("Embedding provider configuration is disabled")
        return True
    error = validate_external_http_url(config.base_url)
    if error is not None:
        logger.error("Embedding base URL is not safe: %s", error)
        return False
    logger.info("Embedding provider configuration is ready")
    return True


def check_database(database_path: Path, logger: logging.Logger) -> bool:
    """Проверяет возможность открыть рабочую SQLite-базу.

    Args:
        database_path: Путь к рабочему файлу SQLite.
        logger: Logger для диагностических сообщений.

    Returns:
        `True`, если соединение успешно открыто.
    """
    try:
        with connect_database(database_path):
            pass
    except (OSError, sqlite3.Error) as error:
        logger.error("Database connection failed: %s", error)
        return False

    logger.info("Database connection is ready")
    return True


def initialize_database(database_path: Path, logger: logging.Logger) -> bool:
    """Открывает рабочую базу и применяет ожидающие миграции.

    Args:
        database_path: Путь к рабочему файлу SQLite.
        logger: Logger для диагностических сообщений.

    Returns:
        `True`, если база готова к работе.
    """
    try:
        with connect_database(database_path) as connection:
            applied = apply_migrations(connection)
    except (MigrationError, OSError, sqlite3.Error) as error:
        logger.error("Database initialization failed: %s", error)
        return False

    for migration in applied:
        logger.info("Applied database migration: %s", migration.name)

    if not applied:
        logger.info("Database schema is up to date")

    logger.info("Database connection is ready")
    return True


def run_sqlite_smoke_command(logger: logging.Logger) -> int:
    """Запускает SQLite smoke-сценарий как команду приложения.

    Args:
        logger: Logger для результата проверки.

    Returns:
        Ноль при успехе, иначе единицу.
    """
    try:
        run_sqlite_smoke()
    except SQLiteSmokeError as error:
        logger.error("%s", error)
        return 1

    logger.info("SQLite smoke scenario passed")
    return 0


def run_pipeline_smoke_command(logger: logging.Logger) -> int:
    """Запускает smoke-сценарий article pipeline как команду приложения.

    Args:
        logger: Logger для результата проверки.

    Returns:
        Ноль при успехе, иначе единицу.
    """
    try:
        run_pipeline_smoke()
    except PipelineSmokeError as error:
        logger.error("%s", error)
        return 1

    logger.info("Pipeline smoke scenario passed")
    return 0


def run_analysis_smoke_command(logger: logging.Logger) -> int:
    """Запускает smoke-сценарий LLM-анализа как команду приложения.

    Args:
        logger: Logger для результата проверки.

    Returns:
        Ноль при успехе, иначе единицу.
    """
    try:
        run_analysis_smoke()
    except AnalysisSmokeError as error:
        logger.error("%s", error)
        return 1

    logger.info("Analysis smoke scenario passed")
    return 0


def run_process_url_command(
    *,
    database_path: Path,
    source_url: str,
    logger: logging.Logger,
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
) -> int:
    """Запускает article pipeline для одного URL из CLI.

    Args:
        database_path: Путь к рабочему файлу SQLite.
        source_url: URL статьи для обработки.
        logger: Logger для результата проверки.
        use_case_factory: Factory use case, полезная для тестов CLI без сети.

    Returns:
        Ноль при успешной обработке, иначе единицу.
    """
    try:
        with connect_database(database_path) as connection:
            apply_migrations(connection)
            result = process_url_for_cli(connection, source_url, use_case_factory)
    except (MigrationError, OSError, sqlite3.Error) as error:
        logger.error("Could not prepare article pipeline: %s", error)
        return 1
    if result.error is not None:
        logger.error("Article pipeline failed: %s", result.error)
        return 1

    logger.info("Article pipeline passed")
    logger.info("Article id: %s", result.article_id)
    logger.info("Article status: %s", result.status)
    logger.info("Article created: %s", result.created)
    logger.info("Article extracted: %s", result.extracted)
    logger.info("Article title: %s", result.title or "missing")
    logger.info(
        "Article cleaned text length: %s",
        result.cleaned_text_length,
    )

    return 0


def run_telegram_bot_command(
    *,
    database_path: Path,
    token: str,
    proxy_url: str | None = None,
    openai_base_url: str = "",
    openai_api_key: str = "",
    openai_model: str = "",
    github_app_config: GitHubAppConfig | None = None,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    embedding_config: EmbeddingConfig | None = None,
    obsidian_created_note_tag: str | None = "knowledge-catcher",
    logger: logging.Logger,
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
    analysis_use_case_factory: AnalyzeArticleUseCaseFactory | None = None,
    github_connection_starter: GitHubConnectionStarter | None = None,
) -> int:
    """Запускает Telegram adapter как CLI-команду.

    Args:
        database_path: Путь к рабочему файлу SQLite.
        token: Telegram Bot API token.
        proxy_url: Необязательный адрес прокси только для Telegram Bot API.
        openai_base_url: Базовый URL OpenAI-compatible API.
        openai_api_key: API key провайдера LLM.
        openai_model: Имя модели для анализа статей.
        github_app_config: Настройки GitHub App или `None`.
        chunking_config: Policy разбиения заметок на chunks.
        embedding_config: Настройки semantic index либо `None`.
        obsidian_created_note_tag: Служебный тег новых заметок или `None`.
        logger: Logger для результата запуска.
        use_case_factory: Factory use case, полезная для тестов без polling.
        analysis_use_case_factory: Factory use case анализа, полезная для тестов.
        github_connection_starter: Готовый coordinator для тестов.

    Returns:
        Ноль при штатной остановке, иначе единицу.
    """
    try:
        if not initialize_database(database_path, logger):
            return 1
        runtime = create_obsidian_process_runtime(
            database_path=database_path,
            github_app_config=github_app_config,
            github_connection_starter=github_connection_starter,
        )
        run_telegram_bot(
            token=token,
            proxy_url=proxy_url,
            incoming_message_processor=lambda incoming_message, completion_handler: (
                app_bootstrap.process_channel_incoming_message(
                    database_path=database_path,
                    incoming_message=incoming_message,
                    openai_base_url=openai_base_url,
                    openai_api_key=openai_api_key,
                    openai_model=openai_model,
                    use_case_factory=use_case_factory,
                    analysis_use_case_factory=analysis_use_case_factory,
                    runtime=runtime,
                    chunking_config=chunking_config,
                    embedding_config=embedding_config,
                    obsidian_created_note_tag=obsidian_created_note_tag,
                    completion_handler=completion_handler,
                )
            ),
            logger=logger,
            should_send_processing_ack=should_acknowledge_obsidian,
            empty_message_reply=empty_obsidian_message_reply(),
        )
    except KeyboardInterrupt:
        logger.info("Telegram bot stopped")
        return 0
    except (MigrationError, OSError, sqlite3.Error) as error:
        logger.error("Could not prepare Telegram bot: %s", error)
        return 1
    except TelegramBotError as error:
        logger.error("%s", error)
        return 1

    return 0


def run_vk_bot_command(
    *,
    database_path: Path,
    token: str,
    group_id: int | None,
    openai_base_url: str = "",
    openai_api_key: str = "",
    openai_model: str = "",
    github_app_config: GitHubAppConfig | None = None,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    embedding_config: EmbeddingConfig | None = None,
    obsidian_created_note_tag: str | None = "knowledge-catcher",
    logger: logging.Logger,
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
    analysis_use_case_factory: AnalyzeArticleUseCaseFactory | None = None,
    github_connection_starter: GitHubConnectionStarter | None = None,
) -> int:
    """Запускает VK adapter как CLI-команду.

    Args:
        database_path: Путь к рабочему файлу SQLite.
        token: VK group access token.
        group_id: ID VK-группы для Bots Long Poll.
        openai_base_url: Базовый URL OpenAI-compatible API.
        openai_api_key: API key провайдера LLM.
        openai_model: Имя модели для анализа статей.
        github_app_config: Настройки GitHub App или `None`.
        chunking_config: Policy разбиения заметок на chunks.
        embedding_config: Настройки semantic index либо `None`.
        obsidian_created_note_tag: Служебный тег новых заметок или `None`.
        logger: Logger для результата запуска.
        use_case_factory: Factory use case для тестов.
        analysis_use_case_factory: Factory analysis use case для тестов.
        github_connection_starter: Готовый coordinator для тестов.

    Returns:
        Ноль при штатной остановке, иначе единицу.
    """
    if not check_vk_config(token=token, group_id=group_id, logger=logger):
        return 2

    try:
        if not initialize_database(database_path, logger):
            return 1
        runtime = create_obsidian_process_runtime(
            database_path=database_path,
            github_app_config=github_app_config,
            github_connection_starter=github_connection_starter,
        )
        run_vk_bot(
            token=token,
            group_id=group_id,
            incoming_message_processor=lambda incoming_message, completion_handler: (
                app_bootstrap.process_channel_incoming_message(
                    database_path=database_path,
                    incoming_message=incoming_message,
                    openai_base_url=openai_base_url,
                    openai_api_key=openai_api_key,
                    openai_model=openai_model,
                    use_case_factory=use_case_factory,
                    analysis_use_case_factory=analysis_use_case_factory,
                    runtime=runtime,
                    chunking_config=chunking_config,
                    embedding_config=embedding_config,
                    obsidian_created_note_tag=obsidian_created_note_tag,
                    completion_handler=completion_handler,
                )
            ),
            logger=logger,
            should_send_processing_ack=should_acknowledge_obsidian,
            empty_message_reply=empty_obsidian_message_reply(),
        )
    except KeyboardInterrupt:
        logger.info("VK bot stopped")
        return 0
    except (MigrationError, OSError, sqlite3.Error) as error:
        logger.error("Could not prepare VK bot: %s", error)
        return 1
    except VkBotError as error:
        logger.error("%s", error)
        return 1

    return 0
