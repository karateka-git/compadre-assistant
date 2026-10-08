"""Собирает общие зависимости приложения и регистрирует возможность Obsidian."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator

from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import (
    ChannelCompletionHandler,
    CapabilityDescriptor,
    IncomingCapabilityPort,
    IncomingResponse,
)
from obs_chat_bot.core.presentation.incoming.dispatcher import (
    IncomingMessageDispatcher,
)
from obs_chat_bot.core.presentation.incoming.registry import CapabilityRegistry
from obs_chat_bot.core.domain.users.identity import UserIdentityService
from obs_chat_bot.capabilities.obsidian.bootstrap import (
    AnalyzeArticleUseCaseFactory,
    GitHubConnectionStarter,
    ObsidianProcessRuntime,
    ProcessArticleUrlUseCaseFactory,
    ObsidianCapability,
    create_obsidian_process_runtime,
    process_url_for_cli,
    validate_external_http_url,
    validate_github_app_config,
)
from obs_chat_bot.capabilities.obsidian.smoke import (
    AnalysisSmokeError,
    PipelineSmokeError,
    SQLiteSmokeError,
    run_analysis_smoke,
    run_pipeline_smoke,
    run_sqlite_smoke,
)
from obs_chat_bot.core.data.config import ChunkingConfig, EmbeddingConfig
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from obs_chat_bot.core.domain.chat_modes import ChatModeRepository
from obs_chat_bot.core.data.sqlite.user_identity_repository import (
    SQLiteAppUserRepository,
    SQLiteExternalIdentityRepository,
    SQLiteIdentityLinkTokenRepository,
    SQLiteIdentityRebindConfirmationRepository,
    SQLiteIdentityLifecycleRepository,
)


def create_user_identity_service(connection: sqlite3.Connection) -> UserIdentityService:
    """Собирает сервис регистрации пользователей и привязки внешних каналов."""
    return UserIdentityService(
        app_user_repository=SQLiteAppUserRepository(connection),
        external_identity_repository=SQLiteExternalIdentityRepository(connection),
        link_token_repository=SQLiteIdentityLinkTokenRepository(connection),
        rebind_confirmation_repository=SQLiteIdentityRebindConfirmationRepository(
            connection
        ),
        lifecycle_repository=SQLiteIdentityLifecycleRepository(connection),
    )


def create_incoming_message_dispatcher(
    *,
    capabilities: tuple[IncomingCapabilityPort, ...],
    user_identity_service: UserIdentityService,
    chat_mode_repository: ChatModeRepository,
    disabled_capabilities: tuple[CapabilityDescriptor, ...] = (),
) -> IncomingMessageDispatcher:
    """Явно регистрирует собранные возможности и общий сценарий профиля.

    Args:
        capabilities: Только включённые обработчики, включая пользовательские реализации.
        user_identity_service: Общий сервис проверенных identities.
        chat_mode_repository: Хранилище режима конкретного чата.
        disabled_capabilities: Метаданные отключённых входов без внешних зависимостей.

    Returns:
        Диспетчер с проверенным реестром точек входа.

    Raises:
        ValueError: Реестр содержит конфликт ID или команд входа.
    """
    return IncomingMessageDispatcher(
        registry=CapabilityRegistry(capabilities, disabled_capabilities),
        user_identity_service=user_identity_service,
        chat_mode_repository=chat_mode_repository,
    )


def validate_enabled_capabilities(enabled_capabilities: tuple[str, ...]) -> None:
    """Проверяет выбранные ID относительно явно доступных сборок приложения.

    Args:
        enabled_capabilities: ID из конфигурации запуска.

    Raises:
        ValueError: Список содержит повторный или неизвестный ID.
    """
    if len(set(enabled_capabilities)) != len(enabled_capabilities):
        raise ValueError("Конфликт ID включённых возможностей")
    unknown = set(enabled_capabilities) - {"obsidian"}
    if unknown:
        raise ValueError(
            f"Неизвестные возможности в конфигурации: {', '.join(sorted(unknown))}"
        )


class ChannelMessageProcessor:
    """Держит один реестр процесса и открывает core scope на каждое событие."""

    def __init__(self, *, database_path: Path,
                 capabilities: tuple[IncomingCapabilityPort, ...],
                 disabled_capabilities: tuple[CapabilityDescriptor, ...] = ()) -> None:
        """Регистрирует обработчики при запуске без соединения с SQLite.

        Args:
            database_path: Общий путь SQLite для коротких core scopes.
            capabilities: Уже собранные входы включённых возможностей.
            disabled_capabilities: Метаданные отключённых входов.

        Raises:
            ValueError: Повторный ID или команда входа в реестре.
        """
        self._database_path = database_path
        self._registry = CapabilityRegistry(capabilities, disabled_capabilities)

    def __call__(self, incoming_message: IncomingEvent,
                 completion_handler: ChannelCompletionHandler | None = None) -> IncomingResponse:
        """Читает identity и режим в коротком независимом core scope."""
        with connect_database(self._database_path) as connection:
            dispatcher = IncomingMessageDispatcher(
                registry=self._registry,
                user_identity_service=create_user_identity_service(connection),
                chat_mode_repository=SQLiteChatModeRepository(connection),
            )
            return dispatcher.execute(incoming_message, completion_handler)


@contextmanager
def create_channel_message_processor(
    *, database_path: Path, runtime: ObsidianProcessRuntime | None = None,
    openai_base_url: str = "", openai_api_key: str = "", openai_model: str = "",
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
    analysis_use_case_factory: AnalyzeArticleUseCaseFactory | None = None,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    embedding_config: EmbeddingConfig | None = None,
    obsidian_created_note_tag: str | None = "knowledge-catcher",
    enabled_capabilities: tuple[str, ...] = ("obsidian",),
) -> Iterator[ChannelMessageProcessor]:
    """Создаёт процессные входы и закрывает принадлежащие им клиенты при выходе.

    Args:
        database_path: Общий путь SQLite для событий и предметных операций.
        runtime: Процессные зависимости Obsidian либо `None`.
        openai_base_url: Endpoint LLM.
        openai_api_key: Ключ LLM при включённой конфигурации.
        openai_model: Модель анализа Obsidian.
        use_case_factory: Подменная фабрика article pipeline для тестов.
        analysis_use_case_factory: Подменная фабрика анализа для тестов.
        chunking_config: Настройки разбиения Markdown.
        embedding_config: Необязательный профиль embeddings.
        obsidian_created_note_tag: Служебный тег новой заметки.
        enabled_capabilities: Явно включённые ID.

    Yields:
        Обработчик всех сообщений процесса с одним реестром.
    """
    try:
        validate_enabled_capabilities(enabled_capabilities)
        entry = ObsidianCapability(
            database_path=database_path, openai_base_url=openai_base_url,
            openai_api_key=openai_api_key, openai_model=openai_model,
            runtime=runtime, use_case_factory=use_case_factory,
            analysis_use_case_factory=analysis_use_case_factory,
            chunking_config=chunking_config, embedding_config=embedding_config,
            created_note_tag=obsidian_created_note_tag,
        ) if "obsidian" in enabled_capabilities else None
        processor = ChannelMessageProcessor(
            database_path=database_path,
            capabilities=(entry,) if entry is not None else (),
            disabled_capabilities=() if entry is not None else (
                CapabilityDescriptor("obsidian", "/obsidian", "работа со статьями и Obsidian vault"),
            ),
        )
        yield processor
    finally:
        if runtime is not None:
            runtime.close()
