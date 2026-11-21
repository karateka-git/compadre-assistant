from __future__ import annotations


from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import logging
from uuid import uuid4

from compadre_assistant.core.presentation.message_content import code_span, compose_content, text_content, join_content
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin, PendingOperationError, VaultResourceBusyError
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import (
    VaultActionConfirmation, VaultConfirmationAction, VaultConfirmationStatus,
)
from compadre_assistant.capabilities.obsidian.domain.articles.ports import ArticleFlowLeaseRepository
from compadre_assistant.capabilities.obsidian.domain.articles.url_utils import normalize_article_url

from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, ScreenDestination
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, ConfirmationAction, CapabilityDescriptor, CapabilityResult, HandlerContext,
    IncomingCapabilityPort, IncomingResponse, PendingInputContinuationPort,
    PendingInputResult, PendingInputOutcome, PendingInput,
    PendingConfirmation, ConfirmationQueryResult, PendingConfirmationQueryPort, ReplyAction, SectionActionsQueryPort,
    ContextualCommandQueryPort,
)
from compadre_assistant.capabilities.obsidian.presentation.commands import ObsidianCommand, ParsedObsidianCommand
from compadre_assistant.capabilities.obsidian.presentation.commands import obsidian_descriptor
from compadre_assistant.capabilities.obsidian.presentation.results import ObsidianResultType
from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import (
    AnalyzeArticleCommand,
    AnalyzeArticleError,
    AnalyzeArticleResult,
    AnalyzeArticleUseCase,
)
from compadre_assistant.capabilities.obsidian.domain.articles.incoming_messages import (
    ArticleIncomingMessage,
    SavedIncomingMessage,
)
from compadre_assistant.capabilities.obsidian.domain.articles.ports import IncomingMessageRepository
from compadre_assistant.capabilities.obsidian.domain.articles.processing import (
    ProcessArticleUrlCommand,
    ProcessArticleUrlError,
    ProcessArticleUrlResult,
    ProcessArticleUrlUseCase,
)
from compadre_assistant.capabilities.obsidian.domain.articles.url_extraction import extract_first_supported_url
from compadre_assistant.capabilities.obsidian.domain.reviews.proposal import (
    PrepareObsidianReviewCommand,
    PrepareObsidianReviewError,
    PrepareObsidianReviewResult,
    PrepareObsidianReviewUseCase,
)
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import (
    ObsidianConfirmationResult,
    ObsidianConfirmationStatus,
    ObsidianProposalConfirmationService,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import (
    GitHubConnectionCompletion,
    GitHubConnectionCompletionStatus,
    GitHubConnectionStartResult,
    GitHubConnectionStartStatus,
    GitHubGatewayError,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.ports import GitHubConnectionStarter
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_selection import (
    GitHubAccountNotConnectedError,
    GitHubRepositoryNotAccessibleError,
    GitHubVaultPathNotFoundError,
    VaultSelectionManager,
    VaultSelectionResult,
    VaultSelectionStatus,
    VaultDisconnectResult,
    VaultDisconnectStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_configuration import VaultConfigurationError
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import (
    VaultStatus,
    VaultSyncManager,
    VaultSyncResult,
    VaultSyncStatus,
    VaultSyncWarning,
    VaultSyncWarningReason,
)
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal, ObsidianProposalAction,
    ObsidianProposalStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import ObsidianVault


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ObsidianMessageResult:
    """Хранит предметные сведения ответа для представления Obsidian."""

    type: ObsidianResultType
    text_override: str | None = None
    pending_operation: VaultActionConfirmation | None = None
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
GitHubCompletionProcessor = Callable[[str, AppUser, GitHubConnectionCompletion, PendingOrigin], ObsidianMessageResult]

_APPROVAL_REPLIES = frozenset({"да", "yes", "y"})
_REJECTION_REPLIES = frozenset({"нет", "no", "n"})


_STATUS_ACTION = ReplyAction("Проверить состояние", ObsidianCommand.GITHUB_STATUS.value, scope=ActionScope.SECTION)
_SYNC_ACTION = ReplyAction("Синхронизировать", ObsidianCommand.GITHUB_SYNC.value, scope=ActionScope.SECTION)
_DISCONNECT_ACTION = ReplyAction("Отключить хранилище", ObsidianCommand.GITHUB_DISCONNECT.value, scope=ActionScope.SECTION)
_REANALYZE_ACTION = ReplyAction("Повторить анализ", ObsidianCommand.REANALYZE.value, scope=ActionScope.SECTION)
_CONFIRM_ACTION = ReplyAction("Подтверждения", "/confirm", scope=ActionScope.SECTION)
_EXIT_ACTION = ReplyAction("Выйти из режима", "/exit", scope=ActionScope.SECTION,
                           purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("main"))

_WAITING_ACTIONS = (CANCEL_SECTION_ACTION, HELP_SECTION_ACTION)


@dataclass(frozen=True, slots=True)
class _PendingDecisions:
    """Хранит доступную часть снимка решений Obsidian в одном чате."""

    proposals: tuple[ObsidianProposal, ...]
    operation: VaultActionConfirmation | None
    complete: bool

    def as_query_result(self, origin: ReplyAddress) -> ConfirmationQueryResult:
        """Возвращает публичные адресные действия для проверок ядра.

        Args:
            origin: Адрес, для которого прочитан снимок.

        Returns:
            Точные команды найденных операций и признак полноты чтения.
        """
        confirmations = [
            PendingConfirmation("obsidian", "proposal", proposal.id, proposal.version,
                                origin, _proposal_actions(proposal))
            for proposal in self.proposals
        ]
        if self.operation is not None:
            pending = self.operation
            confirmations.append(PendingConfirmation(
                "obsidian", pending.action.value, pending.id, pending.version, origin, _vault_operation_actions(pending),
            ))
        return ConfirmationQueryResult("complete" if self.complete else "unavailable", tuple(confirmations))


def _is_confirmation_reply(text: str) -> bool:
    """Распознаёт короткий ответ на ожидание Obsidian."""
    normalized = text.lower()
    return normalized in _APPROVAL_REPLIES or normalized in _REJECTION_REPLIES


class ObsidianMessageHandler(
    IncomingCapabilityPort, PendingInputContinuationPort, PendingConfirmationQueryPort, SectionActionsQueryPort,
    ContextualCommandQueryPort,
):
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
        article_flow_lease_repository: ArticleFlowLeaseRepository | None = None,
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
            article_flow_lease_repository: Координация полного цикла одной ссылки
                между worker; другие URL обрабатываются независимо.
            github_completion_processor: Продолжение с собственным соединением;
                при отсутствии используется текущее предметное окружение.
        """
        self._article_flow_lease_repository = article_flow_lease_repository
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
        return obsidian_descriptor()

    def enter(
        self, context: HandlerContext, event: IncomingEvent, arguments: str
    ) -> CapabilityResult:
        """Показывает актуальный vault и ожидающие операции при входе в режим.

        Args:
            context: Проверенный внутренний пользователь и фоновая доставка.
            event: Исходное событие с командой входа и необязательным аргументом.
            arguments: Текст или URL после команды входа; событие сохраняется целиком.
                Команды и подтверждения передаются отдельными сообщениями.

        Returns:
            Общий ответ длительного режима с результатом аргумента или справкой.
            Недопустимый аргумент возвращает ошибку без смены режима.
        """
        arguments = arguments.strip()
        if arguments.startswith("/") or _is_confirmation_reply(arguments):
            # Аргумент входа проходит обработку до сохранения режима.
            return CapabilityResult(IncomingResponse(
                compose_content(
                    "Сначала открой раздел Obsidian, затем выбери действие в нём."
                ),
                ValueError("Команда или подтверждение переданы как аргумент входа Obsidian"),
            ))
        if arguments:
            return self._handle(context, event, arguments)
        user = context.app_user
        address = event.reply_address
        origin = PendingOrigin(address.channel, address.chat_id)
        pending = self._read_pending_decisions(user.id, origin)
        vault = self._selected_vault(user.id)
        section_actions = self._section_actions(vault, pending)
        lines = ["Режим Obsidian включён."]
        if self._vault_selection_manager is None:
            lines.append("Подключение GitHub сейчас недоступно: провайдер GitHub не настроен на сервере.")
        elif vault is None:
            lines.append(compose_content(
                "Хранилище заметок (vault) не подключено. Пришли ссылку на GitHub repository, чтобы подключить его.",
                "\nМожно указать папку после ссылки, например ",
                code_span("https://github.com/owner/repository vault/path"), ".",
            ))
        else:
            root = f", папка {vault.root_path}" if vault.root_path else ""
            lines.append(f"Подключено хранилище {vault.owner}/{vault.repository}{root}.")
            if self._llm_configured:
                lines.append("Пришли HTTP/HTTPS-ссылку на статью для анализа.")
            if _STATUS_ACTION in section_actions:
                lines.append("Состояние локальной копии доступно по кнопке «Проверить состояние».")
            lines.append("Чтобы заменить хранилище, пришли ссылку на другой GitHub repository.")
        if not self._llm_configured:
            lines.append("Анализ статей сейчас недоступен: провайдер LLM не настроен на сервере.")
        actions = ()
        for proposal in pending.proposals:
            response = _present_result(ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REVIEW_CONFIRMATION_PENDING,
                app_user=user,
                obsidian_confirmation=ObsidianConfirmationResult(ObsidianConfirmationStatus.PENDING, proposal=proposal),
            ))
            lines.append(response.content)
            actions += response.actions
        if pending.operation is not None:
            operation = pending.operation
            label = "замены" if operation.action is VaultConfirmationAction.REPLACE else "отключения"
            lines.append(f"Ожидает подтверждения {label} хранилища. Ожидание действует 10 минут с момента создания.")
            actions += _vault_operation_actions(operation)
        if not pending.complete:
            lines.append("Не удалось прочитать все запросы. Открой «Подтверждения», чтобы попробовать снова.")
        links = ()
        if self._github_connection_starter is not None:
            connection = self._github_connection_starter.get_pending(user.id, origin)
            if connection is not None:
                response = _present_result(ObsidianMessageResult(
                    type=(ObsidianResultType.GITHUB_CONNECT_ALREADY_PENDING
                          if connection.authorization else ObsidianResultType.GITHUB_CONNECT_IN_PROGRESS),
                    app_user=user,
                    github_connection=connection,
                ))
                lines.append(response.content)
                links = response.suggested_actions
        response = IncomingResponse(
            join_content("\n", lines), actions=actions, suggested_actions=section_actions + links,
        )
        return CapabilityResult(_with_short_answer_hint(response, pending.as_query_result(address)))

    def visible_mode_commands(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
    ) -> frozenset[str]:
        """Отбирает справку по подключённому хранилищу и доступным службам.

        Args:
            app_user: Допущенный профиль, которому принадлежит хранилище.
            address: Чат с активным Obsidian.
            mode_revision: Проверенная ядром ревизия режима.

        Returns:
            Имена доступных команд без выполнения предметных операций.
        """
        actions = self._section_actions(self._selected_vault(app_user.id), _PendingDecisions((), None, True))
        return frozenset(action.command for action in actions)

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Читает доступные действия хранилища и ввода в текущем чате.

        Args:
            app_user: Допущенный профиль.
            address: Чат с активным Obsidian.
            mode_revision: Проверенная ядром ревизия режима.
            pending: Действующее ожидание ввода пользователя.

        Returns:
            Действия для текущего хранилища, доступных подтверждений и отмены ввода.
        """
        if pending is not None and pending.owner_id == "obsidian":
            return _WAITING_ACTIONS
        origin = PendingOrigin(address.channel, address.chat_id)
        decisions = self._read_pending_decisions(app_user.id, origin)
        return self._section_actions(self._selected_vault(app_user.id), decisions)

    def _section_actions(
        self, vault: ObsidianVault | None, pending: _PendingDecisions,
    ) -> tuple[ReplyAction, ...]:
        """Собирает один набор действий для экрана режима и меню раздела."""
        actions = ()
        if vault is not None:
            if self._vault_sync_manager is not None:
                actions += (_STATUS_ACTION, _SYNC_ACTION)
            if self._article_analysis_use_case is not None and self._llm_configured:
                actions += (_REANALYZE_ACTION,)
            actions += (_DISCONNECT_ACTION,)
        if not pending.complete or pending.proposals or pending.operation is not None:
            actions += (_CONFIRM_ACTION,)
        return actions + (HELP_SECTION_ACTION, _EXIT_ACTION)

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Выполняет use cases Obsidian и переводит их результат в общий ответ.

        Args:
            context: Проверенный пользователь и доступная фоновая доставка.
            event: Нормализованное сообщение выбранного режима.

        Returns:
            Готовое содержимое ответа с продолжением режима Obsidian.
        """
        parsed = ParsedObsidianCommand.parse(event.text.strip())
        if (parsed is not None and parsed.command is ObsidianCommand.REANALYZE
                and not parsed.arguments and context.pending_inputs is not None
                and self._llm_configured and self._article_analysis_use_case is not None
                and self._selected_vault(context.app_user.id) is not None):
            with context.pending_inputs.lock(event.reply_address):
                current = context.pending_inputs.get(event.reply_address)
                if (current is not None and current.external_user_id not in (None, event.external_user_id)
                        and not context.pending_inputs.expired(current)):
                    return CapabilityResult(IncomingResponse(text_content(
                        "В этом чате ожидается ввод от другого участника. Дождись завершения его шага."
                    ), suggested_actions=(HELP_SECTION_ACTION,)))
                context.pending_inputs.begin(event.reply_address, "obsidian", "reanalyze_article",
                                             app_user_id=context.app_user.id, required_mode="obsidian",
                                             value_label="ID статьи Obsidian", retry_command="/reanalyze",
                                             external_user_id=event.external_user_id, cancel_only_menu=True)
            return CapabilityResult(IncomingResponse(text_content(
                "Укажи числовой ID статьи для повторного анализа. "
                "Ожидание действует 10 минут."
            ), suggested_actions=_WAITING_ACTIONS))
        return self._handle(context, event)

    def continue_input(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent
    ) -> PendingInputResult:
        """Проверяет ID статьи, сохраняя исходное событие для повторного анализа.

        Args:
            context: Повторно допущенный профиль и сервисы ответа.
            pending: Исходное ожидание с типом значения.
            event: Неизменённое сообщение пользователя.

        Returns:
            Retry при неверном значении либо complete с действием вне блокировки.
        """
        if pending.input_kind != "reanalyze_article":
            return PendingInputResult(IncomingResponse(text_content(
                "Ожидание ID недоступно. Начни повторный анализ заново."
            ), suggested_actions=(_REANALYZE_ACTION, HELP_SECTION_ACTION)), PendingInputOutcome.COMPLETE)
        value = event.text.strip()
        if not _valid_article_id(value):
            return PendingInputResult(IncomingResponse(text_content(
                "Не удалось распознать ID статьи. Введи числовой ID или отмени ввод "
                "кнопкой отмены. Ожидание ID продолжается."
            ), suggested_actions=_WAITING_ACTIONS), PendingInputOutcome.RETRY)
        article = self._article_url_use_case.get_saved_for_user(int(value), context.app_user.id)
        if article is None:
            return PendingInputResult(IncomingResponse(text_content(
                "Статья недоступна в этом профиле. Введи другой ID или отмени ввод."
            ), suggested_actions=_WAITING_ACTIONS), PendingInputOutcome.RETRY)
        if self._obsidian_confirmation_service is not None:
            active = self._obsidian_confirmation_service.get_active_for_article(
                app_user_id=context.app_user.id, article_id=article.id or 0
            )
            if active is not None:
                return PendingInputResult(IncomingResponse(text_content(
                    "Для этой статьи уже ожидается действие. Заверши его в исходном чате "
                    "перед повторным анализом. Введи другой ID или отмени ввод."
                ), suggested_actions=_WAITING_ACTIONS), PendingInputOutcome.RETRY)

        def analyze() -> IncomingResponse:
            """Отправляет квитанцию после принятия ID и повторяет предметные проверки."""
            if context.delivery is not None:
                from compadre_assistant.core.presentation.responses import PROCESSING_ACK_TEXT
                context.delivery.send(event.reply_address, IncomingResponse(text_content(PROCESSING_ACK_TEXT)))
            return self._result_response(self._process_article_command(context.app_user, event, value), event.reply_address)

        return PendingInputResult(IncomingResponse(text_content("")), PendingInputOutcome.COMPLETE, analyze)

    def _handle(
        self, context: HandlerContext, event: IncomingEvent, arguments: str | None = None
    ) -> CapabilityResult:
        """Обрабатывает текст входа с неизменными метаданными и адресом события."""
        delivery = context.delivery
        address = event.reply_address

        def complete(result: ObsidianMessageResult) -> None:
            """Оформляет предметный результат до передачи независимой доставке."""
            if delivery is not None:
                delivery.send(address, self._result_response(result, address))

        text = event.text if arguments is None else arguments
        if delivery is not None and should_acknowledge(text):
            from compadre_assistant.core.presentation.responses import PROCESSING_ACK_TEXT

            delivery.send(address, IncomingResponse(text_content(PROCESSING_ACK_TEXT)))
        result = self._process(
            context.app_user, event, complete if delivery else None, text=arguments
        )
        if (
            arguments is not None
            and result.error is None
            and result.type
            in {
                ObsidianResultType.ARTICLE_URL_MISSING,
                ObsidianResultType.REANALYZE_COMMAND_INVALID,
                ObsidianResultType.GITHUB_VAULT_COMMAND_INVALID,
            }
        ):
            # Ошибочный аргумент входа не должен заменять ранее выбранный режим чата.
            result = replace(result, error=ValueError("Некорректный аргумент входа Obsidian"))
        return CapabilityResult(self._result_response(result, address))

    def _result_response(self, result: ObsidianMessageResult, address: ReplyAddress) -> IncomingResponse:
        """Дополняет ответ доступными действиями после выполнения операции."""
        response = _present_result(result)
        if result.app_user is None:
            return response
        user_id = result.app_user.id
        pending = self._read_pending_decisions(user_id, PendingOrigin(address.channel, address.chat_id))
        try:
            vault = self._selected_vault(user_id)
            response = replace(response, suggested_actions=response.suggested_actions + self._section_actions(vault, pending))
        except Exception:
            # Сбой чтения действий после операции не должен скрывать её итог.
            LOGGER.exception("Could not read actions after Obsidian operation")
            response = replace(response, suggested_actions=response.suggested_actions + (_CONFIRM_ACTION, HELP_SECTION_ACTION))
        return _with_short_answer_hint(response, pending.as_query_result(address))

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
            text: Аргумент входа либо None для исходного текста события.

        Returns:
            Предметный результат для представления Obsidian.
        """
        text = (event.text if text is None else text).strip()
        parsed = ParsedObsidianCommand.parse(text)
        command = parsed.command if parsed is not None else None
        origin = PendingOrigin(event.channel, event.chat_id)
        decision = self._decision(text, app_user, origin)
        if decision is not None:
            return decision
        if text.startswith("/") and parsed is None:
            return _operation_message(
                "Не знаю такого действия Obsidian. Открой «Помощь», чтобы посмотреть доступные действия.", app_user
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
            return self._request_vault_disconnect(app_user, origin)
        repository_arguments = _github_repository_arguments(text)
        if repository_arguments is not None:
            return self._connect_vault(
                repository_arguments,
                app_user,
                origin,
                completion_handler=completion_handler,
            )
        return self._process_article_command(
            app_user, event, parsed.arguments if command is ObsidianCommand.REANALYZE else None
        )

    def _process_article_command(
        self, app_user: AppUser, event: IncomingEvent, reanalyze_id: str | None = None
    ) -> ObsidianMessageResult:
        """Проверяет окружение и запускает обработку статьи из прямого или пошагового входа."""
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
        if not self._llm_configured:
            return ObsidianMessageResult(
                type=ObsidianResultType.LLM_NOT_CONFIGURED, app_user=app_user
            )
        if self._selected_vault(app_user.id) is None:
            return ObsidianMessageResult(type=ObsidianResultType.VAULT_REQUIRED, app_user=app_user)
        if reanalyze_id is not None:
            return self._reanalyze_article(
                reanalyze_id,
                incoming_message,
                app_user,
            )
        return self._process_article_url(incoming_message, app_user)

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Читает независимые источники Obsidian, сохраняя известную часть при сбое.

        Args:
            context: Пользователь, допущенный ядром в активный режим.
            origin: Исходный канал и чат ожидаемых решений.

        Returns:
            Актуальные адресные действия и полнота снимка обоих источников.
        """
        pending = self._read_pending_decisions(context.app_user.id, PendingOrigin(origin.channel, origin.chat_id))
        return pending.as_query_result(origin)

    def _read_pending_decisions(self, app_user_id: int, origin: PendingOrigin) -> _PendingDecisions:
        """Сохраняет прочитанные действия при сбое одного источника."""
        proposals = ()
        operation = None
        complete = self._obsidian_confirmation_service is not None and self._vault_selection_manager is not None
        if self._obsidian_confirmation_service is not None:
            try:
                proposals = tuple(
                    proposal for proposal in self._obsidian_confirmation_service.list_pending(app_user_id, origin)
                    if proposal.origin == origin and proposal.app_user_id == app_user_id and proposal.id is not None
                    and proposal.action is not ObsidianProposalAction.UNKNOWN
                    and proposal.status in {ObsidianProposalStatus.PENDING, ObsidianProposalStatus.EXECUTING}
                )
            except Exception:
                LOGGER.exception("Could not query Obsidian proposals")
                complete = False
        if self._vault_selection_manager is not None:
            try:
                pending = self._vault_selection_manager.get_pending(app_user_id, origin)
                if (pending is not None and pending.origin == origin and pending.app_user_id == app_user_id
                        and pending.id is not None and pending.status is VaultConfirmationStatus.PENDING
                        and pending.action in {VaultConfirmationAction.REPLACE, VaultConfirmationAction.DISCONNECT}):
                    operation = pending
            except Exception:
                LOGGER.exception("Could not query Obsidian vault confirmations")
                complete = False
        return _PendingDecisions(proposals, operation, complete)

    def _decision(
        self, text: str, app_user: AppUser, origin: PendingOrigin
    ) -> ObsidianMessageResult | None:
        """Разрешает локальный ответ либо проверяет адресную текстовую команду."""
        normalized = text.lower()
        simple = _is_confirmation_reply(text)
        parts = text.split()
        if not simple and (not parts or parts[0] != "/confirm"):
            return None
        if simple:
            snapshot = self.list_pending_confirmations(
                HandlerContext(app_user), ReplyAddress(origin.channel, origin.chat_id),
            )
            if snapshot.status != "complete":
                return _operation_message(
                    "Не удалось получить полный список действий Obsidian. Ответ «да» или «нет» не обработан. "
                    "Открой «Подтверждения» позже, чтобы получить актуальные действия.", app_user,
                )
            if len(snapshot.confirmations) != 1:
                return _operation_message(
                    (
                        "Есть несколько ожидающих действий, поэтому ответ «да» или «нет» неоднозначен. Открой «Подтверждения» и выбери нужное действие."
                        if snapshot.confirmations
                        else "В этом чате нет действия Obsidian, которое можно подтвердить. Доступные действия описаны в справке."
                    ),
                    app_user,
                )
            item = snapshot.confirmations[0]
            operation = item.operation
            operation_id, version = item.confirmation_id, item.version
            approve = normalized in _APPROVAL_REPLIES
        else:
            if (
                len(parts) != 6
                or parts[1] != "obsidian"
                or parts[2] not in {"proposal", "replace", "disconnect"}
                or any(not value.isascii() or not value.isdecimal() or len(value) > 19
                       or not 0 < int(value) <= 2**63 - 1 for value in parts[3:5])
                or parts[5].lower() not in {"yes", "no"}
            ):
                return _operation_message(
                    "Не удалось распознать решение. Открой «Подтверждения» и выбери нужное действие.",
                    app_user,
                )
            operation, operation_id, version = parts[2], int(parts[3]), int(parts[4])
            approve = parts[5].lower() == "yes"
        if operation == "proposal":
            if self._obsidian_confirmation_service is None:
                return _operation_message("Служба подтверждений Obsidian недоступна. Действие не обработано; открой «Подтверждения» после восстановления службы.", app_user)
            method = (
                self._obsidian_confirmation_service.confirm
                if approve
                else self._obsidian_confirmation_service.cancel
            )
            return self._review_confirmation_result(
                app_user,
                method(app_user.id, proposal_id=operation_id, origin=origin, version=version),
            )
        if self._vault_selection_manager is None:
            return _operation_message("Подключение GitHub сейчас недоступно.", app_user)
        try:
            result = self._vault_selection_manager.decide(
                app_user.id,
                confirmation_id=operation_id,
                origin=origin,
                version=version,
                action=VaultConfirmationAction(operation),
                approve=approve,
            )
        except (PendingOperationError, VaultResourceBusyError) as error:
            return _operation_message(str(error), app_user)
        if isinstance(result, VaultSelectionResult):
            message = ObsidianMessageResult(
                type=(
                    ObsidianResultType.GITHUB_VAULT_REPLACED
                    if approve
                    else ObsidianResultType.GITHUB_VAULT_REPLACEMENT_CANCELLED
                ),
                app_user=app_user,
                vault_selection=result,
            )
            return self._sync_vault(app_user, base_result=message) if approve else message
        return ObsidianMessageResult(
            type=(
                ObsidianResultType.GITHUB_DISCONNECTED
                if approve
                else ObsidianResultType.GITHUB_DISCONNECT_CANCELLED
            ),
            app_user=app_user,
            vault_disconnect=result,
        )

    def _unavailable_article_url(self, url: str, user: AppUser) -> ObsidianMessageResult | None:
        """Останавливает неизвестный цикл статьи до sync, записи и захвата lease."""
        article = self._article_url_use_case.get_saved_for_url(url, user.id)
        if article is None:
            return None
        if article.status is ArticleStatus.UNKNOWN:
            return _operation_message(
                "Статус этой статьи неизвестен текущей версии. Данные не изменены.", user
            )
        service = self._obsidian_confirmation_service
        if service is None or article.id is None:
            return None
        proposal = service.get_active_for_article(app_user_id=user.id, article_id=article.id)
        if proposal is None and article.status is ArticleStatus.REVIEWED:
            proposal = service.get_latest_applied(app_user_id=user.id, article_id=article.id)
        if proposal is not None and (
            proposal.action is ObsidianProposalAction.UNKNOWN
            or proposal.status is ObsidianProposalStatus.UNKNOWN
        ):
            return _operation_message(
                "Предложение этой статьи недоступно текущей версии. Данные не изменены.", user
            )
        return None

    def _process_article_url(
        self, incoming_message: ArticleIncomingMessage, app_user_result: AppUser
    ) -> ObsidianMessageResult:
        """Резервирует один URL на весь fetch/analysis/review между TG/VK."""
        url = extract_first_supported_url(incoming_message.text)
        unavailable = self._unavailable_article_url(url, app_user_result) if url else None
        if unavailable is not None:
            return unavailable
        if url is None or self._article_flow_lease_repository is None:
            return self._run_article_url(incoming_message, app_user_result)
        try:
            normalized_url = normalize_article_url(url)
        except ValueError:
            return self._run_article_url(incoming_message, app_user_result)
        owner = uuid4().hex
        now = datetime.now(UTC)
        if not self._article_flow_lease_repository.acquire(
            app_user_id=app_user_result.id,
            normalized_url=normalized_url,
            owner=owner,
            now=now,
            expires_at=now + timedelta(minutes=30),
        ):
            return _operation_message(
                "Эта статья уже обрабатывается. Повтори ссылку после завершения.", app_user_result
            )
        try:
            return self._run_article_url(incoming_message, app_user_result)
        finally:
            self._article_flow_lease_repository.release(
                app_user_id=app_user_result.id, normalized_url=normalized_url, owner=owner
            )

    def _run_article_url(
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

        unavailable = self._unavailable_article_url(url, app_user_result)
        if unavailable is not None:
            return unavailable

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
            auto_sync_result if isinstance(auto_sync_result, VaultSyncResult) else None
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

        if (
            self._obsidian_confirmation_service is not None
            and article_result.article.id is not None
        ):
            active = self._obsidian_confirmation_service.get_active_for_article(
                app_user_id=app_user_result.id, article_id=article_result.article.id
            )
            if active is not None:
                if (
                    active.action is ObsidianProposalAction.UNKNOWN
                    or active.status is ObsidianProposalStatus.UNKNOWN
                ):
                    return _operation_message(
                        "Предложение этой статьи недоступно текущей версии. Данные не изменены.",
                        app_user_result,
                    )
                origin = PendingOrigin(incoming_message.channel, incoming_message.chat_id)
                if active.origin != origin:
                    return _operation_message(
                        "Эта статья уже обрабатывается. Заверши действие в исходном чате.",
                        app_user_result,
                    )
                return self._review_confirmation_result(
                    app_user_result,
                    ObsidianConfirmationResult(ObsidianConfirmationStatus.PENDING, proposal=active),
                )
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
                if applied.action is ObsidianProposalAction.UNKNOWN:
                    return _operation_message(
                        "Итог этой статьи недоступен текущей версии. Данные не изменены.",
                        app_user_result,
                    )
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
            "Article analysis completed: article_id=%s analysis_id=%s created=%s " "model=%s",
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
                        origin=PendingOrigin(incoming_message.channel, incoming_message.chat_id),
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
                type=(ObsidianResultType.GITHUB_VAULT_CONFIGURATION_REQUIRED),
                app_user=app_user,
                error=error,
            )
        except (GitHubGatewayError, OSError, ValueError, RuntimeError) as error:
            LOGGER.warning(
                "Automatic vault check failed: " "app_user_id=%s error_type=%s",
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
            last_checked_at=(status.vault.last_checked_at if status.vault is not None else None),
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
        if confirmation.status is ObsidianConfirmationStatus.FOREIGN_ORIGIN:
            return _operation_message(
                "Заверши действие в исходном чате; операция здесь недоступна.", app_user
            )
        if confirmation.status is ObsidianConfirmationStatus.STALE:
            return _operation_message(
                "Это действие устарело. Актуальные действия доступны в исходном чате.", app_user
            )
        result_types = {
            ObsidianConfirmationStatus.PENDING: (
                ObsidianResultType.ARTICLE_REVIEW_CONFIRMATION_PENDING
            ),
            ObsidianConfirmationStatus.CANCELLED: (ObsidianResultType.ARTICLE_REVIEW_CANCELLED),
            ObsidianConfirmationStatus.APPLIED: (ObsidianResultType.ARTICLE_REVIEW_APPLIED),
            ObsidianConfirmationStatus.CONFLICT: (ObsidianResultType.ARTICLE_REVIEW_CONFLICT),
            ObsidianConfirmationStatus.IN_PROGRESS: (ObsidianResultType.ARTICLE_REVIEW_IN_PROGRESS),
            ObsidianConfirmationStatus.FAILED: (ObsidianResultType.ARTICLE_REVIEW_APPLY_FAILED),
            ObsidianConfirmationStatus.NO_PENDING: (ObsidianResultType.CONFIRMATION_MISSING),
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
        origin: PendingOrigin,
        *,
        completion_handler: ObsidianCompletionHandler | None,
    ) -> ObsidianMessageResult:
        """Подключает vault либо запускает необходимую GitHub-авторизацию."""
        result = self._select_github_vault(arguments, app_user, origin)
        if result.type is ObsidianResultType.GITHUB_VAULT_GITHUB_REQUIRED:
            return self._start_github_connection(
                arguments,
                app_user,
                origin,
                completion_handler=completion_handler,
            )
        if (
            result.type is ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE
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
        origin: PendingOrigin,
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
                        type=(ObsidianResultType.GITHUB_AUTHORIZED_SYNC_STARTED),
                        app_user=app_user,
                        github_completion=completion,
                    )
                )
            result = processor(
                repository_arguments,
                app_user,
                completion,
                origin,
            )
            if completion_handler is not None:
                completion_handler(result)

        try:
            connection = self._github_connection_starter.start(
                app_user.id,
                origin,
                complete_github,
            )
        except (GitHubGatewayError, OSError, ValueError) as error:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_FAILED,
                app_user=app_user,
                error=error,
            )
        result_types = {
            GitHubConnectionStartStatus.STARTED: (ObsidianResultType.GITHUB_CONNECT_STARTED),
            GitHubConnectionStartStatus.ALREADY_PENDING: (
                ObsidianResultType.GITHUB_CONNECT_ALREADY_PENDING
            ),
            GitHubConnectionStartStatus.PREPARING: (ObsidianResultType.GITHUB_CONNECT_PREPARING),
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
        origin: PendingOrigin,
    ) -> ObsidianMessageResult:
        """Продолжает подключение vault после завершения фонового Device Flow."""
        if completion.status is GitHubConnectionCompletionStatus.CONNECTED:
            result = self._select_github_vault(repository_arguments, app_user, origin)
            if (
                result.type is ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE
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
        origin: PendingOrigin,
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
                origin=origin,
                repository_url=parts[0],
                root_path=parts[1] if len(parts) == 2 else "",
            )
        except (PendingOperationError, VaultResourceBusyError) as error:
            return _operation_message(str(error), app_user)
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
            VaultSelectionStatus.SELECTED: (ObsidianResultType.GITHUB_VAULT_SELECTED),
            VaultSelectionStatus.ALREADY_SELECTED: (
                ObsidianResultType.GITHUB_VAULT_ALREADY_SELECTED
            ),
            VaultSelectionStatus.REPLACEMENT_CONFIRMATION_REQUIRED: (
                ObsidianResultType.GITHUB_VAULT_REPLACEMENT_CONFIRMATION_REQUIRED
            ),
        }
        result = ObsidianMessageResult(
            type=result_types[selection.status],
            app_user=app_user,
            vault_selection=selection,
            pending_operation=selection.confirmation,
        )
        if selection.status is VaultSelectionStatus.SELECTED:
            return self._sync_vault(app_user, base_result=result)
        return result

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
        origin: PendingOrigin,
    ) -> ObsidianMessageResult:
        """Запрашивает явное подтверждение отключения активного vault."""
        if self._vault_selection_manager is None:
            return ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE,
                app_user=app_user,
            )
        try:
            disconnect = self._vault_selection_manager.request_disconnect(app_user.id, origin)
        except (PendingOperationError, VaultResourceBusyError) as error:
            return _operation_message(str(error), app_user)
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
            pending_operation=disconnect.confirmation,
        )

    def _reanalyze_article(
        self, arguments: str, incoming_message: ArticleIncomingMessage, app_user: AppUser
    ) -> ObsidianMessageResult:
        """Повторно анализирует статью, не подменяя активный proposal её цикла."""
        if not _valid_article_id(arguments):
            return self._run_reanalyze_article(arguments, incoming_message, app_user)
        article = self._article_url_use_case.get_saved_for_user(int(arguments), app_user.id)
        if article is None:
            return _operation_message("Статья недоступна в этом профиле.", app_user)
        if article.status is ArticleStatus.UNKNOWN:
            return _operation_message(
                "Статус этой статьи неизвестен текущей версии. Данные не изменены.",
                app_user,
            )
        if self._obsidian_confirmation_service is not None:
            active = self._obsidian_confirmation_service.get_active_for_article(
                app_user_id=app_user.id, article_id=article.id or 0
            )
            if active is not None:
                return _operation_message(
                    "Для этой статьи уже ожидается действие. Заверши его в исходном чате перед повторным анализом.",
                    app_user,
                )
        repository = self._article_flow_lease_repository
        if repository is None:
            return self._run_reanalyze_article(arguments, incoming_message, app_user)
        owner = uuid4().hex
        now = datetime.now(UTC)
        if not repository.acquire(
            app_user_id=app_user.id,
            normalized_url=article.normalized_url,
            owner=owner,
            now=now,
            expires_at=now + timedelta(minutes=30),
        ):
            return _operation_message(
                "Эта статья уже обрабатывается. Повтори команду после завершения.", app_user
            )
        try:
            return self._run_reanalyze_article(arguments, incoming_message, app_user)
        finally:
            repository.release(
                app_user_id=app_user.id, normalized_url=article.normalized_url, owner=owner
            )

    def _run_reanalyze_article(
        self,
        arguments: str,
        incoming_message: ArticleIncomingMessage,
        app_user: AppUser,
    ) -> ObsidianMessageResult:
        """Повторно анализирует сохранённую статью по команде пользователя."""
        if not _valid_article_id(arguments):
            return ObsidianMessageResult(
                type=ObsidianResultType.REANALYZE_COMMAND_INVALID,
                app_user=app_user,
            )
        article = self._article_url_use_case.get_saved_for_user(int(arguments), app_user.id)
        if article is not None and article.status is ArticleStatus.UNKNOWN:
            return _operation_message(
                "Статус этой статьи неизвестен текущей версии. Данные не изменены.",
                app_user,
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
                        origin=PendingOrigin(incoming_message.channel, incoming_message.chat_id),
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
        True для прежнего предиката ранней квитанции.
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
    from compadre_assistant.capabilities.obsidian.presentation.responses import (
        format_obsidian_message_result,
    )

    actions = ()
    proposal = (
        result.review_result.proposal
        if result.review_result
        else (result.obsidian_confirmation.proposal if result.obsidian_confirmation else None)
    )
    if (proposal is not None and proposal.id is not None and proposal.status is ObsidianProposalStatus.PENDING
            and proposal.action is not ObsidianProposalAction.UNKNOWN):
        actions = _proposal_actions(proposal)
    pending = result.pending_operation
    if (pending is not None and pending.id is not None and pending.status is VaultConfirmationStatus.PENDING
            and pending.action in {VaultConfirmationAction.REPLACE, VaultConfirmationAction.DISCONNECT}):
        actions += _vault_operation_actions(pending)
    content = (text_content(result.text_override) if result.text_override is not None
               else format_obsidian_message_result(result))
    return IncomingResponse(content, result.error, actions, suggested_actions=_github_link_actions(result))


def _github_link_actions(result: ObsidianMessageResult) -> tuple[ReplyAction, ...]:
    """Открывает текущий шаг GitHub, оставляя буквальную ссылку в ответе."""
    connection = result.github_connection
    if (result.type in (ObsidianResultType.GITHUB_CONNECT_STARTED, ObsidianResultType.GITHUB_CONNECT_ALREADY_PENDING)
            and connection is not None and connection.authorization is not None):
        label, url = "Авторизовать GitHub", connection.authorization.verification_uri
    elif result.installation_url:
        label, url = "Открыть настройки GitHub App", result.installation_url
    else:
        return ()
    try:
        return (ReplyAction(label, url=url),)
    except ValueError:
        return ()


def _proposal_actions(proposal: ObsidianProposal) -> tuple[ConfirmationAction, ...]:
    """Создаёт короткие подписи с ID статьи, общий текст решения остаётся в ответе."""
    approve = "Завершить разбор" if proposal.action is ObsidianProposalAction.SKIP else "Принять предложение"
    return _operation_actions(
        "proposal", proposal.id or 0, proposal.version,
        f"{approve} №{proposal.article_id}", f"Отменить запрос №{proposal.article_id}",
    )


def _vault_operation_actions(pending: VaultActionConfirmation) -> tuple[ConfirmationAction, ...]:
    """Подписывает конкретное действие с хранилищем и отказ от него."""
    approve = "Подтвердить замену" if pending.action is VaultConfirmationAction.REPLACE else "Подтвердить отключение"
    return _operation_actions(pending.action.value, pending.id or 0, pending.version, approve, "Сохранить подключение")


def _operation_actions(
    operation: str, operation_id: int, version: int, approve_label: str, reject_label: str,
) -> tuple[ConfirmationAction, ...]:
    """Создаёт команды с неизменными ID операции и показанной пользователю версией."""
    return tuple(
        ConfirmationAction(
            f"obsidian:{operation}:{operation_id}:{version}:{decision}",
            label,
            f"/confirm obsidian {operation} {operation_id} {version} {decision}",
        )
        for decision, label in (("yes", approve_label), ("no", reject_label))
    )


def _with_short_answer_hint(response: IncomingResponse, snapshot: ConfirmationQueryResult) -> IncomingResponse:
    """Предлагает короткий ответ только для единственного показанного ожидания."""
    if (snapshot.status != "complete" or len(snapshot.confirmations) != 1
            or response.actions != snapshot.confirmations[0].actions):
        return response
    return replace(response, content=compose_content(
        response.content, "\n\nВ этом чате можно ответить «да», чтобы подтвердить, или «нет», чтобы отменить запрос.",
    ))


def _operation_message(text: str, user: AppUser) -> ObsidianMessageResult:
    """Оформляет краткий ответ о доступности операции без её содержимого."""
    return ObsidianMessageResult(
        type=ObsidianResultType.CONFIRMATION_MISSING, app_user=user, text_override=text
    )


def _valid_article_id(value: str) -> bool:
    """Принимает числовой ID в диапазоне SQLite, исключая Unicode-псевдоцифры."""
    return bool(value) and value.isascii() and value.isdecimal() and len(value) <= 19 and int(value) <= 2**63 - 1
