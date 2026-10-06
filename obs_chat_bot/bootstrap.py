"""Собирает общие зависимости приложения и регистрирует возможность Obsidian."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import (
    ChannelCompletionHandler,
    IncomingCapabilityPort,
    IncomingResponse,
)
from obs_chat_bot.core.presentation.incoming.dispatcher import (
    IncomingMessageDispatcher,
)
from obs_chat_bot.core.presentation.incoming.registry import CapabilityRegistry
from obs_chat_bot.core.presentation.incoming.ports import ProfileSetupPort
from obs_chat_bot.core.domain.users.identity import UserIdentityService
from obs_chat_bot.capabilities.obsidian.bootstrap import (
    AnalyzeArticleUseCaseFactory,
    GitHubConnectionStarter,
    ObsidianProcessRuntime,
    ProcessArticleUrlUseCaseFactory,
    create_obsidian_entry,
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
    )


def create_incoming_message_dispatcher(
    *,
    capabilities: tuple[IncomingCapabilityPort, ...],
    user_identity_service: UserIdentityService,
    chat_mode_repository: ChatModeRepository,
    profile_setup: ProfileSetupPort | None = None,
    registration_capability_id: str | None = None,
) -> IncomingMessageDispatcher:
    """Явно регистрирует собранные возможности и общий сценарий профиля.

    Args:
        capabilities: Только включённые обработчики, включая пользовательские реализации.
        user_identity_service: Общий сервис проверенных identities.
        chat_mode_repository: Хранилище режима конкретного чата.
        profile_setup: Временный прежний onboarding до шага 2.3.
        registration_capability_id: Вход для продолжения прежнего onboarding.

    Returns:
        Диспетчер с проверенным реестром точек входа.

    Raises:
        ValueError: Реестр содержит конфликт ID или команд входа.
    """
    return IncomingMessageDispatcher(
        registry=CapabilityRegistry(capabilities),
        user_identity_service=user_identity_service,
        chat_mode_repository=chat_mode_repository,
        profile_setup=profile_setup,
        registration_capability_id=registration_capability_id,
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


def process_channel_incoming_message(
    *,
    database_path: Path,
    incoming_message: IncomingEvent,
    openai_base_url: str,
    openai_api_key: str,
    openai_model: str,
    runtime: ObsidianProcessRuntime | None = None,
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
    analysis_use_case_factory: AnalyzeArticleUseCaseFactory | None = None,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    embedding_config: EmbeddingConfig | None = None,
    obsidian_created_note_tag: str | None = "knowledge-catcher",
    completion_handler: ChannelCompletionHandler | None = None,
    enabled_capabilities: tuple[str, ...] = ("obsidian",),
) -> IncomingResponse:
    """Обрабатывает одно событие канала с отдельным SQLite-соединением.

    Args:
        database_path: Путь к общей SQLite-БД.
        incoming_message: Нормализованное событие канала.
        openai_base_url: Базовый URL OpenAI-compatible API.
        openai_api_key: API key LLM.
        openai_model: Модель анализа и proposal.
        runtime: Процессные зависимости Obsidian.
        use_case_factory: Тестовая фабрика article pipeline.
        analysis_use_case_factory: Тестовая фабрика анализа.
        chunking_config: Настройки разбиения заметок.
        embedding_config: Настройки semantic index.
        obsidian_created_note_tag: Служебный тег новых заметок.
        completion_handler: Доставка фонового ответа в сохранённый адрес канала.
        enabled_capabilities: Явно включённые ID текущей сборки.

    Returns:
        Готовый нейтральный ответ каналу.
    """
    validate_enabled_capabilities(enabled_capabilities)
    with connect_database(database_path) as connection:
        entry = create_obsidian_entry(
            connection,
            database_path=database_path,
            openai_base_url=openai_base_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            runtime=runtime,
            use_case_factory=use_case_factory,
            analysis_use_case_factory=analysis_use_case_factory,
            chunking_config=chunking_config,
            embedding_config=embedding_config,
            created_note_tag=obsidian_created_note_tag,
        ) if "obsidian" in enabled_capabilities else None
        use_case = create_incoming_message_dispatcher(
            capabilities=(entry,) if entry else (),
            profile_setup=entry,
            registration_capability_id="obsidian" if entry else None,
            user_identity_service=create_user_identity_service(connection),
            chat_mode_repository=SQLiteChatModeRepository(connection),
        )

        return use_case.execute(incoming_message, completion_handler)
