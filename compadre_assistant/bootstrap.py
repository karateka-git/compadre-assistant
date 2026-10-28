"""Собирает общие зависимости приложения и явно регистрирует возможности."""

from __future__ import annotations

import sqlite3
import logging
from dataclasses import replace
from threading import Event, RLock
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator

from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.presentation.incoming.action_selection import ResponseActionStore
from compadre_assistant.capabilities.ask.bootstrap import create_ask_runtime
from compadre_assistant.capabilities.ask.data.configuration import AskConfig, load_ask_config
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability

from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    ChannelCompletionHandler,
    CommandMenuUpdate,
    CommandMenuUpdateHandler,
    IncomingCapabilityPort,
    IncomingResponse,
    BackgroundReply,
)
from compadre_assistant.core.presentation.incoming.dispatcher import (
    IncomingMessageDispatcher,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry

from compadre_assistant.core.domain.users.identity import UserIdentityService
from compadre_assistant.core.domain.users.entities import is_private_chat_address
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


LOGGER = logging.getLogger(__name__)
# Неполный снимок требует нового чтения, даже если консервативное меню доставлено.
INCOMPLETE_MENU_SNAPSHOT = "Неполный снимок подтверждений"


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
        self._pending_inputs = PendingInputStore()
        self._response_actions = ResponseActionStore()
        self._registry = CapabilityRegistry(capabilities, disabled_capabilities)
        # Одна блокировка сохраняет порядок публикаций при параллельных событиях.
        self._menu_lock = RLock()
        self._published_menus: dict[ReplyAddress, CommandMenuUpdate] = {}
        self._menu_failures: dict[ReplyAddress, str] = {}
        self._unregistered_menu_events: dict[ReplyAddress, IncomingEvent] = {}

    @property
    def menu_delivery_failures(self) -> dict[ReplyAddress, str]:
        """Возвращает снимок ошибок доставки, ожидающих повторной попытки."""
        with self._menu_lock:
            return dict(self._menu_failures)

    def reconcile_menus(self, handler: CommandMenuUpdateHandler) -> None:
        """Восстанавливает или сверяет меню всех известных личных чатов.

        Вызывать при запуске с готовым обработчиком и после изменения серверной
        доступности или подтверждений. Повторный вызов проверяет TTL ввода и
        доставляет только изменившиеся либо ранее не доставленные меню.

        Args:
            handler: Нейтральный callback публикации меню вне SQLite scope.
        """
        with self._menu_lock:
            try:
                with connect_database(self._database_path) as connection:
                    service = create_user_identity_service(connection)
                    identities = service.list_all_known_private_chats()
            except Exception:
                LOGGER.exception("Could not enumerate known chats for menu reconciliation")
                return
            events = {
                ReplyAddress(identity.channel, identity.external_chat_id):
                    (IncomingEvent(identity.channel, identity.external_chat_id, "menu-reconcile",
                                   "/help"), False)
                for _, identity in identities
            }
            for address in self._pending_inputs.addresses():
                if address not in events and address in self._unregistered_menu_events:
                    events[address] = (self._unregistered_menu_events[address], True)
            for address in self._menu_failures:
                if address not in events and address in self._unregistered_menu_events:
                    events[address] = (self._unregistered_menu_events[address], True)
            for event, allow_unknown in events.values():
                self._reconcile_menu_event(event, handler, allow_unknown=allow_unknown)

    def run_menu_reconciliation(
        self, handler: CommandMenuUpdateHandler, stop: Event, *, interval_seconds: float = 30.0,
    ) -> None:
        """Запускает сверку при старте и периодически до сигнала остановки.

        Args:
            handler: Callback канала для публикации меню.
            stop: Сигнал завершения фонового цикла владельцем процесса.
            interval_seconds: Период проверки TTL и повторных доставок.

        Raises:
            ValueError: Период не положителен.
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.reconcile_menus(handler)
        while not stop.wait(interval_seconds):
            self.reconcile_menus(handler)

    def _reconcile_menu_event(
        self, event: IncomingEvent, handler: CommandMenuUpdateHandler, *,
        allow_unknown: bool,
    ) -> None:
        """Под процессной блокировкой перечитывает состояние перед доставкой."""
        address = event.reply_address
        if allow_unknown and not is_private_chat_address(
                address.channel, event.external_user_id, address.chat_id):
            return
        try:
            with connect_database(self._database_path) as connection:
                service = create_user_identity_service(connection)
                known = service.find_known_private_chat(address.channel, address.chat_id)
                if known is not None:
                    owner, current = known
                    if event.external_user_id != current.external_user_id:
                        # Явный автор исходного события не может получить меню
                        # другого владельца совпавшего адреса. Только внутренний
                        # проход без автора подставляет текущую identity из БД.
                        if event.external_user_id is not None:
                            return
                        event = IncomingEvent(address.channel, address.chat_id, "menu-reconcile",
                                              "/help", current.external_user_id)
                elif not allow_unknown or service.has_known_chat_address(address.channel, address.chat_id):
                    return
                else:
                    self._unregistered_menu_events[address] = event
                dispatcher = IncomingMessageDispatcher(
                    registry=self._registry, pending_inputs=self._pending_inputs,
                    user_identity_service=service,
                    chat_mode_repository=SQLiteChatModeRepository(connection),
                    capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
                )
                menu, complete = dispatcher.command_menu_snapshot(
                    event, known_user=owner if known is not None else None)
                if known is not None and not service.owns_known_private_chat(owner, current):
                    return
            if not complete:
                # Проекция уже скрыла /confirm. Доставляем её, чтобы прежнее
                # полное меню не оставалось видимым при ошибке источника.
                self._menu_failures.setdefault(address, INCOMPLETE_MENU_SNAPSHOT)
            update = CommandMenuUpdate(address, menu)
            if (self._published_menus.get(address) == update
                    and self._menu_failures.get(address) in (None, INCOMPLETE_MENU_SNAPSHOT)):
                if complete:
                    self._menu_failures.pop(address, None)
                    if self._pending_inputs.get(address) is None:
                        self._unregistered_menu_events.pop(address, None)
                return
            handler(update)
            self._published_menus[address] = update
            if complete:
                self._menu_failures.pop(address, None)
                if self._pending_inputs.get(address) is None:
                    self._unregistered_menu_events.pop(address, None)
        except Exception as error:
            self._menu_failures[address] = str(error)
            LOGGER.exception("Could not deliver reconciled command menu")

    def __call__(self, incoming_message: IncomingEvent,
                 completion_handler: ChannelCompletionHandler | None = None,
                 menu_update_handler: CommandMenuUpdateHandler | None = None) -> IncomingResponse:
        """Обрабатывает событие в коротком core scope.

        Args:
            incoming_message: Событие с identity и адресом исходного чата.
            completion_handler: Доставка последующего ответа каналу.
            menu_update_handler: Необязательная доставка изменившегося меню.

        Returns:
            Текстовый ответ независимо от результата обновления меню.
        """
        return self._process(incoming_message, completion_handler, menu_update_handler)

    def select_action(self, event: IncomingEvent, token: str,
                      completion_handler: ChannelCompletionHandler | None = None,
                      menu_update_handler: CommandMenuUpdateHandler | None = None) -> IncomingResponse:
        """Передаёт callback адаптера общему безопасному маршруту выбора.

        Args:
            event: Проверенные адаптером канал, чат, явный автор и ID callback.
                Текст игнорируется; при отсутствии текста можно передать token.
            token: Только непрозрачный token из response.bound_actions.
            completion_handler: Доставка фоновых ответов с тем же контрактом.
            menu_update_handler: Публикация изменившегося меню.

        Returns:
            Обычный ответ с новыми действиями либо безопасный отказ.
        """
        return self._process(event, completion_handler, menu_update_handler,
                             token=token if isinstance(token, str) else "")

    def _process(self, incoming_message: IncomingEvent,
                 completion_handler: ChannelCompletionHandler | None,
                 menu_update_handler: CommandMenuUpdateHandler | None,
                 *, token: str | None = None) -> IncomingResponse:
        menu_updates: list[CommandMenuUpdate] = []
        with connect_database(self._database_path) as connection:
            dispatcher = IncomingMessageDispatcher(
                registry=self._registry,
                pending_inputs=self._pending_inputs,
                user_identity_service=create_user_identity_service(connection),
                chat_mode_repository=SQLiteChatModeRepository(connection),
                capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
            )
            try:
                original_context = dispatcher.action_context(incoming_message)
            except Exception:
                original_context = None

            def deliver(reply: BackgroundReply) -> None:
                """Связывает фоновые действия только с неизменившимся исходным чатом."""
                response = replace(reply.response, bound_actions=())
                if original_context is not None and reply.address == incoming_message.reply_address:
                    try:
                        with connect_database(self._database_path) as background_connection:
                            background_dispatcher = IncomingMessageDispatcher(
                                registry=self._registry, pending_inputs=self._pending_inputs,
                                user_identity_service=create_user_identity_service(background_connection),
                                chat_mode_repository=SQLiteChatModeRepository(background_connection),
                                capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(background_connection),
                            )
                            if background_dispatcher.action_context(incoming_message) == original_context:
                                response = background_dispatcher.bind_response_actions(
                                    incoming_message, response, self._response_actions,
                                )
                    except Exception:
                        LOGGER.exception("Could not bind background response actions")
                completion_handler(BackgroundReply(reply.address, response))

            completion = deliver if completion_handler is not None else None
            menu_handler = menu_updates.append if menu_update_handler is not None else None
            if token is None:
                response = dispatcher.execute(incoming_message, completion, menu_handler)
            else:
                response = dispatcher.select_action(
                    incoming_message, token, self._response_actions, completion, menu_handler,
                )
            response = dispatcher.bind_response_actions(incoming_message, response, self._response_actions)
        if menu_update_handler is not None:
            with self._menu_lock:
                # Первое сообщение личного чата также требует публикации:
                # до него у адаптера ещё нет снимка, даже если команда не меняла меню.
                addresses = list(dict.fromkeys(update.address for update in menu_updates))
                if (incoming_message.reply_address not in addresses
                        and incoming_message.reply_address not in self._published_menus):
                    addresses.append(incoming_message.reply_address)
                for address in addresses:
                    self._reconcile_menu_event(
                        IncomingEvent(address.channel, address.chat_id,
                                      "menu-reconcile", "/help",
                                      incoming_message.external_user_id
                                      if address == incoming_message.reply_address else None),
                        menu_update_handler,
                        allow_unknown=address == incoming_message.reply_address,
                    )
        return response


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
