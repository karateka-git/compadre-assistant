from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from document_chunker import (
    ChunkingPolicy,
    DocumentChunker,
    DocumentFormat,
    MarkdownDocumentParser,
)

from obs_chat_bot.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleUseCase
from obs_chat_bot.capabilities.obsidian.domain.articles.ports import IncomingMessageRepository
from obs_chat_bot.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlUseCase
from obs_chat_bot.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlCommand, ProcessArticleUrlError
from obs_chat_bot.core.presentation.incoming.ports import IncomingResponse, IncomingResult
from obs_chat_bot.capabilities.obsidian.presentation.processing import ObsidianMessageHandler, ObsidianMessageResult, should_acknowledge
from obs_chat_bot.capabilities.obsidian.presentation.responses import EMPTY_MESSAGE_REPLY, format_obsidian_message_result
from obs_chat_bot.capabilities.obsidian.domain.reviews.proposal import PrepareObsidianReviewUseCase
from obs_chat_bot.capabilities.obsidian.domain.reviews.confirmation import (
    ObsidianProposalConfirmationService,
)
from obs_chat_bot.capabilities.obsidian.domain.search.full_text import VaultFullTextSearchService
from obs_chat_bot.capabilities.obsidian.domain.search.hybrid import (
    VaultFtsFallbackSearchService,
    VaultHybridSearchService,
)
from obs_chat_bot.capabilities.obsidian.domain.search.ports import EmbeddingProvider, VaultNoteChunker
from obs_chat_bot.capabilities.obsidian.domain.search.vector import VaultVectorSearchService
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_connection import (
    GitHubConnectionCoordinator,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import (
    GitHubConnectionStarter,
    GitHubRepositoryGateway,
    GitHubVaultGateway,
    GitHubVaultWriteGateway,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_selection import VaultSelectionManager
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_sync import VaultSyncManager
from obs_chat_bot.capabilities.obsidian.data.chunking.document_note_chunker import DocumentVaultNoteChunker
from obs_chat_bot.core.data.config import ChunkingConfig, EmbeddingConfig, GitHubAppConfig
from obs_chat_bot.capabilities.obsidian.data.embeddings.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from obs_chat_bot.capabilities.obsidian.data.extraction.trafilatura_article_extractor import (
    TrafilaturaArticleTextExtractor,
)
from obs_chat_bot.capabilities.obsidian.data.http.article_html_fetcher import UrllibArticleHtmlFetcher
from obs_chat_bot.capabilities.obsidian.data.http.url_safety import UnsafeUrlError, validate_public_http_url
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import GitHubGatewayError
from obs_chat_bot.capabilities.obsidian.data.llm.openai_article_analyzer import OpenAIArticleAnalyzer
from obs_chat_bot.capabilities.obsidian.data.llm.openai_obsidian_proposal_generator import (
    OpenAIObsidianProposalGenerator,
)
from obs_chat_bot.capabilities.obsidian.data.github.github_app_client import HttpxGitHubAppClient
from obs_chat_bot.capabilities.obsidian.data.github.jwt_signer import PyJwtGitHubAppSigner
from obs_chat_bot.capabilities.obsidian.data.sqlite.analysis_result_repository import (
    SQLiteArticleAnalysisResultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from obs_chat_bot.capabilities.obsidian.data.sqlite.incoming_message_repository import (
    SQLiteIncomingMessageRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_installation_writer import (
    SQLiteGitHubAccountAccessWriter,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_connection_state_store import (
    SQLiteGitHubConnectionStateStore,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_vault_selection_manager import (
    SQLiteGitHubVaultSelectionManager,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_vault_sync_manager import (
    SQLiteGitHubVaultSyncManager,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.processing_error_repository import (
    SQLiteProcessingErrorRecorder,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import (
    SQLiteObsidianProposalRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import (
    SQLiteVaultSyncLeaseRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_full_text_search_repository import (
    SQLiteVaultFullTextSearchRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import (
    SQLiteVaultChunkIndexRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_embedding_index_repository import (
    SQLiteVaultEmbeddingIndexRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_instruction_repository import (
    SQLiteVaultInstructionRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository


ProcessArticleUrlUseCaseFactory = Callable[
    [sqlite3.Connection],
    ProcessArticleUrlUseCase,
]
AnalyzeArticleUseCaseFactory = Callable[
    [sqlite3.Connection],
    AnalyzeArticleUseCase,
]


@dataclass(frozen=True, slots=True)
class ObsidianProcessRuntime:
    """Хранит процессные GitHub-зависимости для всех сообщений одного бота.

    Attributes:
        connection_starter: Coordinator с in-memory Device Flow sessions.
        repository_gateway: Общий HTTP-клиент для работы с vault.
    """

    connection_starter: GitHubConnectionStarter | None
    repository_gateway: GitHubRepositoryGateway | None


@dataclass(frozen=True, slots=True)
class ProcessUrlReport:
    """Передаёт CLI сведения о результате article pipeline без domain-модели.

    Attributes:
        article_id: Идентификатор сохранённой статьи.
        status: Текущий статус статьи.
        created: Признак создания новой статьи.
        extracted: Признак извлечения текста.
        title: Заголовок статьи.
        cleaned_text_length: Длина очищенного текста.
        error: Сообщение об ошибке pipeline либо `None`.
    """

    article_id: int | None = None
    status: str | None = None
    created: bool = False
    extracted: bool = False
    title: str | None = None
    cleaned_text_length: int = 0
    error: str | None = None


def process_url_for_cli(
    connection: sqlite3.Connection,
    source_url: str,
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
) -> ProcessUrlReport:
    """Выполняет article pipeline и возвращает нейтральный отчёт для CLI.

    Args:
        connection: Открытое SQLite-соединение приложения.
        source_url: URL статьи.
        use_case_factory: Необязательная тестовая фабрика сценария.

    Returns:
        Сведения о статье или текст ошибки pipeline.
    """
    use_case = (use_case_factory or create_process_article_url_use_case)(connection)
    try:
        result = use_case.execute(ProcessArticleUrlCommand(source_url=source_url))
    except ProcessArticleUrlError as error:
        return ProcessUrlReport(error=str(error))
    article = result.article
    return ProcessUrlReport(
        article_id=article.id,
        status=article.status.value,
        created=result.created,
        extracted=result.extracted,
        title=article.title,
        cleaned_text_length=len(article.cleaned_text or ""),
    )


def validate_github_app_config(config: GitHubAppConfig) -> str | None:
    """Проверяет подпись GitHub App и возвращает причину ошибки без секрета.

    Args:
        config: Настройки приложения GitHub.

    Returns:
        Текст ошибки либо `None` при корректной подписи.
    """
    try:
        PyJwtGitHubAppSigner(
            client_id=config.client_id,
            private_key_path=config.private_key_path,
        ).create()
    except GitHubGatewayError as error:
        return str(error)
    return None


def validate_external_http_url(url: str) -> str | None:
    """Проверяет публичный HTTP URL из конфигурации приложения.

    Args:
        url: Адрес внешнего API.

    Returns:
        Текст ошибки либо `None` для безопасного URL.
    """
    try:
        validate_public_http_url(url)
    except (UnsafeUrlError, ValueError) as error:
        return str(error)
    return None


def present_obsidian_result(result: IncomingResult) -> IncomingResponse | None:
    """Преобразует результат Obsidian в нейтральный ответ канала.

    Args:
        result: Результат общего входа или возможности.

    Returns:
        Нейтральный ответ либо `None` для чужого результата.
    """
    if isinstance(result, ObsidianMessageResult):
        return IncomingResponse(format_obsidian_message_result(result), result.error)
    return None


def should_acknowledge_obsidian(text: str) -> bool:
    """Возвращает предикат ранней квитанции обработки Obsidian."""
    return should_acknowledge(text)


def empty_obsidian_message_reply() -> str:
    """Возвращает текст подсказки для пустого сообщения Obsidian."""
    return EMPTY_MESSAGE_REPLY


def create_obsidian_process_runtime(
    *,
    database_path: Path,
    github_app_config: GitHubAppConfig | None,
    github_connection_starter: GitHubConnectionStarter | None = None,
) -> ObsidianProcessRuntime:
    """Создаёт один GitHub gateway и coordinator на процесс бота.

    Args:
        database_path: Путь к общей SQLite-БД.
        github_app_config: Настройки GitHub App либо `None`.
        github_connection_starter: Готовый coordinator для тестов.

    Returns:
        Процессные зависимости, переиспользуемые worker threads.
    """
    gateway = create_github_app_client(github_app_config) if github_app_config else None
    starter = github_connection_starter
    if starter is None and github_app_config is not None:
        starter = create_github_connection_coordinator(
            database_path=database_path,
            config=github_app_config,
            gateway=gateway,
        )
    return ObsidianProcessRuntime(starter, gateway)


def create_vault_note_chunker(config: ChunkingConfig) -> VaultNoteChunker:
    """Собирает project adapter независимого Markdown document chunker.

    Args:
        config: Технические размеры chunks из конфигурации приложения.

    Returns:
        Adapter, добавляющий к универсальным chunks project IDs заметки.
    """
    engine = DocumentChunker(
        parsers={
            DocumentFormat.MARKDOWN: MarkdownDocumentParser(),
        },
        policy=ChunkingPolicy(
            minimum_size_chars=config.minimum_size_chars,
            target_size_chars=config.target_size_chars,
            maximum_size_chars=config.maximum_size_chars,
        ),
    )
    return DocumentVaultNoteChunker(engine)


def create_vault_full_text_search_service(
    connection: sqlite3.Connection,
    *,
    chunking_config: ChunkingConfig = ChunkingConfig(),
) -> VaultFullTextSearchService:
    """Собирает поиск FTS5 с проверкой текущей chunking signature.

    Args:
        connection: Открытое SQLite-соединение приложения.
        chunking_config: Policy, определяющая допустимое поколение chunks.

    Returns:
        Application-сервис изолированного полнотекстового поиска по vault.
    """
    return VaultFullTextSearchService(
        repository=SQLiteVaultFullTextSearchRepository(connection),
        chunker=create_vault_note_chunker(chunking_config),
    )


def create_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    """Собирает OpenAI-compatible embedding adapter.

    Args:
        config: Отдельная конфигурация semantic embedding provider.

    Returns:
        Adapter embedding API за application-owned port.
    """
    return OpenAICompatibleEmbeddingProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        document_model=config.document_model,
        query_model=config.query_model,
        price_per_million_tokens=config.price_per_million_tokens,
        price_currency=config.price_currency,
        tariff_version=config.tariff_version,
    )


def create_vault_hybrid_search_service(
    connection: sqlite3.Connection,
    *,
    embedding_config: EmbeddingConfig,
    chunking_config: ChunkingConfig = ChunkingConfig(),
) -> VaultHybridSearchService:
    """Собирает BM25, cosine search и RRF поверх одного SQLite connection.

    Args:
        connection: Открытое SQLite-соединение приложения.
        embedding_config: Обязательный provider semantic query.
        chunking_config: Policy допустимого поколения chunks.

    Returns:
        Hybrid retrieval service с явным FTS5 fallback ожидаемых semantic ошибок.
    """
    chunker = create_vault_note_chunker(chunking_config)
    provider = create_embedding_provider(embedding_config)
    return VaultHybridSearchService(
        lexical_search=VaultFullTextSearchService(
            repository=SQLiteVaultFullTextSearchRepository(connection),
            chunker=chunker,
        ),
        vector_search=VaultVectorSearchService(
            chunk_repository=SQLiteVaultChunkIndexRepository(connection),
            embedding_repository=SQLiteVaultEmbeddingIndexRepository(connection),
            embedding_provider=provider,
            chunker=chunker,
        ),
    )


def create_process_article_url_use_case(
    connection: sqlite3.Connection,
) -> ProcessArticleUrlUseCase:
    """Собирает concrete dependencies для обработки URL статьи.

    Args:
        connection: Открытое соединение SQLite.

    Returns:
        Use case с SQLite, HTTP и trafilatura-реализациями ports.
    """
    return ProcessArticleUrlUseCase(
        article_repository=SQLiteArticleRepository(connection),
        html_fetcher=UrllibArticleHtmlFetcher(),
        text_extractor=TrafilaturaArticleTextExtractor(),
        error_recorder=SQLiteProcessingErrorRecorder(connection),
    )


def create_analyze_article_use_case(
    connection: sqlite3.Connection,
    *,
    openai_base_url: str,
    openai_api_key: str,
    openai_model: str,
) -> AnalyzeArticleUseCase:
    """Собирает concrete dependencies для LLM-анализа статьи.

    Args:
        connection: Открытое соединение SQLite.
        openai_base_url: Базовый URL OpenAI-compatible API.
        openai_api_key: API key провайдера LLM.
        openai_model: Имя модели для анализа статей.

    Returns:
        Use case с SQLite-хранилищем и OpenAI-compatible analyzer.
    """
    return AnalyzeArticleUseCase(
        article_repository=SQLiteArticleRepository(connection),
        analyzer=OpenAIArticleAnalyzer(
            base_url=openai_base_url,
            api_key=openai_api_key,
            model=openai_model,
        ),
        analysis_result_repository=SQLiteArticleAnalysisResultRepository(connection),
        error_recorder=SQLiteProcessingErrorRecorder(connection),
    )


def create_prepare_obsidian_review_use_case(
    connection: sqlite3.Connection,
    *,
    openai_base_url: str,
    openai_api_key: str,
    openai_model: str,
    embedding_config: EmbeddingConfig | None,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    created_note_tag: str | None = "knowledge-catcher",
) -> PrepareObsidianReviewUseCase:
    """Собирает retrieval, preflight и двухфазную LLM-генерацию 10.9.

    Args:
        connection: Открытое SQLite-соединение одного incoming worker.
        openai_base_url: Базовый URL Chat Completions API.
        openai_api_key: API key LLM-провайдера.
        openai_model: Модель планирования и написания Markdown.
        embedding_config: Semantic provider либо `None` для явного FTS fallback.
        chunking_config: Ожидаемая signature локального chunk index.
        created_note_tag: Служебный тег новых заметок или `None` для отключения.

    Returns:
        Полностью собранный use case подготовки Obsidian review.
    """
    lexical_search = create_vault_full_text_search_service(
        connection,
        chunking_config=chunking_config,
    )
    search = (
        create_vault_hybrid_search_service(
            connection,
            embedding_config=embedding_config,
            chunking_config=chunking_config,
        )
        if embedding_config is not None
        else VaultFtsFallbackSearchService(lexical_search=lexical_search)
    )
    return PrepareObsidianReviewUseCase(
        article_repository=SQLiteArticleRepository(connection),
        vault_repository=SQLiteObsidianVaultRepository(connection),
        instruction_repository=SQLiteVaultInstructionRepository(connection),
        note_repository=SQLiteVaultNoteRepository(connection),
        search=search,
        generator=OpenAIObsidianProposalGenerator(
            base_url=openai_base_url,
            api_key=openai_api_key,
            model=openai_model,
        ),
        proposal_repository=SQLiteObsidianProposalRepository(connection),
        error_recorder=SQLiteProcessingErrorRecorder(connection),
        created_note_tag=created_note_tag,
    )


def create_obsidian_proposal_confirmation_service(
    connection: sqlite3.Connection,
    *,
    github_gateway: GitHubVaultWriteGateway,
    vault_sync_manager: VaultSyncManager | None = None,
) -> ObsidianProposalConfirmationService:
    """Собирает подтверждение proposal, SHA preflight и GitHub write-back."""
    return ObsidianProposalConfirmationService(
        proposal_repository=SQLiteObsidianProposalRepository(connection),
        vault_repository=SQLiteObsidianVaultRepository(connection),
        lease_repository=SQLiteVaultSyncLeaseRepository(connection),
        github_gateway=github_gateway,
        vault_sync_manager=vault_sync_manager,
        error_recorder=SQLiteProcessingErrorRecorder(connection),
    )


def create_incoming_message_repository(
    connection: sqlite3.Connection,
) -> IncomingMessageRepository:
    """Создаёт repository входящих сообщений для adapters каналов."""
    return SQLiteIncomingMessageRepository(connection)


def create_github_app_client(config: GitHubAppConfig) -> HttpxGitHubAppClient:
    """Создаёт общий HTTP adapter зарегистрированного GitHub App.

    Args:
        config: Полная конфигурация GitHub App.

    Returns:
        Client для Device Flow, installation tokens и чтения repositories.
    """
    signer = PyJwtGitHubAppSigner(
        client_id=config.client_id,
        private_key_path=config.private_key_path,
    )
    return HttpxGitHubAppClient(
        client_id=config.client_id,
        app_jwt_factory=signer.create,
    )


def create_vault_selection_manager(
    *,
    database_path: Path,
    github_gateway: GitHubRepositoryGateway,
) -> VaultSelectionManager:
    """Собирает application-сценарий выбора vault для одного сообщения.

    Args:
        database_path: Путь к общей SQLite-базе adapters.
        github_gateway: Процессный GitHub App HTTP adapter.

    Returns:
        Сервис с общими SQLite repositories пользователя.
    """
    return SQLiteGitHubVaultSelectionManager(
        database_path=database_path,
        github_gateway=github_gateway,
    )


def create_vault_sync_manager(
    *,
    database_path: Path,
    github_gateway: GitHubVaultGateway,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    embedding_config: EmbeddingConfig | None = None,
) -> VaultSyncManager:
    """Собирает GitHub/SQLite-сценарий синхронизации vault.

    Args:
        database_path: Путь к общей SQLite-базе adapters.
        github_gateway: GitHub App gateway чтения Git trees и blobs.
        chunking_config: Policy разбиения Markdown-заметок на chunks.
        embedding_config: Настройки semantic index либо `None` для FTS-only.

    Returns:
        Менеджер с отдельным соединением для каждой операции.
    """
    return SQLiteGitHubVaultSyncManager(
        database_path=database_path,
        github_gateway=github_gateway,
        note_chunker=create_vault_note_chunker(chunking_config),
        embedding_provider=(
            create_embedding_provider(embedding_config)
            if embedding_config is not None
            else None
        ),
    )


def create_github_connection_coordinator(
    *,
    database_path: Path,
    config: GitHubAppConfig,
    gateway: HttpxGitHubAppClient | None = None,
) -> GitHubConnectionCoordinator:
    """Собирает процессный coordinator GitHub Device Flow.

    Args:
        database_path: Путь к общей SQLite-базе adapters.
        config: Полная конфигурация зарегистрированного GitHub App.
        gateway: Общий процессный GitHub client или `None` для создания нового.

    Returns:
        Coordinator, хранящий временные Device Flow sessions только в памяти.
    """
    runtime_gateway = gateway or create_github_app_client(config)
    return GitHubConnectionCoordinator(
        gateway=runtime_gateway,
        account_writer=SQLiteGitHubAccountAccessWriter(database_path),
        state_store=SQLiteGitHubConnectionStateStore(database_path),
        installation_url=config.installation_url,
    )


def create_obsidian_entry(
    connection: sqlite3.Connection,
    *,
    database_path: Path,
    openai_base_url: str,
    openai_api_key: str,
    openai_model: str,
    runtime: ObsidianProcessRuntime | None = None,
    use_case_factory: ProcessArticleUrlUseCaseFactory | None = None,
    analysis_use_case_factory: AnalyzeArticleUseCaseFactory | None = None,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    embedding_config: EmbeddingConfig | None = None,
    created_note_tag: str | None = "knowledge-catcher",
) -> ObsidianMessageHandler:
    """Собирает публичный вход Obsidian на одно SQLite-соединение сообщения.

    Args:
        connection: Соединение текущего worker thread.
        database_path: Путь к общей БД для фоновых операций.
        openai_base_url: Базовый URL OpenAI-compatible API.
        openai_api_key: API key провайдера LLM.
        openai_model: Модель анализа и proposal.
        runtime: Процессные GitHub-зависимости либо `None`.
        use_case_factory: Тестовая фабрика article pipeline.
        analysis_use_case_factory: Тестовая фабрика анализа.
        chunking_config: Настройки разбиения заметок.
        embedding_config: Настройки semantic index либо `None`.
        created_note_tag: Служебный тег новых заметок.

    Returns:
        Подключаемый вход Obsidian через общий protocol.
    """
    article_url_use_case = (
        use_case_factory or create_process_article_url_use_case
    )(connection)
    article_analysis_use_case = (
        analysis_use_case_factory(connection)
        if analysis_use_case_factory is not None
        else create_analyze_article_use_case(
            connection,
            openai_base_url=openai_base_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )
    )
    gateway = runtime.repository_gateway if runtime is not None else None
    starter = runtime.connection_starter if runtime is not None else None
    vault_sync_manager = (
        create_vault_sync_manager(
            database_path=database_path,
            github_gateway=gateway,
            chunking_config=chunking_config,
            embedding_config=embedding_config,
        )
        if gateway is not None and hasattr(gateway, "fetch_vault_snapshot")
        else None
    )
    return ObsidianMessageHandler(
        article_url_use_case=article_url_use_case,
        article_analysis_use_case=article_analysis_use_case,
        incoming_message_repository=create_incoming_message_repository(connection),
        github_connection_starter=starter,
        vault_selection_manager=(
            create_vault_selection_manager(
                database_path=database_path,
                github_gateway=gateway,
            )
            if gateway is not None
            else None
        ),
        vault_sync_manager=vault_sync_manager,
        obsidian_review_use_case=create_prepare_obsidian_review_use_case(
            connection,
            openai_base_url=openai_base_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            embedding_config=embedding_config,
            chunking_config=chunking_config,
            created_note_tag=created_note_tag,
        ),
        obsidian_confirmation_service=(
            create_obsidian_proposal_confirmation_service(
                connection,
                github_gateway=gateway,
                vault_sync_manager=vault_sync_manager,
            )
            if gateway is not None
            and hasattr(gateway, "inspect_vault_target")
            and hasattr(gateway, "commit_vault_markdown")
            else None
        ),
    )
