from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import logging

from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import (
    CapabilityDescriptor, CapabilityHelp, CapabilityResult, HandlerContext,
    HelpSection, IncomingCapabilityPort, IncomingResponse,
)
from obs_chat_bot.capabilities.obsidian.presentation.commands import ObsidianCommand, ParsedObsidianCommand
from obs_chat_bot.capabilities.obsidian.presentation.commands import help_sections as obsidian_help_sections
from obs_chat_bot.capabilities.obsidian.presentation.results import ObsidianResultType
from obs_chat_bot.capabilities.obsidian.domain.articles.analyzing import (
    AnalyzeArticleCommand,
    AnalyzeArticleError,
    AnalyzeArticleResult,
    AnalyzeArticleUseCase,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.incoming_messages import (
    ArticleIncomingMessage,
    SavedIncomingMessage,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.ports import IncomingMessageRepository
from obs_chat_bot.capabilities.obsidian.domain.articles.processing import (
    ProcessArticleUrlCommand,
    ProcessArticleUrlError,
    ProcessArticleUrlResult,
    ProcessArticleUrlUseCase,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.url_extraction import extract_first_supported_url
from obs_chat_bot.capabilities.obsidian.domain.reviews.proposal import (
    PrepareObsidianReviewCommand,
    PrepareObsidianReviewError,
    PrepareObsidianReviewResult,
    PrepareObsidianReviewUseCase,
)
from obs_chat_bot.capabilities.obsidian.domain.reviews.confirmation import (
    ObsidianConfirmationResult,
    ObsidianConfirmationStatus,
    ObsidianProposalConfirmationService,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import (
    GitHubConnectionCompletion,
    GitHubConnectionCompletionStatus,
    GitHubConnectionStartResult,
    GitHubConnectionStartStatus,
    GitHubGatewayError,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import GitHubConnectionStarter
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_selection import (
    GitHubAccountNotConnectedError,
    GitHubRepositoryNotAccessibleError,
    GitHubVaultPathNotFoundError,
    VaultSelectionManager,
    VaultSelectionResult,
    VaultSelectionStatus,
    VaultDisconnectResult,
    VaultDisconnectStatus,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_configuration import VaultConfigurationError
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_sync import (
    VaultStatus,
    VaultSyncManager,
    VaultSyncResult,
    VaultSyncStatus,
    VaultSyncWarning,
    VaultSyncWarningReason,
)
from obs_chat_bot.core.domain.users.entities import AppUser
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ObsidianMessageResult:
    """Хранит предметные сведения ответа для представления Obsidian."""

    type: ObsidianResultType
    app_user: AppUser | None = None
    error: Exception | None = None
    saved_message: SavedIncomingMessage | None = None
    article_result: ProcessArticleUrlResult | None = None
    analysis_result: AnalyzeArticleResult | None = None
    review_result: PrepareObsidianReviewResult | None = None
    obsidian_confirmation: ObsidianConfirmationResult | None = None
    github_connection: GitHubConnectionStartResult | None = None
    github_completion: GitHubConnectionCompletion | None = None
    vault_selection: VaultSelectionResult | None = None
    installation_url: str | None = None
    vault_sync_result: VaultSyncResult | None = None
    vault_status: VaultStatus | None = None
    vault_disconnect: VaultDisconnectResult | None = None
    vault_sync_warning: VaultSyncWarning | None = None


ObsidianCompletionHandler = Callable[[ObsidianMessageResult], None]
GitHubCompletionProcessor = Callable[[str, AppUser, GitHubConnectionCompletion], ObsidianMessageResult]


class ObsidianMessageHandler(IncomingCapabilityPort):
    """Обрабатывает текст, команды и подтверждения возможности Obsidian."""

    def __init__(
        self,
        *,
        article_url_use_case: ProcessArticleUrlUseCase,
        article_analysis_use_case: AnalyzeArticleUseCase | None = None,
        incoming_message_repository: IncomingMessageRepository | None = None,
        github_connection_starter: GitHubConnectionStarter | None = None,
        vault_selection_manager: VaultSelectionManager | None = None,
        vault_sync_manager: VaultSyncManager | None = None,
        obsidian_review_use_case: PrepareObsidianReviewUseCase | None = None,
        obsidian_confirmation_service: ObsidianProposalConfirmationService | None = None,
        github_completion_processor: GitHubCompletionProcessor | None = None,
        llm_configured: bool = True,
    ) -> None:
        """Внедряет предметные use cases и продолжение фоновой авторизации.

        Args:
            article_url_use_case: Обработка и сохранение статьи.
            article_analysis_use_case: Необязательный анализ сохранённой статьи.
            incoming_message_repository: Журнал сообщений только этой возможности.
            github_connection_starter: Процессный Device Flow coordinator.
            vault_selection_manager: Выбор и подтверждаемая замена vault.
            vault_sync_manager: Обновление локального снимка vault.
            obsidian_review_use_case: Подготовка предложения изменения заметки.
            obsidian_confirmation_service: Подтверждение сохранённого proposal.
            llm_configured: Наличие обязательной конфигурации LLM в текущей сборке.
            github_completion_processor: Продолжение с собственным соединением;
                при отсутствии используется текущее предметное окружение.
        """
        self._llm_configured = llm_configured
        self._github_completion_processor = github_completion_processor
        self._article_url_use_case = article_url_use_case
        self._article_analysis_use_case = article_analysis_use_case
        self._incoming_message_repository = incoming_message_repository
        self._github_connection_starter = github_connection_starter
        self._vault_selection_manager = vault_selection_manager
        self._vault_sync_manager = vault_sync_manager
        self._obsidian_review_use_case = obsidian_review_use_case
        self._obsidian_confirmation_service = obsidian_confirmation_service

    @property
    def descriptor(self) -> CapabilityDescriptor:
        """Объявляет единственный вход Obsidian без динамического поиска."""
        return CapabilityDescriptor(
            "obsidian", "/obsidian", "работа со статьями и Obsidian vault", self._help()
        )

    def enter(
        self, context: HandlerContext, event: IncomingEvent, arguments: str
    ) -> CapabilityResult:
        """Показывает актуальный vault и ожидающие операции при входе в режим.

        Args:
            context: Проверенный внутренний пользователь и фоновая доставка.
            event: Исходное событие с командой входа и необязательным аргументом.
            arguments: Текст или URL после команды входа; событие сохраняется целиком.

        Returns:
            Общий ответ длительного режима с результатом аргумента или справкой.
        """
        if arguments:
            return self._handle(context, event, arguments)
        user = context.app_user
        pending = (
            self._obsidian_confirmation_service.get_pending(user.id)
            if self._obsidian_confirmation_service
            else None
        )
        if pending is not None:
            result = ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REVIEW_CONFIRMATION_PENDING,
                app_user=user,
                obsidian_confirmation=ObsidianConfirmationResult(
                    status=ObsidianConfirmationStatus.PENDING, proposal=pending
                ),
            )
            response = _present_result(result)
            return CapabilityResult(
                replace(response, text=f"Режим Obsidian. Выход: `/exit`.\n{response.text}")
            )
        vault = self._selected_vault(user.id)
        lines = [
            "Режим Obsidian. Выход: `/exit`.",
            "Пришли ссылку на статью для анализа и предложения изменения заметки.",
            "Ссылка на GitHub-репозиторий подключает или заменяет vault; после ссылки можно указать папку.",
            "Команды: `/github_status`, `/github_sync`, `/github_disconnect`, `/reanalyze <id>`.",
        ]
        if self._vault_selection_manager is None:
            lines.append("Провайдер GitHub не настроен на сервере.")
        elif vault is None:
            lines.append("Твой Obsidian vault не подключён. Пришли ссылку на GitHub-репозиторий с vault.")
        else:
            root = f", папка: `{vault.root_path}`" if vault.root_path else ""
            lines.append(f"Подключён vault: `{vault.owner}/{vault.repository}`{root}.")
        if not self._llm_configured:
            lines.append("Провайдер LLM не настроен на сервере; анализ статей пока недоступен.")
        if self._vault_selection_manager is not None:
            if self._vault_selection_manager.has_replacement_confirmation(user.id):
                lines.append("Ожидается подтверждение замены vault: ответь `да` или `нет`.")
            if self._vault_selection_manager.has_disconnect_confirmation(user.id):
                lines.append("Ожидается подтверждение отключения vault: ответь `да` или `нет`.")
        if self._github_connection_starter is not None:
            connection = self._github_connection_starter.get_pending(user.id)
            if connection is not None:
                result = ObsidianMessageResult(
                    type=(
                        ObsidianResultType.GITHUB_CONNECT_ALREADY_PENDING
                        if connection.authorization
                        else ObsidianResultType.GITHUB_CONNECT_IN_PROGRESS
                    ),
                    app_user=user,
                    github_connection=connection,
                )
                lines.append(_present_result(result).text)
        return CapabilityResult(IncomingResponse("\n".join(lines)))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Выполняет use cases Obsidian и переводит их результат в общий ответ.

        Args:
            context: Проверенный пользователь и доступная фоновая доставка.
            event: Нормализованное сообщение выбранного режима.

        Returns:
            Готовое содержимое ответа с продолжением режима Obsidian.
        """
        return self._handle(context, event)

    def _handle(
        self, context: HandlerContext, event: IncomingEvent, arguments: str | None = None
    ) -> CapabilityResult:
        """Обрабатывает текст входа с неизменными метаданными и адресом события."""
        delivery = context.delivery
        address = event.reply_address

        def complete(result: ObsidianMessageResult) -> None:
            """Оформляет предметный результат до передачи независимой доставке."""
            if delivery is not None:
                delivery.send(address, _present_result(result))

        text = event.text if arguments is None else arguments
        if delivery is not None and should_acknowledge(text):
            from obs_chat_bot.core.presentation.responses import PROCESSING_ACK_TEXT

            delivery.send(address, IncomingResponse(PROCESSING_ACK_TEXT))
        result = self._process(
            context.app_user, event, complete if delivery else None, text=arguments
        )
        if arguments is not None and result.error is None and result.type in {
            ObsidianResultType.ARTICLE_URL_MISSING,
            ObsidianResultType.REANALYZE_COMMAND_INVALID,
            ObsidianResultType.GITHUB_VAULT_COMMAND_INVALID,
        }:
            # Ошибочный аргумент входа не должен заменять ранее выбранный режим чата.
            result = replace(result, error=ValueError("Некорректный аргумент входа Obsidian"))
        return CapabilityResult(_present_result(result))

    def _help(self) -> CapabilityHelp:
        """Описывает команды и подсказки Obsidian для общей справки."""
        return CapabilityHelp(
            sections=tuple(
                HelpSection(title, commands)
                for title, commands in obsidian_help_sections()
            ),
            instructions=(
                "Войди через `/obsidian [текст или URL]`. Подключение vault выполняется внутри этого режима.",
                "После настройки пришли HTTP/HTTPS-ссылку — сохранить и проанализировать статью.",
                "",
                "Ответы `да` и `нет` используются, когда бот просит подтвердить перепривязку или замену.",
            ),
        )

    def _selected_vault(self, app_user_id: int) -> ObsidianVault | None:
        """Читает vault только при предметном обращении к Obsidian."""
        if self._vault_selection_manager is None:
            return None
        return self._vault_selection_manager.get_selected(app_user_id)

    def _process(
        self,
        app_user: AppUser,
        event: IncomingEvent,
        completion_handler: ObsidianCompletionHandler | None = None,
        *,
        text: str | None = None,
    ) -> ObsidianMessageResult:
        """Выполняет Obsidian-сценарий после общей идентификации.

        Args:
            app_user: Уже найденный пользователь приложения.
            event: Нейтральное событие канала.
            completion_handler: Доставка фонового GitHub-ответа в исходный чат.
            text: Аргумент входа либо `None` для исходного текста события.

        Returns:
            Предметный результат для представления Obsidian.
        """
        text = (event.text if text is None else text).strip()
        parsed = ParsedObsidianCommand.parse(text)
        command = parsed.command if parsed is not None else None
        normalized = text.lower()
        if normalized in {"да", "yes", "y"}:
            if (
                self._vault_selection_manager is not None
                and self._vault_selection_manager.has_replacement_confirmation(app_user.id)
            ):
                return self._confirm_vault_replacement(app_user)
            if (
                self._vault_selection_manager is not None
                and self._vault_selection_manager.has_disconnect_confirmation(app_user.id)
            ):
                return self._confirm_vault_disconnect(app_user)
            if self._obsidian_confirmation_service is not None:
                confirmation = self._obsidian_confirmation_service.confirm(app_user.id)
                if confirmation.status is not ObsidianConfirmationStatus.NO_PENDING:
                    return self._review_confirmation_result(app_user, confirmation)
            return ObsidianMessageResult(type=ObsidianResultType.CONFIRMATION_MISSING)
        if normalized in {"нет", "no", "n"}:
            if self._vault_selection_manager is not None:
                cancelled = self._vault_selection_manager.cancel_replacement(app_user.id)
                if cancelled is not None:
                    return ObsidianMessageResult(
                        type=ObsidianResultType.GITHUB_VAULT_REPLACEMENT_CANCELLED,
                        app_user=app_user,
                        vault_selection=cancelled,
                    )
                disconnected = self._vault_selection_manager.cancel_disconnect(app_user.id)
                if disconnected is not None:
                    return ObsidianMessageResult(
                        type=ObsidianResultType.GITHUB_DISCONNECT_CANCELLED,
                        app_user=app_user,
                        vault_disconnect=disconnected,
                    )
            if self._obsidian_confirmation_service is not None:
                confirmation = self._obsidian_confirmation_service.cancel(app_user.id)
                if confirmation.status is not ObsidianConfirmationStatus.NO_PENDING:
                    return self._review_confirmation_result(app_user, confirmation)
            return ObsidianMessageResult(type=ObsidianResultType.CONFIRMATION_MISSING)

        pending = (
            self._obsidian_confirmation_service.get_pending(app_user.id)
            if self._obsidian_confirmation_service is not None
            else None
        )
        if pending is not None and command is not ObsidianCommand.GITHUB_STATUS:
            return ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REVIEW_CONFIRMATION_PENDING,
                app_user=app_user,
                obsidian_confirmation=ObsidianConfirmationResult(
                    status=ObsidianConfirmationStatus.PENDING, proposal=pending
                ),
            )
        if command is ObsidianCommand.GITHUB_STATUS:
            if self._vault_sync_manager is None:
                return ObsidianMessageResult(
                    type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                    app_user=app_user,
                )
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_STATUS,
                app_user=app_user,
                vault_status=self._vault_sync_manager.get_status(app_user.id),
            )
        if command is ObsidianCommand.GITHUB_SYNC:
            return self._sync_vault(app_user)
        if command is ObsidianCommand.GITHUB_DISCONNECT:
            return self._request_vault_disconnect(app_user)
        repository_arguments = _github_repository_arguments(text)
        if repository_arguments is not None:
            return self._connect_vault(
                repository_arguments,
                app_user,
                completion_handler=completion_handler,
            )
        incoming_message = ArticleIncomingMessage(
            channel=event.channel,
            chat_id=event.chat_id,
            message_id=event.message_id,
            text=event.text,
            app_user_id=app_user.id,
            external_user_id=event.external_user_id,
            username=event.username,
            display_name=event.display_name,
        )
        if self._vault_selection_manager is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE, app_user=app_user
            )
        if self._selected_vault(app_user.id) is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.VAULT_REQUIRED, app_user=app_user
            )
        if not self._llm_configured:
            return ObsidianMessageResult(
                type=ObsidianResultType.LLM_NOT_CONFIGURED, app_user=app_user
            )
        if command is ObsidianCommand.REANALYZE:
            return self._reanalyze_article(
                parsed.arguments if parsed is not None else "",
                incoming_message,
                app_user,
            )
        return self._process_article_url(incoming_message, app_user)

    def _process_article_url(
        self, incoming_message: ArticleIncomingMessage, app_user_result: AppUser
    ) -> ObsidianMessageResult:
        """Сохраняет только article-сообщение и запускает его pipeline."""
        url = extract_first_supported_url(incoming_message.text)
        if url is None:
            result = ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_URL_MISSING,
                app_user=app_user_result,
            )
            _log_result(result)
            return result

        auto_sync_result = self._auto_sync_vault(app_user_result)
        if isinstance(auto_sync_result, ObsidianMessageResult):
            _log_result(auto_sync_result)
            return auto_sync_result
        vault_sync_warning = (
            auto_sync_result
            if isinstance(auto_sync_result, VaultSyncWarning)
            else (
                self._vault_sync_warning(
                    app_user_result.id,
                    VaultSyncWarningReason.EMBEDDING_UPDATE_FAILED,
                )
                if (
                    isinstance(auto_sync_result, VaultSyncResult)
                    and auto_sync_result.embedding_update_failed
                )
                else None
            )
        )
        vault_sync_result = (
            auto_sync_result
            if isinstance(auto_sync_result, VaultSyncResult)
            else None
        )

        saved_message = self._save_incoming_message(incoming_message)
        incoming_message_id = saved_message.id if saved_message is not None else None
        if saved_message is not None:
            LOGGER.debug(
                "Incoming message saved: incoming_message_id=%s app_user_id=%s "
                "channel=%s chat_id=%s message_id=%s",
                saved_message.id,
                saved_message.app_user_id,
                saved_message.channel,
                saved_message.chat_id,
                saved_message.message_id,
            )

        try:
            article_result = self._article_url_use_case.execute(
                ProcessArticleUrlCommand(
                    source_url=url,
                    app_user_id=incoming_message.app_user_id,
                    incoming_message_id=incoming_message_id,
                )
            )
        except ProcessArticleUrlError as error:
            result = ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_PROCESSING_FAILED,
                app_user=app_user_result,
                saved_message=saved_message,
                vault_sync_result=vault_sync_result,
                vault_sync_warning=vault_sync_warning,
                error=error,
            )
            _log_result(result)
            return result

        self._link_message_to_article(saved_message, article_result)
        LOGGER.debug(
            "Article flow completed: article_id=%s app_user_id=%s created=%s "
            "extracted=%s status=%s",
            article_result.article.id,
            article_result.article.app_user_id,
            article_result.created,
            article_result.extracted,
            article_result.article.status.value,
        )

        if (
            article_result.article.status is ArticleStatus.REVIEWED
            and self._obsidian_confirmation_service is not None
            and article_result.article.id is not None
        ):
            applied = self._obsidian_confirmation_service.get_latest_applied(
                app_user_id=app_user_result.id,
                article_id=article_result.article.id,
            )
            if applied is not None:
                saved_analysis = (
                    self._article_analysis_use_case.execute(
                        AnalyzeArticleCommand(
                            article_id=article_result.article.id,
                            app_user_id=app_user_result.id,
                            incoming_message_id=incoming_message_id,
                        )
                    )
                    if self._article_analysis_use_case is not None
                    else None
                )
                result = ObsidianMessageResult(
                    type=ObsidianResultType.ARTICLE_REVIEWED_RESULT,
                    app_user=app_user_result,
                    saved_message=saved_message,
                    article_result=article_result,
                    analysis_result=saved_analysis,
                    obsidian_confirmation=ObsidianConfirmationResult(
                        status=ObsidianConfirmationStatus.APPLIED,
                        proposal=applied,
                    ),
                    vault_sync_result=vault_sync_result,
                    vault_sync_warning=vault_sync_warning,
                )
                _log_result(result)
                return result

        if self._article_analysis_use_case is None or article_result.article.id is None:
            result = ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_PROCESSED,
                app_user=app_user_result,
                saved_message=saved_message,
                article_result=article_result,
                vault_sync_result=vault_sync_result,
                vault_sync_warning=vault_sync_warning,
            )
            _log_result(result)
            return result

        try:
            analysis_result = self._article_analysis_use_case.execute(
                AnalyzeArticleCommand(
                    article_id=article_result.article.id,
                    app_user_id=incoming_message.app_user_id,
                    incoming_message_id=incoming_message_id,
                )
            )
        except AnalyzeArticleError as error:
            result = ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_ANALYSIS_FAILED,
                app_user=app_user_result,
                saved_message=saved_message,
                article_result=article_result,
                vault_sync_result=vault_sync_result,
                vault_sync_warning=vault_sync_warning,
                error=error,
            )
            _log_result(result)
            return result

        LOGGER.debug(
            "Article analysis completed: article_id=%s analysis_id=%s created=%s "
            "model=%s",
            analysis_result.article.id,
            analysis_result.analysis.id,
            analysis_result.created,
            analysis_result.analysis.llm_model,
        )
        if self._obsidian_review_use_case is not None:
            try:
                review_result = self._obsidian_review_use_case.execute(
                    PrepareObsidianReviewCommand(
                        article_id=article_result.article.id,
                        analysis=analysis_result.analysis,
                        app_user_id=incoming_message.app_user_id,
                        incoming_message_id=incoming_message_id,
                    )
                )
            except PrepareObsidianReviewError as error:
                result = ObsidianMessageResult(
                    type=ObsidianResultType.ARTICLE_REVIEW_FAILED,
                    app_user=app_user_result,
                    saved_message=saved_message,
                    article_result=article_result,
                    analysis_result=analysis_result,
                    vault_sync_result=vault_sync_result,
                    vault_sync_warning=vault_sync_warning,
                    error=error,
                )
                _log_result(result)
                return result
            result = ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REVIEW_PREPARED,
                app_user=app_user_result,
                saved_message=saved_message,
                article_result=article_result,
                analysis_result=analysis_result,
                review_result=review_result,
                vault_sync_result=vault_sync_result,
                vault_sync_warning=vault_sync_warning,
            )
            _log_result(result)
            return result

        result = ObsidianMessageResult(
            type=ObsidianResultType.ARTICLE_ANALYZED,
            app_user=app_user_result,
            saved_message=saved_message,
            article_result=article_result,
            analysis_result=analysis_result,
            vault_sync_result=vault_sync_result,
            vault_sync_warning=vault_sync_warning,
        )
        _log_result(result)
        return result

    def _auto_sync_vault(
        self,
        app_user: AppUser,
    ) -> VaultSyncResult | VaultSyncWarning | ObsidianMessageResult | None:
        """Проверяет устаревший vault перед обработкой URL статьи."""
        if self._vault_sync_manager is None:
            return None
        try:
            sync_result = self._vault_sync_manager.sync_if_stale(app_user.id)
        except VaultConfigurationError as error:
            return ObsidianMessageResult(
                type=(
                    ObsidianResultType
                    .GITHUB_VAULT_CONFIGURATION_REQUIRED
                ),
                app_user=app_user,
                error=error,
            )
        except (GitHubGatewayError, OSError, ValueError, RuntimeError) as error:
            LOGGER.warning(
                "Automatic vault check failed: "
                "app_user_id=%s error_type=%s",
                app_user.id,
                type(error).__name__,
            )
            status = self._get_vault_status_safe(app_user.id)
            if not _has_usable_local_snapshot(status):
                return ObsidianMessageResult(
                    type=ObsidianResultType.GITHUB_SYNC_FAILED,
                    app_user=app_user,
                    vault_status=status,
                    error=error,
                )
            return self._vault_sync_warning(
                app_user.id,
                VaultSyncWarningReason.UPDATE_FAILED,
                status=status,
            )
        LOGGER.debug(
            "Automatic vault check completed: app_user_id=%s status=%s "
            "downloaded_notes=%s total_notes=%s",
            app_user.id,
            sync_result.status.value,
            sync_result.downloaded_notes,
            sync_result.total_notes,
        )
        if sync_result.status is VaultSyncStatus.NO_VAULT:
            return ObsidianMessageResult(
                type=ObsidianResultType.VAULT_REQUIRED,
                app_user=app_user,
            )
        if sync_result.status is VaultSyncStatus.IN_PROGRESS:
            status = self._get_vault_status_safe(app_user.id)
            if not _has_usable_local_snapshot(status):
                return ObsidianMessageResult(
                    type=ObsidianResultType.GITHUB_SYNC_FAILED,
                    app_user=app_user,
                    vault_status=status,
                    error=RuntimeError(
                        "Vault synchronization is in progress without a usable snapshot"
                    ),
                )
            return self._vault_sync_warning(
                app_user.id,
                VaultSyncWarningReason.IN_PROGRESS,
                status=status,
            )
        return sync_result

    def _vault_sync_warning(
        self,
        app_user_id: int,
        reason: VaultSyncWarningReason,
        *,
        status: VaultStatus | None = None,
    ) -> VaultSyncWarning:
        """Дополняет fallback доступным состоянием локальной копии."""
        status = status or self._get_vault_status_safe(app_user_id)
        if status is None:
            return VaultSyncWarning(reason=reason)
        return VaultSyncWarning(
            reason=reason,
            note_count=status.note_count,
            last_checked_at=(
                status.vault.last_checked_at
                if status.vault is not None
                else None
            ),
        )

    def _get_vault_status_safe(self, app_user_id: int) -> VaultStatus | None:
        """Читает readiness локального vault без подмены исходной sync-ошибки."""
        if self._vault_sync_manager is None:
            return None
        try:
            return self._vault_sync_manager.get_status(app_user_id)
        except (OSError, ValueError, RuntimeError):
            return None

    def _review_confirmation_result(
        self,
        app_user: AppUser,
        confirmation: ObsidianConfirmationResult,
    ) -> ObsidianMessageResult:
        """Преобразует итог review-сервиса в channel-agnostic result type."""
        result_types = {
            ObsidianConfirmationStatus.PENDING: (
                ObsidianResultType.ARTICLE_REVIEW_CONFIRMATION_PENDING
            ),
            ObsidianConfirmationStatus.CANCELLED: (
                ObsidianResultType.ARTICLE_REVIEW_CANCELLED
            ),
            ObsidianConfirmationStatus.APPLIED: (
                ObsidianResultType.ARTICLE_REVIEW_APPLIED
            ),
            ObsidianConfirmationStatus.CONFLICT: (
                ObsidianResultType.ARTICLE_REVIEW_CONFLICT
            ),
            ObsidianConfirmationStatus.IN_PROGRESS: (
                ObsidianResultType.ARTICLE_REVIEW_IN_PROGRESS
            ),
            ObsidianConfirmationStatus.FAILED: (
                ObsidianResultType.ARTICLE_REVIEW_APPLY_FAILED
            ),
            ObsidianConfirmationStatus.NO_PENDING: (
                ObsidianResultType.CONFIRMATION_MISSING
            ),
        }
        return ObsidianMessageResult(
            type=result_types[confirmation.status],
            app_user=app_user,
            obsidian_confirmation=confirmation,
            error=confirmation.error,
        )

    def _connect_vault(
        self,
        arguments: str,
        app_user: AppUser,
        *,
        completion_handler: ObsidianCompletionHandler | None,
    ) -> ObsidianMessageResult:
        """Подключает vault либо запускает необходимую GitHub-авторизацию."""
        result = self._select_github_vault(arguments, app_user)
        if result.type is ObsidianResultType.GITHUB_VAULT_GITHUB_REQUIRED:
            return self._start_github_connection(
                arguments,
                app_user,
                completion_handler=completion_handler,
            )
        if (
            result.type
            is ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE
            and self._github_connection_starter is not None
        ):
            return _with_installation_url(
                result,
                self._github_connection_starter.installation_url,
            )
        return result

    def _start_github_connection(
        self,
        repository_arguments: str,
        app_user: AppUser,
        *,
        completion_handler: ObsidianCompletionHandler | None,
    ) -> ObsidianMessageResult:
        """Запускает Device Flow и продолжает выбор исходного vault после него."""
        if self._github_connection_starter is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                app_user=app_user,
            )

        processor = self._github_completion_processor or self._complete_github_connection

        def complete_github(completion: GitHubConnectionCompletion) -> None:
            """Продолжает фоновой сценарий через процессор с собственным соединением."""
            if (
                completion.status is GitHubConnectionCompletionStatus.CONNECTED
                and completion_handler is not None
            ):
                completion_handler(
                    ObsidianMessageResult(
                        type=(
                            ObsidianResultType
                            .GITHUB_AUTHORIZED_SYNC_STARTED
                        ),
                        app_user=app_user,
                        github_completion=completion,
                    )
                )
            result = processor(
                repository_arguments,
                app_user,
                completion,
            )
            if completion_handler is not None:
                completion_handler(result)

        try:
            connection = self._github_connection_starter.start(
                app_user.id,
                complete_github,
            )
        except (GitHubGatewayError, OSError, ValueError) as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_FAILED,
                app_user=app_user,
                error=error,
            )
        result_types = {
            GitHubConnectionStartStatus.STARTED: (
                ObsidianResultType.GITHUB_CONNECT_STARTED
            ),
            GitHubConnectionStartStatus.ALREADY_PENDING: (
                ObsidianResultType.GITHUB_CONNECT_ALREADY_PENDING
            ),
            GitHubConnectionStartStatus.PREPARING: (
                ObsidianResultType.GITHUB_CONNECT_PREPARING
            ),
            GitHubConnectionStartStatus.IN_PROGRESS: (
                ObsidianResultType.GITHUB_CONNECT_IN_PROGRESS
            ),
        }
        return ObsidianMessageResult(
            type=result_types[connection.status],
            app_user=app_user,
            github_connection=connection,
        )

    def _complete_github_connection(
        self,
        repository_arguments: str,
        app_user: AppUser,
        completion: GitHubConnectionCompletion,
    ) -> ObsidianMessageResult:
        """Продолжает подключение vault после завершения фонового Device Flow."""
        if completion.status is GitHubConnectionCompletionStatus.CONNECTED:
            result = self._select_github_vault(repository_arguments, app_user)
            if (
                result.type
                is ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE
                and self._github_connection_starter is not None
            ):
                return _with_installation_url(
                    result,
                    self._github_connection_starter.installation_url,
                )
            return result
        if completion.status is GitHubConnectionCompletionStatus.NO_INSTALLATIONS:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_APP_REQUIRED,
                app_user=app_user,
                github_completion=completion,
                installation_url=(
                    self._github_connection_starter.installation_url
                    if self._github_connection_starter is not None
                    else None
                ),
            )
        return ObsidianMessageResult(
            type=ObsidianResultType.GITHUB_CONNECT_FAILED,
            app_user=app_user,
            github_completion=completion,
        )

    def _select_github_vault(
        self,
        arguments: str,
        app_user: AppUser,
    ) -> ObsidianMessageResult:
        """Проверяет GitHub URL внутри режима и запускает выбор vault."""
        parts = arguments.split(maxsplit=1)
        if not parts:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_COMMAND_INVALID,
                app_user=app_user,
            )
        if self._vault_selection_manager is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                app_user=app_user,
            )
        try:
            selection = self._vault_selection_manager.select(
                app_user_id=app_user.id,
                repository_url=parts[0],
                root_path=parts[1] if len(parts) == 2 else "",
            )
        except GitHubAccountNotConnectedError as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_GITHUB_REQUIRED,
                app_user=app_user,
                error=error,
            )
        except GitHubRepositoryNotAccessibleError as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE,
                app_user=app_user,
                error=error,
            )
        except GitHubVaultPathNotFoundError as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_PATH_NOT_FOUND,
                app_user=app_user,
                error=error,
            )
        except ValueError as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_COMMAND_INVALID,
                app_user=app_user,
                error=error,
            )
        except (GitHubGatewayError, OSError) as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_FAILED,
                app_user=app_user,
                error=error,
            )
        result_types = {
            VaultSelectionStatus.SELECTED: (
                ObsidianResultType.GITHUB_VAULT_SELECTED
            ),
            VaultSelectionStatus.ALREADY_SELECTED: (
                ObsidianResultType.GITHUB_VAULT_ALREADY_SELECTED
            ),
            VaultSelectionStatus.REPLACEMENT_CONFIRMATION_REQUIRED: (
                ObsidianResultType
                .GITHUB_VAULT_REPLACEMENT_CONFIRMATION_REQUIRED
            ),
        }
        result = ObsidianMessageResult(
            type=result_types[selection.status],
            app_user=app_user,
            vault_selection=selection,
        )
        if selection.status is VaultSelectionStatus.SELECTED:
            return self._sync_vault(app_user, base_result=result)
        return result

    def _confirm_vault_replacement(
        self,
        app_user: AppUser,
    ) -> ObsidianMessageResult:
        """Применяет ожидающую замену vault после ответа `да`."""
        if self._vault_selection_manager is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                app_user=app_user,
            )
        try:
            selection = self._vault_selection_manager.confirm_replacement(app_user.id)
        except (OSError, ValueError) as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_FAILED,
                app_user=app_user,
                error=error,
            )
        if selection is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_CONFIRMATION_MISSING,
                app_user=app_user,
            )
        result = ObsidianMessageResult(
            type=ObsidianResultType.GITHUB_VAULT_REPLACED,
            app_user=app_user,
            vault_selection=selection,
        )
        return self._sync_vault(app_user, base_result=result)

    def _sync_vault(
        self,
        app_user: AppUser,
        *,
        base_result: ObsidianMessageResult | None = None,
    ) -> ObsidianMessageResult:
        """Запускает ручную или первую синхронизацию выбранного vault."""
        if self._vault_sync_manager is None:
            return base_result or ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                app_user=app_user,
            )
        try:
            sync_result = self._vault_sync_manager.sync(app_user.id)
        except (GitHubGatewayError, OSError, ValueError, RuntimeError) as error:
            if base_result is not None:
                return replace(base_result, error=error)
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_SYNC_FAILED,
                app_user=app_user,
                error=error,
            )
        if base_result is not None:
            return replace(base_result, vault_sync_result=sync_result)
        return ObsidianMessageResult(
            type=ObsidianResultType.GITHUB_SYNC_COMPLETED,
            app_user=app_user,
            vault_sync_result=sync_result,
        )

    def _request_vault_disconnect(
        self,
        app_user: AppUser,
    ) -> ObsidianMessageResult:
        """Запрашивает явное подтверждение отключения активного vault."""
        if self._vault_selection_manager is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                app_user=app_user,
            )
        try:
            disconnect = self._vault_selection_manager.request_disconnect(app_user.id)
        except (OSError, ValueError) as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_FAILED,
                app_user=app_user,
                error=error,
            )
        result_type = (
            ObsidianResultType.GITHUB_DISCONNECT_NOT_CONNECTED
            if disconnect.status is VaultDisconnectStatus.NOT_CONNECTED
            else ObsidianResultType.GITHUB_DISCONNECT_CONFIRMATION_REQUIRED
        )
        return ObsidianMessageResult(
            type=result_type,
            app_user=app_user,
            vault_disconnect=disconnect,
        )

    def _confirm_vault_disconnect(
        self,
        app_user: AppUser,
    ) -> ObsidianMessageResult:
        """Удаляет vault и локальную копию после ответа пользователя `да`."""
        if self._vault_selection_manager is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                app_user=app_user,
            )
        try:
            disconnect = self._vault_selection_manager.confirm_disconnect(app_user.id)
        except (OSError, ValueError) as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_FAILED,
                app_user=app_user,
                error=error,
            )
        if disconnect is None:
            return ObsidianMessageResult(
                type=(
                    ObsidianResultType
                    .GITHUB_DISCONNECT_CONFIRMATION_MISSING
                ),
                app_user=app_user,
            )
        result_type = (
            ObsidianResultType.GITHUB_DISCONNECT_NOT_CONNECTED
            if disconnect.status is VaultDisconnectStatus.NOT_CONNECTED
            else ObsidianResultType.GITHUB_DISCONNECTED
        )
        return ObsidianMessageResult(
            type=result_type,
            app_user=app_user,
            vault_disconnect=disconnect,
        )

    def _reanalyze_article(
        self,
        arguments: str,
        incoming_message: ArticleIncomingMessage,
        app_user: AppUser,
    ) -> ObsidianMessageResult:
        """Повторно анализирует сохранённую статью по команде пользователя."""
        if not arguments.isdigit():
            return ObsidianMessageResult(
                type=ObsidianResultType.REANALYZE_COMMAND_INVALID,
                app_user=app_user,
            )
        if self._article_analysis_use_case is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REANALYSIS_FAILED,
                app_user=app_user,
                error=AnalyzeArticleError("Article analysis use case is not configured"),
            )

        try:
            article_result = self._article_url_use_case.reprocess_existing(
                article_id=int(arguments),
                app_user_id=app_user.id,
                incoming_message_id=None,
            )
            analysis_result = self._article_analysis_use_case.execute(
                AnalyzeArticleCommand(
                    article_id=int(arguments),
                    app_user_id=app_user.id,
                    incoming_message_id=None,
                    force=True,
                )
            )
            review_result = None
            if self._obsidian_review_use_case is not None:
                review_result = self._obsidian_review_use_case.execute(
                    PrepareObsidianReviewCommand(
                        article_id=int(arguments),
                        analysis=analysis_result.analysis,
                        app_user_id=app_user.id,
                        incoming_message_id=None,
                    )
                )
        except (
            ProcessArticleUrlError,
            AnalyzeArticleError,
            PrepareObsidianReviewError,
        ) as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REANALYSIS_FAILED,
                app_user=app_user,
                error=error,
            )

        return ObsidianMessageResult(
            type=ObsidianResultType.ARTICLE_REANALYZED,
            app_user=app_user,
            article_result=article_result,
            analysis_result=analysis_result,
            review_result=review_result,
        )

    def _save_incoming_message(
        self,
        incoming_message: ArticleIncomingMessage,
    ) -> SavedIncomingMessage | None:
        """Сохраняет сообщение, если repository передан в use case."""
        if self._incoming_message_repository is None:
            return None
        return self._incoming_message_repository.save(incoming_message)

    def _link_message_to_article(
        self,
        saved_message: SavedIncomingMessage | None,
        article_result: ProcessArticleUrlResult,
    ) -> None:
        """Связывает сохраненное сообщение со статьей после успешной обработки."""
        if (
            self._incoming_message_repository is None
            or saved_message is None
            or article_result.article.id is None
        ):
            return
        self._incoming_message_repository.link_to_article(
            incoming_message_id=saved_message.id,
            article_id=article_result.article.id,
        )


def _github_repository_arguments(text: str) -> str | None:
    """Возвращает GitHub repository URL с optional path из обычного сообщения."""
    stripped = text.strip()
    if not stripped:
        return None
    first_part = stripped.split(maxsplit=1)[0].lower()
    if first_part.startswith(("https://github.com/", "http://github.com/")):
        return stripped
    return None


def _with_installation_url(
    result: ObsidianMessageResult,
    installation_url: str,
) -> ObsidianMessageResult:
    """Добавляет безопасную ссылку настройки App к готовому результату."""
    return replace(result, installation_url=installation_url)


def _has_usable_local_snapshot(status: VaultStatus | None) -> bool:
    """Проверяет readiness локального source/FTS до платного article pipeline."""
    if status is None or status.vault is None:
        return False
    return bool(
        status.vault.head_commit_sha
        and status.vault.tree_sha
        and status.chunk_index_current
    )


def _text_kind(text: str) -> str:
    stripped_text = text.strip()
    if not stripped_text:
        return "empty"
    parsed_command = ParsedObsidianCommand.parse(stripped_text)
    if parsed_command is not None:
        return f"command:{parsed_command.command.value}"
    if stripped_text.startswith("/"):
        return "command:unknown"
    if extract_first_supported_url(stripped_text) is not None:
        return "url"
    return "text"


def should_acknowledge(text: str) -> bool:
    """Сообщает каналу, что URL или повторный анализ займёт время.

    Args:
        text: Необработанный текст сообщения.

    Returns:
        `True` для прежнего предиката ранней квитанции.
    """
    stripped_text = text.strip()
    return stripped_text.startswith("/reanalyze") or extract_first_supported_url(stripped_text) is not None


def _log_result(result: ObsidianMessageResult) -> None:
    LOGGER.debug(
        "Incoming message processed: result=%s app_user_id=%s "
        "incoming_message_id=%s article_id=%s analysis_id=%s "
        "proposal_action=%s vault_warning=%s error_type=%s",
        result.type.value,
        result.app_user.id if result.app_user is not None else None,
        result.saved_message.id if result.saved_message is not None else None,
        (
            result.article_result.article.id
            if result.article_result is not None
            else None
        ),
        (
            result.analysis_result.analysis.id
            if result.analysis_result is not None
            else None
        ),
        (
            result.review_result.proposal.action.value
            if result.review_result is not None
            else None
        ),
        (
            result.vault_sync_warning.reason.value
            if result.vault_sync_warning is not None
            else None
        ),
        type(result.error).__name__ if result.error is not None else None,
    )


def _present_result(result: ObsidianMessageResult) -> IncomingResponse:
    """Оставляет преобразование предметных моделей в presentation Obsidian."""
    from obs_chat_bot.capabilities.obsidian.presentation.responses import format_obsidian_message_result

    return IncomingResponse(format_obsidian_message_result(result), result.error)
