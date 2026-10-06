"""Собирает общие зависимости приложения и регистрирует возможность Obsidian."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import (
    ChannelCompletionHandler,
    IncomingCapabilityPort,
    IncomingResponse,
    IncomingResult,
)
from obs_chat_bot.core.presentation.incoming.dispatcher import (
    ProcessIncomingMessageResult,
    IncomingMessageDispatcher,
)
from obs_chat_bot.core.domain.users.identity import UserIdentityService
from obs_chat_bot.capabilities.obsidian.bootstrap import (
    AnalyzeArticleUseCaseFactory,
    GitHubConnectionStarter,
    ObsidianProcessRuntime,
    ProcessArticleUrlUseCaseFactory,
    create_obsidian_entry,
    create_obsidian_process_runtime,
    empty_obsidian_message_reply,
    present_obsidian_result,
    process_url_for_cli,
    should_acknowledge_obsidian,
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
from obs_chat_bot.core.presentation.responses import format_incoming_message_result


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
    incoming_capability: IncomingCapabilityPort,
    incoming_capability_id: str,
    user_identity_service: UserIdentityService | None,
    chat_mode_repository: ChatModeRepository | None = None,
) -> IncomingMessageDispatcher:
    """Подключает публичный вход возможности к общему сценарию пользователя.

    Args:
        incoming_capability: Вход зарегистрированной возможности.
        incoming_capability_id: Устойчивый ID подключённой возможности.
        user_identity_service: Общий сервис identities либо `None` для тестов.
        chat_mode_repository: Хранилище маршрута конкретного чата.

    Returns:
        Сценарий обработки сообщений любого канала.
    """
    return IncomingMessageDispatcher(
        incoming_capability=incoming_capability,
        incoming_capability_id=incoming_capability_id,
        user_identity_service=user_identity_service,
        chat_mode_repository=chat_mode_repository,
    )


def present_channel_result(result: IncomingResult) -> IncomingResponse:
    """Преобразует общий или зарегистрированный результат в ответ каналу.

    Raises:
        TypeError: Если результат не поддерживается текущей сборкой.
    """
    if isinstance(result, ProcessIncomingMessageResult):
        return IncomingResponse(format_incoming_message_result(result), result.error)
    capability_response = present_obsidian_result(result)
    if capability_response is not None:
        return capability_response
    raise TypeError(f"Unsupported incoming result: {type(result).__name__}")


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
        completion_handler: Доставка фонового ответа в исходный чат.

    Returns:
        Готовый нейтральный ответ каналу.
    """
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
        )
        use_case = create_incoming_message_dispatcher(
            incoming_capability=entry,
            incoming_capability_id="obsidian",
            user_identity_service=create_user_identity_service(connection),
            chat_mode_repository=SQLiteChatModeRepository(connection),
        )

        def present_completion(result: IncomingResult) -> None:
            """Передаёт фоновой результат каналу после форматирования."""
            if completion_handler is not None:
                completion_handler(present_channel_result(result))

        result = use_case.execute(
            incoming_message,
            present_completion if completion_handler is not None else None,
        )
        return present_channel_result(result)
