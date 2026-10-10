from __future__ import annotations

import sqlite3

from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from compadre_assistant.capabilities.obsidian.data.sqlite.article_flow_lease_repository import SQLiteArticleFlowLeaseRepository

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.domain.users.entities import AppUser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from document_chunker import (
    ChunkingPolicy,
    DocumentChunker,
    DocumentFormat,
    MarkdownDocumentParser,
)

from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleUseCase
from compadre_assistant.capabilities.obsidian.domain.articles.ports import IncomingMessageRepository
from compadre_assistant.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlUseCase
from compadre_assistant.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlCommand, ProcessArticleUrlError
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler, ObsidianMessageResult, obsidian_capability_help
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import CapabilityDescriptor, CapabilityResult, HandlerContext, IncomingCapabilityPort
from compadre_assistant.capabilities.obsidian.domain.reviews.proposal import PrepareObsidianReviewUseCase
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import (
    ObsidianProposalConfirmationService,
)
from compadre_assistant.capabilities.obsidian.domain.search.full_text import VaultFullTextSearchService
from compadre_assistant.capabilities.obsidian.domain.search.hybrid import (
    VaultFtsFallbackSearchService,
    VaultHybridSearchService,
)
from compadre_assistant.capabilities.obsidian.domain.search.ports import EmbeddingProvider, VaultNoteChunker
from compadre_assistant.capabilities.obsidian.domain.search.vector import VaultVectorSearchService
from compadre_assistant.capabilities.obsidian.domain.vaults.github_connection import (
    GitHubConnectionCoordinator,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.ports import (
    GitHubConnectionStarter,
    GitHubRepositoryGateway,
    GitHubVaultGateway,
    GitHubVaultWriteGateway,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_selection import VaultSelectionManager
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import VaultSyncManager
from compadre_assistant.capabilities.obsidian.data.chunking.document_note_chunker import DocumentVaultNoteChunker
from compadre_assistant.capabilities.obsidian.data.configuration.config import (
    ChunkingConfig,
    EmbeddingConfig,
    GitHubAppConfig,
    ObsidianConfig,
    ObsidianConfigError,
    load_obsidian_config,
)
from compadre_assistant.capabilities.obsidian.data.embeddings.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from compadre_assistant.capabilities.obsidian.data.extraction.trafilatura_article_extractor import (
    TrafilaturaArticleTextExtractor,
)
from compadre_assistant.capabilities.obsidian.data.http.article_html_fetcher import UrllibArticleHtmlFetcher
from compadre_assistant.capabilities.obsidian.data.http.url_safety import UnsafeUrlError, validate_public_http_url
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import GitHubConnectionCompletion, GitHubGatewayError
from compadre_assistant.capabilities.obsidian.data.llm.openai_article_analyzer import OpenAIArticleAnalyzer
from compadre_assistant.capabilities.obsidian.data.llm.openai_obsidian_proposal_generator import (
    OpenAIObsidianProposalGenerator,
)
from compadre_assistant.capabilities.obsidian.data.github.github_app_client import HttpxGitHubAppClient
from compadre_assistant.capabilities.obsidian.data.github.jwt_signer import PyJwtGitHubAppSigner
from compadre_assistant.capabilities.obsidian.data.sqlite.analysis_result_repository import (
    SQLiteArticleAnalysisResultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.article_repository import SQLiteArticleRepository
from compadre_assistant.capabilities.obsidian.data.sqlite.incoming_message_repository import (
    SQLiteIncomingMessageRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.github_installation_writer import (
    SQLiteGitHubAccountAccessWriter,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.github_connection_state_store import (
    SQLiteGitHubConnectionStateStore,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.github_vault_selection_manager import (
    SQLiteGitHubVaultSelectionManager,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.github_vault_sync_manager import (
    SQLiteGitHubVaultSyncManager,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.processing_error_repository import (
    SQLiteProcessingErrorRecorder,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_vault_repository import (
    SQLiteObsidianVaultRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import (
    SQLiteObsidianProposalRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_sync_lease_repository import (
    SQLiteVaultSyncLeaseRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_full_text_search_repository import (
    SQLiteVaultFullTextSearchRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_chunk_index_repository import (
    SQLiteVaultChunkIndexRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_embedding_index_repository import (
    SQLiteVaultEmbeddingIndexRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_instruction_repository import (
    SQLiteVaultInstructionRepository,
)
from compadre_assistant.capabilities.obsidian.data.sqlite.vault_note_repository import SQLiteVaultNoteRepository


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
        llm_client: Общий SDK-клиент анализа и подготовки proposal.
        embedding_client: Общий SDK-клиент semantic index.
        note_chunker: Неизменяемый parser и policy Markdown для всех операций.
    """

    connection_starter: GitHubConnectionStarter | None
    repository_gateway: GitHubRepositoryGateway | None
    llm_client: Any | None = None
    embedding_client: Any | None = None
    note_chunker: VaultNoteChunker | None = None

    def close(self) -> None:
        """Закрывает принадлежащие процессу HTTP и SDK-клиенты."""
        for client in (self.embedding_client, self.llm_client, self.repository_gateway):
            close = getattr(client, "close", None)
            if callable(close):
                close()


class ObsidianCapability(IncomingCapabilityPort):
    """Держит один зарегистрированный вход и открывает SQLite только для вызова."""

    def __init__(
        self,
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
    ) -> None:
        """Сохраняет настройки без соединения и repositories.

        Args:
            database_path: Путь к общей SQLite-БД для operation scopes.
            openai_base_url: Endpoint LLM.
            openai_api_key: Ключ LLM, если провайдер настроен.
            openai_model: Имя модели анализа.
            runtime: Процессные внешние клиенты и Device Flow.
            use_case_factory: Подменная фабрика article pipeline для тестов.
            analysis_use_case_factory: Подменная фабрика анализа для тестов.
            chunking_config: Параметры разбиения заметок.
            embedding_config: Необязательный профиль semantic index.
            created_note_tag: Служебный тег новых заметок.
        """
        self._database_path = database_path
        self._options = dict(
            database_path=database_path,
            openai_base_url=openai_base_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            runtime=runtime,
            use_case_factory=use_case_factory,
            analysis_use_case_factory=analysis_use_case_factory,
            chunking_config=chunking_config,
            embedding_config=embedding_config,
            created_note_tag=created_note_tag,
        )
        self._descriptor = CapabilityDescriptor(
            "obsidian", "/obsidian", "работа со статьями и Obsidian vault",
            obsidian_capability_help(),
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        """Возвращает статическую справку без обращения к SQLite."""
        return self._descriptor

    def enter(
        self, context: HandlerContext, event: IncomingEvent, arguments: str
    ) -> CapabilityResult:
        """Входит в Obsidian с отдельным SQLite operation scope."""
        with connect_database(self._database_path) as connection:
            return create_obsidian_entry(connection, **self._options).enter(context, event, arguments)

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Передаёт событие Obsidian с отдельным SQLite operation scope."""
        with connect_database(self._database_path) as connection:
            return create_obsidian_entry(connection, **self._options).handle(context, event)


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


def create_obsidian_process_runtime(
    *,
    database_path: Path,
    github_app_config: GitHubAppConfig | None,
    github_connection_starter: GitHubConnectionStarter | None = None,
    openai_base_url: str = "",
    openai_api_key: str = "",
    openai_model: str = "",
    embedding_config: EmbeddingConfig | None = None,
    chunking_config: ChunkingConfig = ChunkingConfig(),
) -> ObsidianProcessRuntime:
    """Создаёт процессные GitHub и настроенные LLM/embedding клиенты.

    Args:
        database_path: Путь к общей SQLite-БД.
        github_app_config: Настройки GitHub App либо `None`.
        github_connection_starter: Готовый coordinator для тестов.
        openai_base_url: Endpoint LLM при включённой конфигурации.
        openai_api_key: Ключ LLM при включённой конфигурации.
        openai_model: Имя модели LLM.
        embedding_config: Отдельная конфигурация embedding API.
        chunking_config: Неизменяемая policy процессного Markdown chunker.

    Returns:
        Процессные зависимости, переиспользуемые worker threads.
    """
    gateway = create_github_app_client(github_app_config) if github_app_config else None
    starter = github_connection_starter
    llm_client = embedding_client = None
    try:
        note_chunker = create_vault_note_chunker(chunking_config)
        if starter is None and github_app_config is not None:
            starter = create_github_connection_coordinator(
                database_path=database_path,
                config=github_app_config,
                gateway=gateway,
            )
        if openai_api_key.strip() and openai_model.strip():
            from openai import OpenAI

            llm_client = OpenAI(base_url=openai_base_url, api_key=openai_api_key)
        if embedding_config is not None:
            from openai import OpenAI

            embedding_client = OpenAI(
                base_url=embedding_config.base_url,
                api_key=embedding_config.api_key,
                timeout=30,
                max_retries=2,
            )
    except Exception:
        ObsidianProcessRuntime(starter, gateway, llm_client, embedding_client).close()
        raise
    return ObsidianProcessRuntime(
        starter, gateway, llm_client, embedding_client, note_chunker
    )


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
    note_chunker: VaultNoteChunker | None = None,
) -> VaultFullTextSearchService:
    """Собирает поиск FTS5 с проверкой текущей chunking signature.

    Args:
        connection: Открытое SQLite-соединение приложения.
        chunking_config: Policy, определяющая допустимое поколение chunks.
        note_chunker: Процессный Markdown chunker или `None` для отдельной сборки.

    Returns:
        Application-сервис изолированного полнотекстового поиска по vault.
    """
    return VaultFullTextSearchService(
        repository=SQLiteVaultFullTextSearchRepository(connection),
        chunker=note_chunker or create_vault_note_chunker(chunking_config),
    )


def create_embedding_provider(config: EmbeddingConfig, *, client: Any | None = None) -> EmbeddingProvider:
    """Собирает OpenAI-compatible embedding adapter.

    Args:
        config: Отдельная конфигурация semantic embedding provider.
        client: Процессный SDK-клиент или `None` для автономного adapter.

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
        client=client,
    )


def create_vault_hybrid_search_service(
    connection: sqlite3.Connection,
    *,
    embedding_config: EmbeddingConfig,
    chunking_config: ChunkingConfig = ChunkingConfig(),
    embedding_client: Any | None = None,
    note_chunker: VaultNoteChunker | None = None,
) -> VaultHybridSearchService:
    """Собирает BM25, cosine search и RRF поверх одного SQLite connection.

    Args:
        connection: Открытое SQLite-соединение приложения.
        embedding_config: Обязательный provider semantic query.
        chunking_config: Policy допустимого поколения chunks.
        embedding_client: Процессный SDK-клиент или `None`.
        note_chunker: Процессный Markdown chunker или `None`.

    Returns:
        Hybrid retrieval service с явным FTS5 fallback ожидаемых semantic ошибок.
    """
    chunker = note_chunker or create_vault_note_chunker(chunking_config)
    provider = create_embedding_provider(embedding_config, client=embedding_client)
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
    llm_client: Any | None = None,
) -> AnalyzeArticleUseCase:
    """Собирает concrete dependencies для LLM-анализа статьи.

    Args:
        connection: Открытое соединение SQLite.
        openai_base_url: Базовый URL OpenAI-compatible API.
        openai_api_key: API key провайдера LLM.
        openai_model: Имя модели для анализа статей.
        llm_client: Процессный SDK-клиент или `None`.

    Returns:
        Use case с SQLite-хранилищем и OpenAI-compatible analyzer.
    """
    return AnalyzeArticleUseCase(
        article_repository=SQLiteArticleRepository(connection),
        analyzer=OpenAIArticleAnalyzer(
            base_url=openai_base_url,
            api_key=openai_api_key,
            model=openai_model,
            client=llm_client,
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
    llm_client: Any | None = None,
    embedding_client: Any | None = None,
    note_chunker: VaultNoteChunker | None = None,
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
        llm_client: Процессный SDK-клиент или `None`.
        embedding_client: Процессный SDK-клиент или `None`.
        note_chunker: Процессный Markdown chunker или `None`.

    Returns:
        Полностью собранный use case подготовки Obsidian review.
    """
    chunker = note_chunker or create_vault_note_chunker(chunking_config)
    lexical_search = create_vault_full_text_search_service(
        connection,
        chunking_config=chunking_config,
        note_chunker=chunker,
    )
    search = (
        create_vault_hybrid_search_service(
            connection,
            embedding_config=embedding_config,
            chunking_config=chunking_config,
            embedding_client=embedding_client,
            note_chunker=chunker,
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
            client=llm_client,
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
    embedding_client: Any | None = None,
    note_chunker: VaultNoteChunker | None = None,
) -> VaultSyncManager:
    """Собирает GitHub/SQLite-сценарий синхронизации vault.

    Args:
        database_path: Путь к общей SQLite-базе adapters.
        github_gateway: GitHub App gateway чтения Git trees и blobs.
        chunking_config: Policy разбиения Markdown-заметок на chunks.
        embedding_config: Настройки semantic index либо `None` для FTS-only.
        embedding_client: Процессный SDK-клиент или `None`.
        note_chunker: Процессный Markdown chunker или `None`.

    Returns:
        Менеджер с отдельным соединением для каждой операции.
    """
    return SQLiteGitHubVaultSyncManager(
        database_path=database_path,
        github_gateway=github_gateway,
        note_chunker=note_chunker or create_vault_note_chunker(chunking_config),
        embedding_provider=(
            create_embedding_provider(embedding_config, client=embedding_client)
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
    """Собирает предметный обработчик на одно SQLite-соединение операции.

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
    article_url_use_case = (use_case_factory or create_process_article_url_use_case)(connection)
    llm_configured = bool(
        openai_api_key.strip()
        and openai_model.strip()
        and validate_external_http_url(openai_base_url) is None
    )
    article_analysis_use_case = (
        analysis_use_case_factory(connection)
        if analysis_use_case_factory is not None
        else (
            create_analyze_article_use_case(
                connection,
                openai_base_url=openai_base_url,
                openai_api_key=openai_api_key,
                openai_model=openai_model,
                llm_client=runtime.llm_client if runtime is not None else None,
            )
            if llm_configured
            else None
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
            embedding_client=runtime.embedding_client if runtime is not None else None,
            note_chunker=runtime.note_chunker if runtime is not None else None,
        )
        if gateway is not None and hasattr(gateway, "fetch_vault_snapshot")
        else None
    )

    def complete_github(
        repository_arguments: str,
        app_user: AppUser,
        completion: GitHubConnectionCompletion,
        origin: PendingOrigin,
    ) -> ObsidianMessageResult:
        """Собирает продолжение фоновой операции на новом коротком соединении."""
        with connect_database(database_path) as completion_connection:
            handler = create_obsidian_entry(
                completion_connection,
                database_path=database_path,
                openai_base_url=openai_base_url,
                openai_api_key=openai_api_key,
                openai_model=openai_model,
                runtime=runtime,
                use_case_factory=use_case_factory,
                analysis_use_case_factory=analysis_use_case_factory,
                chunking_config=chunking_config,
                embedding_config=embedding_config,
                created_note_tag=created_note_tag,
            )
            return handler._complete_github_connection(
                repository_arguments, app_user, completion, origin
            )

    return ObsidianMessageHandler(
        article_flow_lease_repository=SQLiteArticleFlowLeaseRepository(connection),
        github_completion_processor=complete_github,
        llm_configured=llm_configured or analysis_use_case_factory is not None,
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
        obsidian_review_use_case=(
            create_prepare_obsidian_review_use_case(
                connection,
                openai_base_url=openai_base_url,
                openai_api_key=openai_api_key,
                openai_model=openai_model,
                embedding_config=embedding_config,
                chunking_config=chunking_config,
                created_note_tag=created_note_tag,
                llm_client=runtime.llm_client if runtime is not None else None,
                embedding_client=runtime.embedding_client if runtime is not None else None,
                note_chunker=runtime.note_chunker if runtime is not None else None,
            )
            if llm_configured
            else None
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
