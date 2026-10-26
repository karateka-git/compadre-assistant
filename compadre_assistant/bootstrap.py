"""Собирает общие зависимости приложения и явно регистрирует возможности."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator

from compadre_assistant.capabilities.ask.bootstrap import create_ask_runtime
from compadre_assistant.capabilities.ask.data.configuration import AskConfig, load_ask_config
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability

from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    ChannelCompletionHandler,
    IncomingCapabilityPort,
    IncomingResponse,
)
from compadre_assistant.core.presentation.incoming.dispatcher import (
    IncomingMessageDispatcher,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.domain.users.identity import UserIdentityService
from compadre_assistant.capabilities.obsidian.bootstrap import (
    AnalyzeArticleUseCaseFactory,
    GitHubConnectionStarter,
    ObsidianProcessRuntime,
    ProcessArticleUrlUseCaseFactory,
    ObsidianCapability,
    obsidian_descriptor,
    create_obsidian_process_runtime,
    process_url_for_cli,
    validate_external_http_url,
    validate_github_app_config,
    ObsidianConfig,
    load_obsidian_config,
    ObsidianConfigError,
    EmbeddingConfig,
    GitHubAppConfig,
)
from compadre_assistant.capabilities.obsidian.smoke import (
    AnalysisSmokeError,
    PipelineSmokeError,
    SQLiteSmokeError,
    run_analysis_smoke,
    run_pipeline_smoke,
    run_sqlite_smoke,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreferenceRepository
from compadre_assistant.core.domain.chat_modes import ChatModeRepository
from compadre_assistant.core.data.sqlite.user_identity_repository import (
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
    capability_preference_repository: UserCapabilityPreferenceRepository,
    disabled_capabilities: tuple[CapabilityDescriptor, ...] = (),
) -> IncomingMessageDispatcher:
    """Явно регистрирует собранные возможности и общий сценарий профиля.

    Args:
        capabilities: Только включённые обработчики, включая пользовательские реализации.
        user_identity_service: Общий сервис проверенных identities.
        chat_mode_repository: Хранилище режима конкретного чата.
        capability_preference_repository: Хранилище личного выбора AppUser.
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
        capability_preference_repository=capability_preference_repository,
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
    unknown = set(enabled_capabilities) - {"obsidian", "ask"}
    if unknown:
        raise ValueError(
            f"Неизвестные возможности в конфигурации: {', '.join(sorted(unknown))}"
        )


def load_obsidian_profile(
    enabled_capabilities: tuple[str, ...],
) -> ObsidianConfig | None:
    """Загружает предметный профиль только для включённого Obsidian.

    Args:
        enabled_capabilities: Проверенные ID выбранных возможностей.

    Returns:
        Профиль Obsidian либо `None`, если возможность отключена.

    Raises:
        ObsidianConfigError: Настройки включённой возможности некорректны.
    """
    return load_obsidian_config() if "obsidian" in enabled_capabilities else None


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
                capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
            )
            return dispatcher.execute(incoming_message, completion_handler)


@contextmanager
def create_channel_message_processor(
    *, database_path: Path, runtime: ObsidianProcessRuntime | None = None,
    obsidian_config: ObsidianConfig | None = None,
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
    analysis_use_case_factory: AnalyzeArticleUseCaseFactory | None = None,
    enabled_capabilities: tuple[str, ...] = ("obsidian",),
    ask_config: AskConfig | None = None,
) -> Iterator[ChannelMessageProcessor]:
    """Создаёт процессные входы и закрывает принадлежащие им клиенты при выходе.

    Args:
        database_path: Общий путь SQLite для событий и предметных операций.
        runtime: Процессные зависимости Obsidian либо `None`.
        obsidian_config: Профиль настроек включённой возможности.
        use_case_factory: Подменная фабрика article pipeline для тестов.
        analysis_use_case_factory: Подменная фабрика анализа для тестов.
        enabled_capabilities: Явно включённые ID.
        ask_config: Профиль Ask либо None для чтения ASK_* при включении.

    Yields:
        Обработчик всех сообщений процесса с одним реестром.
    """
    try:
        validate_enabled_capabilities(enabled_capabilities)
        if "obsidian" in enabled_capabilities and obsidian_config is None:
            raise ValueError("Для включённого Obsidian требуется его профиль настроек")
        entry = ObsidianCapability(
            database_path=database_path,
            openai_base_url=obsidian_config.openai_base_url,
            openai_api_key=obsidian_config.openai_api_key,
            openai_model=obsidian_config.openai_model,
            runtime=runtime, use_case_factory=use_case_factory,
            analysis_use_case_factory=analysis_use_case_factory,
            chunking_config=obsidian_config.chunking,
            embedding_config=obsidian_config.embedding,
            created_note_tag=obsidian_config.obsidian_created_note_tag,
        ) if "obsidian" in enabled_capabilities else None
        with create_ask_runtime(enabled="ask" in enabled_capabilities, config=ask_config) as ask:
            capabilities = (entry,) if entry is not None else ()
            disabled = () if entry is not None else (
                obsidian_descriptor(),
            )
            if ask is not None:
                capabilities += (AskCapability(ask),)
            else:
                disabled += (AskCapability.descriptor,)
            yield ChannelMessageProcessor(
                database_path=database_path,
                capabilities=capabilities,
                disabled_capabilities=disabled,
            )
    finally:
        if runtime is not None:
            runtime.close()
