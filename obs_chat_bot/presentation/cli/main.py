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
    process_url_for_cli,
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
    parse_channels,
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
    parser.add_argument(
        "--channels", type=parse_channels,
        help="Channels to validate: telegram, vk or telegram,vk (healthcheck/configuration).",
    )
    args = parser.parse_args()
    if args.channels is not None and any((
        args.telegram_bot, args.vk_bot, args.process_url,
        args.sqlite_smoke, args.pipeline_smoke, args.analysis_smoke,
    )):
        parser.error("--channels applies only to --healthcheck or configuration checks")
    return args


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
        channels = (
            ("telegram",) if args.telegram_bot else
            ("vk",) if args.vk_bot else
            () if args.process_url else args.channels
        )
        config = load_config(
            channels=channels,
            include_capability_settings=not bool(args.process_url),
        )
        app_bootstrap.validate_enabled_capabilities(config.enabled_capabilities)
    except (ConfigError, ValueError) as error:
        logger.error("Configuration error: %s", error)
        return 2
    configure_debug_logging(config.app_debug)

    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logger.error("Data directory is not available: %s", error)
        return 1

    if args.healthcheck:
        return run_healthcheck(config, logger)

    if not args.process_url and not check_runtime_configuration(config, logger):
        return 2

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
            enabled_capabilities=config.enabled_capabilities,
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
            enabled_capabilities=config.enabled_capabilities,
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

    # Выполняем независимые проверки целиком, чтобы первый сбой не скрывал другие.
    configuration_ready = check_runtime_configuration(config, logger)
    database_ready = check_database(config.database_path, logger)
    if not configuration_ready or not database_ready:
        return 1

    logger.info("Health check passed")
    return 0


def check_runtime_configuration(config: AppConfig, logger: logging.Logger) -> bool:
    """Проверяет выбранные каналы и включённые возможности без внешних API.

    Args:
        config: Настройки текущего запуска.
        logger: Logger отдельных результатов каналов и провайдеров.

    Returns:
        `True`, если запуск выбранной конфигурации разрешён.
    """
    try:
        app_bootstrap.validate_enabled_capabilities(config.enabled_capabilities)
    except ValueError as error:
        logger.error("Capability configuration is invalid: %s", error)
        return False
    results = []
    for channel in config.selected_channels:
        if channel == "telegram":
            results.append(check_telegram_config(config.telegram_bot_token, logger))
        elif channel == "vk":
            results.append(check_vk_config(token=config.vk_bot_token, group_id=config.vk_group_id, logger=logger))
        else:
            logger.error("Unknown selected channel: %s", channel)
            results.append(False)
    results.append(check_capability_configuration(
        enabled_capabilities=config.enabled_capabilities,
        openai_base_url=config.openai_base_url,
        openai_api_key=config.openai_api_key,
        openai_model=config.openai_model,
        github_app_config=config.github_app,
        embedding_config=config.embedding,
        logger=logger,
    ))
    return all(results)


def check_capability_configuration(
    *,
    enabled_capabilities: tuple[str, ...],
    openai_base_url: str,
    openai_api_key: str,
    openai_model: str,
    github_app_config: GitHubAppConfig | None,
    embedding_config: EmbeddingConfig | None,
    logger: logging.Logger,
) -> bool:
    """Проверяет провайдеры включённого Obsidian и сообщает доступность функций.

    Полностью пустые группы GitHub/LLM сохраняют доступность общих команд;
    обработчик Obsidian сообщает о ненастроенном провайдере. Частичные или
    некорректные группы блокируют запуск. Отсутствие embeddings включает FTS.

    Args:
        enabled_capabilities: ID возможностей текущей сборки.
        openai_base_url: Endpoint анализа/review Obsidian.
        openai_api_key: Отдельный ключ LLM.
        openai_model: Модель анализа/review.
        github_app_config: Полная группа GitHub либо `None`.
        embedding_config: Полная группа embeddings либо `None` для FTS.
        logger: Logger безопасных диагностических сообщений.

    Returns:
        `True`, если конфигурация провайдеров допускает запуск.
    """
    if "obsidian" not in enabled_capabilities:
        logger.info("Obsidian is disabled; provider configuration is not checked")
        return True
    llm_values = (openai_base_url.strip(), openai_api_key.strip(), openai_model.strip())
    if not any(llm_values):
        logger.info("Obsidian LLM is not configured; article analysis/review unavailable")
        llm_ready = True
    else:
        llm_ready = check_llm_config(
            base_url=openai_base_url, api_key=openai_api_key,
            model=openai_model, logger=logger,
        )
    github_ready = check_github_config(github_app_config, logger)
    embedding_ready = check_embedding_config(embedding_config, logger)
    return llm_ready and github_ready and embedding_ready


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
        logger.error("LLM API key (OPENAI_API_KEY) is empty")
        return False
    if not model.strip():
        logger.error("LLM model (OPENAI_MODEL) is empty")
        return False
    error = validate_external_http_url(base_url)
    if error is not None:
        logger.error("LLM base URL (OPENAI_BASE_URL) is not safe: %s", error)
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
        logger.info("Obsidian GitHub is not configured; vault connection/sync/write unavailable")
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
        logger.info("Obsidian embeddings are not configured; FTS fallback is active")
        return True
    error = validate_external_http_url(config.base_url)
    if error is not None:
        logger.error("Embedding base URL (EMBEDDING_BASE_URL) is not safe: %s", error)
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
    enabled_capabilities: tuple[str, ...] = ("obsidian",),
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
        enabled_capabilities: Явно подключённые ID возможностей.

    Returns:
        Ноль при штатной остановке, иначе единицу.
    """
    if not check_telegram_config(token, logger):
        return 2
    if not check_capability_configuration(
        enabled_capabilities=enabled_capabilities,
        openai_base_url=openai_base_url, openai_api_key=openai_api_key,
        openai_model=openai_model, github_app_config=github_app_config,
        embedding_config=embedding_config, logger=logger,
    ):
        return 2
    try:
        app_bootstrap.validate_enabled_capabilities(enabled_capabilities)
        if not initialize_database(database_path, logger):
            return 1
        runtime = create_obsidian_process_runtime(
            database_path=database_path,
            github_app_config=github_app_config,
            github_connection_starter=github_connection_starter,
            openai_base_url=openai_base_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            embedding_config=embedding_config,
            chunking_config=chunking_config,
        ) if "obsidian" in enabled_capabilities else None
        with app_bootstrap.create_channel_message_processor(
            database_path=database_path, runtime=runtime,
            openai_base_url=openai_base_url, openai_api_key=openai_api_key,
            openai_model=openai_model, use_case_factory=use_case_factory,
            analysis_use_case_factory=analysis_use_case_factory,
            chunking_config=chunking_config, embedding_config=embedding_config,
            obsidian_created_note_tag=obsidian_created_note_tag,
            enabled_capabilities=enabled_capabilities,
        ) as processor:
            run_telegram_bot(
                token=token, proxy_url=proxy_url,
                incoming_message_processor=processor, logger=logger,
            )
    except KeyboardInterrupt:
        logger.info("Telegram bot stopped")
        return 0
    except (MigrationError, OSError, sqlite3.Error, ValueError) as error:
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
    enabled_capabilities: tuple[str, ...] = ("obsidian",),
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
        enabled_capabilities: Явно подключённые ID возможностей.

    Returns:
        Ноль при штатной остановке, иначе единицу.
    """
    if not check_vk_config(token=token, group_id=group_id, logger=logger):
        return 2
    if not check_capability_configuration(
        enabled_capabilities=enabled_capabilities,
        openai_base_url=openai_base_url, openai_api_key=openai_api_key,
        openai_model=openai_model, github_app_config=github_app_config,
        embedding_config=embedding_config, logger=logger,
    ):
        return 2

    try:
        app_bootstrap.validate_enabled_capabilities(enabled_capabilities)
        if not initialize_database(database_path, logger):
            return 1
        runtime = create_obsidian_process_runtime(
            database_path=database_path,
            github_app_config=github_app_config,
            github_connection_starter=github_connection_starter,
            openai_base_url=openai_base_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            embedding_config=embedding_config,
            chunking_config=chunking_config,
        ) if "obsidian" in enabled_capabilities else None
        with app_bootstrap.create_channel_message_processor(
            database_path=database_path, runtime=runtime,
            openai_base_url=openai_base_url, openai_api_key=openai_api_key,
            openai_model=openai_model, use_case_factory=use_case_factory,
            analysis_use_case_factory=analysis_use_case_factory,
            chunking_config=chunking_config, embedding_config=embedding_config,
            obsidian_created_note_tag=obsidian_created_note_tag,
            enabled_capabilities=enabled_capabilities,
        ) as processor:
            run_vk_bot(
                token=token, group_id=group_id,
                incoming_message_processor=processor, logger=logger,
            )
    except KeyboardInterrupt:
        logger.info("VK bot stopped")
        return 0
    except (MigrationError, OSError, sqlite3.Error, ValueError) as error:
        logger.error("Could not prepare VK bot: %s", error)
        return 1
    except VkBotError as error:
        logger.error("%s", error)
        return 1

    return 0
