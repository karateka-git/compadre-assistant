"""Собирает общие зависимости приложения и явно регистрирует возможности."""

from __future__ import annotations

import sqlite3
import logging
from dataclasses import replace
from threading import Event, RLock
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Callable, Iterator, Mapping

from compadre_assistant.core.presentation.incoming.pending_input import PendingInput, PendingInputStore
from compadre_assistant.core.presentation.incoming.action_selection import ResponseActionStore
from compadre_assistant.capabilities.admin.bootstrap import create_admin_runtime
from compadre_assistant.capabilities.admin.data.configuration import load_admin_profile, load_admin_timeweb_config
from compadre_assistant.capabilities.admin.presentation.handler import AdminCapability
from compadre_assistant.capabilities.ai.bootstrap import create_ai_runtime
from compadre_assistant.capabilities.ai.data.configuration import AiConfig, load_ai_config
from compadre_assistant.capabilities.ai.presentation.handler import AiCapability
from compadre_assistant.capabilities.family.bootstrap import create_family_capability, create_shopping_notifications
from compadre_assistant.capabilities.family.data.expense_recognition import ReceiptRecognizer, receipt_timeout_seconds
from compadre_assistant.capabilities.family.presentation.handler import FamilyCapability
from compadre_assistant.capabilities.settings.presentation.handler import SettingsCapability
from compadre_assistant.core.data.config import load_messenger_dialogs
from compadre_assistant.core.presentation.incoming.deferred_actions import DeferredActions

from compadre_assistant.core.domain.capability_preferences import CapabilityDefault, GetUserCapabilitySelections
from compadre_assistant.core.presentation.notifications import NotificationRetry, NotificationSend, NotificationSource, NotificationTarget
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    ChannelCompletionHandler,
    CommandMenuUpdate,
    CommandMenuUpdateHandler,
    IncomingCapabilityPort,
    IncomingResponse,
    BackgroundReply,
    PendingInputExpiryHandler,
    ReplyAction,
)
from compadre_assistant.core.presentation.incoming.dispatcher import (
    IncomingMessageDispatcher,
    pending_expired_reply,
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
# Публичный ID режима для серверной конфигурации и диагностики.
AI_CAPABILITY_ID = AiCapability.descriptor.capability_id
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
    profile_access: Mapping[str, Callable[[int], bool]] | None = None,
    messenger_dialogs: Mapping[str, str | None] | None = None,
) -> IncomingMessageDispatcher:
    """Явно регистрирует собранные возможности и общий сценарий профиля.

    Args:
        capabilities: Только включённые обработчики, включая пользовательские реализации.
        user_identity_service: Общий сервис проверенных identities.
        chat_mode_repository: Хранилище режима конкретного чата.
        capability_preference_repository: Хранилище личного выбора AppUser.
        disabled_capabilities: Метаданные отключённых входов без внешних зависимостей.
        profile_access: Проверки допуска к ограниченным возможностям по ID профиля.
        messenger_dialogs: Публичные диалоги включённых каналов установки.

    Returns:
        Диспетчер с проверенным реестром точек входа.

    Raises:
        ValueError: Реестр содержит конфликт ID или команд входа.
    """
    return IncomingMessageDispatcher(
        registry=CapabilityRegistry(
            (*capabilities, SettingsCapability(messenger_dialogs, lambda user_id: user_identity_service.linked_channels(user_id))),
            disabled_capabilities, profile_access=profile_access,
        ),
        user_identity_service=user_identity_service,
        chat_mode_repository=chat_mode_repository,
        capability_preference_repository=capability_preference_repository,
    )


def validate_enabled_capabilities(enabled_capabilities: tuple[str, ...]) -> None:
    """Проверяет выбранные ID относительно доступных сборок приложения.

    Args:
        enabled_capabilities: ID из конфигурации запуска.

    Raises:
        ValueError: Список содержит повторный или неизвестный ID.
    """
    if len(set(enabled_capabilities)) != len(enabled_capabilities):
        raise ValueError("Конфликт ID включённых возможностей")
    unknown = set(enabled_capabilities) - {
        "obsidian", AI_CAPABILITY_ID, AdminCapability.descriptor.capability_id, FamilyCapability.descriptor.capability_id,
    }
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
                 disabled_capabilities: tuple[CapabilityDescriptor, ...] = (),
                 profile_access: Mapping[str, Callable[[int], bool]] | None = None,
                 messenger_dialogs: Mapping[str, str | None] | None = None,
                 notification_sources: tuple[tuple[str, NotificationSource], ...] = ()) -> None:
        """Регистрирует обработчики при запуске без соединения с SQLite.

        Args:
            database_path: Общий путь SQLite для коротких core scopes.
            capabilities: Уже собранные входы включённых возможностей.
            disabled_capabilities: Метаданные отключённых входов.
            profile_access: Проверки допуска к ограниченным возможностям по ID профиля.
            messenger_dialogs: Публичные диалоги включённых каналов установки.

        Raises:
            ValueError: Повторный ID или команда входа в реестре.
        """
        self._notification_sources = notification_sources
        self._database_path = database_path
        self._pending_inputs = PendingInputStore()
        self._deferred_actions = DeferredActions()
        self._deferred_menu_closed = Event()
        self._response_actions = ResponseActionStore()
        self._registry = CapabilityRegistry(
            (*capabilities, SettingsCapability(messenger_dialogs, self._linked_channels)),
            disabled_capabilities, profile_access=profile_access,
        )
        # Одна блокировка сохраняет порядок публикаций при параллельных событиях.
        self._menu_lock = RLock()
        self._published_menus: dict[ReplyAddress, CommandMenuUpdate] = {}
        self._menu_failures: dict[ReplyAddress, str] = {}
        self._unregistered_menu_events: dict[ReplyAddress, IncomingEvent] = {}
        self._expiry_attempts: dict[ReplyAddress, tuple[str, CommandMenuUpdate]] = {}

    def _notification_target(self, capability_id: str, user_id: int) -> NotificationTarget:
        with connect_database(self._database_path) as db:
            # Профиль, доступ и личная привязка читаются из одного снимка.
            db.execute("BEGIN")
            user = SQLiteAppUserRepository(db).get_by_id(user_id)
            if user is None:
                return NotificationTarget(False)
            defaults = tuple(CapabilityDefault(d.capability_id, d.enabled_by_default)
                             for d in self._registry.descriptors())
            selections = GetUserCapabilitySelections(SQLiteUserCapabilityPreferenceRepository(db), defaults)
            personal = self._registry.for_user(selections.execute(user_id), app_user_id=user_id, private_chat=True)
            if personal.by_id(capability_id) is None:
                return NotificationTarget(False)
            identities = create_user_identity_service(db)
            matches = [identity for identity in identities.list_known_private_chats(user)
                       if identity.channel == user.notification_channel
                       and is_private_chat_address(identity.channel, identity.external_user_id, identity.external_chat_id)
                       and identities.owns_known_private_chat(user, identity)]
            if len(matches) != 1:
                return NotificationTarget(True)
            return NotificationTarget(True, ReplyAddress(matches[0].channel, matches[0].external_chat_id))

    def dispatch_notifications(self, channel: str, send: NotificationSend, *, now: datetime | None = None) -> None:
        """Предлагает подключённым источникам одну отправку через работающий канал.

        Args:
            channel: ID адаптера, запустившего фоновый цикл.
            send: Отправка одного готового сообщения с подтверждением API.
            now: Время проверки сроков; по умолчанию текущее UTC.
        """
        for capability_id, source in self._notification_sources:
            route = partial(self._notification_target, capability_id)

            def deliver(user_id: int, address: ReplyAddress, response: IncomingResponse) -> None:
                # Адаптер получает сообщение только для всё ещё выбранного и принадлежащего профилю адреса.
                try:
                    current = route(user_id)
                except Exception as error:
                    # Адаптер ещё не вызван, поэтому повтор безопасен.
                    raise NotificationRetry(5) from error
                if address.channel != channel or current != NotificationTarget(True, address):
                    raise NotificationRetry(1)
                send(address, response)

            source.dispatch(channel, now or datetime.now(UTC), route, deliver)

    def _linked_channels(self, app_user_id: int) -> frozenset[str]:
        with connect_database(self._database_path) as connection:
            return create_user_identity_service(connection).linked_channels(app_user_id)

    def close_deferred_actions(self) -> None:
        """Останавливает отложенные действия до закрытия зависимостей возможностей."""
        self._deferred_menu_closed.set()
        self._deferred_actions.close()

    def reply_for_unsupported_input(self, address: ReplyAddress) -> IncomingResponse | None:
        """Возвращает состояние занятого чата для события без поддерживаемого ввода.

        Args:
            address: Исходный адрес сообщения без текста и поддерживаемых вложений.

        Returns:
            Ответ занятого чата либо None для обычной подсказки адаптера.
        """
        with self._pending_inputs.lock(address):
            if self._deferred_actions.get(address) is not None:
                return IncomingMessageDispatcher.busy_reply()
        return None

    def _deferred_access(self, event: IncomingEvent, app_user_id: int,
                         capability_id: str, required_mode: str | None) -> bool:
        with connect_database(self._database_path) as connection:
            dispatcher = IncomingMessageDispatcher(
                registry=self._registry,
                user_identity_service=create_user_identity_service(connection),
                chat_mode_repository=SQLiteChatModeRepository(connection),
                capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
            )
            return dispatcher.may_finish_deferred(event, app_user_id, capability_id, required_mode)

    @property
    def menu_delivery_failures(self) -> dict[ReplyAddress, str]:
        """Возвращает снимок ошибок доставки, ожидающих повторной попытки."""
        with self._menu_lock:
            return dict(self._menu_failures)

    def reconcile_menus(
        self, handler: CommandMenuUpdateHandler, *, notify_expiry: PendingInputExpiryHandler | None = None,
    ) -> None:
        """Восстанавливает или сверяет меню всех известных личных чатов.

        Вызывать при запуске с готовым обработчиком и после изменения серверной
        доступности или подтверждений. Повторный вызов проверяет TTL ввода и
        передаёт каналу только изменившиеся либо ранее не доставленные меню.

        Args:
            handler: Callback вне SQLite scope. False означает, что канал
                оставил меню без публикации.
            notify_expiry: Доставка уведомления с меню для истёкшего ожидания VK.
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
                if address in events:
                    continue
                if address in self._unregistered_menu_events:
                    events[address] = (self._unregistered_menu_events[address], True)
                elif notify_expiry is not None:
                    events[address] = (
                        IncomingEvent(address.channel, address.chat_id, "menu-reconcile", "/help"), False,
                    )
            for address in self._menu_failures:
                if address not in events and address in self._unregistered_menu_events:
                    events[address] = (self._unregistered_menu_events[address], True)
            for event, allow_unknown in events.values():
                if notify_expiry is not None:
                    if event.channel != "vk" or self._notify_pending_expiry(event, allow_unknown, notify_expiry):
                        continue
                self._reconcile_menu_event(event, handler, allow_unknown=allow_unknown)

    def run_menu_reconciliation(
        self, handler: CommandMenuUpdateHandler, stop: Event, *, interval_seconds: float = 30.0,
        notify_expiry: PendingInputExpiryHandler | None = None,
    ) -> None:
        """Запускает сверку при старте и периодически до сигнала остановки.

        Args:
            handler: Callback канала для публикации меню.
            stop: Сигнал завершения фонового цикла владельцем процесса.
            interval_seconds: Период проверки TTL и повторных доставок.
            notify_expiry: Доставка уведомления с меню для истёкшего ожидания VK.

        Raises:
            ValueError: Период не положителен.
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.reconcile_menus(handler, notify_expiry=notify_expiry)
        while not stop.wait(interval_seconds):
            self.reconcile_menus(handler, notify_expiry=notify_expiry)

    def _notify_pending_expiry(
        self, event: IncomingEvent, allow_unknown: bool, notify: PendingInputExpiryHandler,
    ) -> bool:
        """Доставляет сообщение с актуальным меню при истечении ожидания."""
        address = event.reply_address
        with self._pending_inputs.lock(address):
            pending = self._pending_inputs.get(address)
            if pending is None or not pending.retry_command or not self._pending_inputs.expired(pending):
                self._expiry_attempts.pop(address, None)
                return False
            try:
                # Блокировка чата сохраняет порядок с отменой и заменой ожидания.
                # SQLite scope заканчивается до внешней отправки.
                snapshot = self._read_menu_snapshot(event, allow_unknown=allow_unknown, expired_input=pending)
                if snapshot is None:
                    self._pending_inputs.clear(address, pending.request_id)
                    self._expiry_attempts.pop(address, None)
                    return False
                update, complete, _ = snapshot
                attempt = self._expiry_attempts.get(address)
                if attempt is None or attempt[0] != pending.request_id:
                    attempt = (pending.request_id, update)
                    self._expiry_attempts[address] = attempt
                if notify(pending.request_id, pending_expired_reply(pending), update):
                    self._pending_inputs.clear(address, pending.request_id)
                    self._expiry_attempts.pop(address, None)
                    # VK может подтвердить первую попытку с тем же random_id.
                    if (attempt[1] == update
                            and self._read_menu_snapshot(event, allow_unknown=allow_unknown) == snapshot):
                        self._record_menu_delivery(update, complete)
                    else:
                        self._menu_failures[address] = "Состояние меню изменилось во время доставки"
                else:
                    self._menu_failures[address] = "Не удалось доставить уведомление об истечении"
            except Exception as error:
                self._menu_failures[address] = str(error)
                LOGGER.exception("Could not notify about expired pending input")
            return True

    def _reconcile_menu_event(
        self, event: IncomingEvent, handler: CommandMenuUpdateHandler, *,
        allow_unknown: bool,
        send: Callable[[CommandMenuUpdate], bool | None] | None = None,
    ) -> None:
        """Под процессной блокировкой перечитывает состояние перед доставкой."""
        address = event.reply_address
        try:
            snapshot = self._read_menu_snapshot(event, allow_unknown=allow_unknown)
            if snapshot is None:
                return
            update, complete, _ = snapshot
            if not complete:
                # Скрываем /confirm, пока источник подтверждений недоступен.
                self._menu_failures.setdefault(address, INCOMPLETE_MENU_SNAPSHOT)
            if (self._same_menu_projection(self._published_menus.get(address), update)
                    and self._menu_failures.get(address) in (None, INCOMPLETE_MENU_SNAPSHOT)):
                if complete:
                    self._acknowledge_menu(update)
                return
            sent = send(update) if send is not None else None
            if sent is False:
                self._menu_failures[address] = "Не удалось доставить меню с ответом"
                return
            if sent is None:
                if send is not None and not self._same_menu_snapshot(
                    self._read_menu_snapshot(event, allow_unknown=allow_unknown), snapshot,
                ):
                    self._menu_failures[address] = "Состояние меню изменилось во время доставки"
                    return
                if handler(update) is False:
                    return
            if not self._same_menu_snapshot(self._read_menu_snapshot(event, allow_unknown=allow_unknown), snapshot):
                self._menu_failures[address] = "Состояние меню изменилось во время доставки"
                return
            self._record_menu_delivery(update, complete)
        except Exception as error:
            self._menu_failures[address] = str(error)
            LOGGER.exception("Could not deliver reconciled command menu")

    @staticmethod
    def _same_menu_projection(published: CommandMenuUpdate | None, current: CommandMenuUpdate) -> bool:
        if published is None or published.address != current.address:
            return False
        old_actions, actions = published.menu.section_actions, current.menu.section_actions
        if current.address.channel == "vk":
            # Новая редакция требует доставки даже при прежних подписях.
            return tuple((action.label, action.url, action.menu_revision) for action in (old_actions or ())) == tuple(
                (action.label, action.url, action.menu_revision) for action in (actions or ())
            )
        return published == current

    @classmethod
    def _same_menu_snapshot(
        cls, previous: tuple[CommandMenuUpdate, bool, int | None] | None,
        current: tuple[CommandMenuUpdate, bool, int | None],
    ) -> bool:
        return previous is not None and previous[1:] == current[1:] and cls._same_menu_projection(previous[0], current[0])

    def _record_menu_delivery(self, update: CommandMenuUpdate, complete: bool) -> None:
        if complete:
            self._acknowledge_menu(update)
        else:
            self._published_menus[update.address] = update
            self._menu_failures[update.address] = INCOMPLETE_MENU_SNAPSHOT

    def _acknowledge_menu(self, update: CommandMenuUpdate) -> None:
        self._published_menus[update.address] = update
        self._menu_failures.pop(update.address, None)
        if self._pending_inputs.get(update.address) is None:
            self._unregistered_menu_events.pop(update.address, None)

    def _read_menu_snapshot(
        self, event: IncomingEvent, *, allow_unknown: bool, expired_input: PendingInput | None = None,
        read_only: bool = False,
    ) -> tuple[CommandMenuUpdate, bool, int | None] | None:
        """Читает меню и владельца без доставки и без обновления identity."""
        address = event.reply_address
        if allow_unknown and not is_private_chat_address(
                address.channel, event.external_user_id, address.chat_id):
            return None
        with connect_database(self._database_path) as connection:
            service = create_user_identity_service(connection)
            known = service.find_known_private_chat(address.channel, address.chat_id)
            owner = None
            if known is not None:
                owner, current = known
                if event.external_user_id is None:
                    event = replace(event, external_user_id=current.external_user_id)
                elif event.external_user_id != current.external_user_id:
                    return None
            elif not allow_unknown or service.has_known_chat_address(address.channel, address.chat_id):
                return None
            elif not read_only:
                self._unregistered_menu_events[address] = event
            dispatcher = IncomingMessageDispatcher(
                registry=self._registry, pending_inputs=self._pending_inputs,
                deferred_actions=self._deferred_actions,
                user_identity_service=service,
                chat_mode_repository=SQLiteChatModeRepository(connection),
                capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
            )
            if expired_input is not None and not dispatcher.may_notify_input_expired(
                event, expired_input, known_user=owner,
            ):
                return None
            menu, complete = dispatcher.command_menu_snapshot(
                event, known_user=owner, preserve_expired=True,
                cleanup_pending=not read_only,
            )
            if known is not None and not service.owns_known_private_chat(*known):
                return None
        if address.channel == "vk" and menu.section_actions is not None:
            # В нижнем меню публикуются действия раздела; справка и подтверждения не меняют его снимок.
            menu = replace(menu, items=(), pending_input_label=None)
            complete = True
        return CommandMenuUpdate(address, menu), complete, owner.id if owner is not None else None

    def deliver_response_menu(
        self, event: IncomingEvent, handler: CommandMenuUpdateHandler,
        send: Callable[[CommandMenuUpdate | None], tuple[bool, bool | None]],
    ) -> bool:
        """Доставляет ответ и по возможности актуальное меню исходного чата.

        Args:
            event: Исходный адрес с явным автором события.
            handler: Адресная доставка меню, если её поддерживает канал.
            send: Доставка ответа. Возвращает успех всего ответа и успех
                доставки меню либо None при отсутствии подходящей части.

        Returns:
            Успех ответа, независимый от результата адресной доставки меню.
        """
        delivered = None

        def send_with_menu(update: CommandMenuUpdate) -> bool | None:
            nonlocal delivered
            # Даже исключение адаптера не должно вызвать повтор ответа без меню.
            delivered = False
            delivered, menu_delivered = send(update)
            return menu_delivered

        with self._menu_lock:
            if not self._deferred_menu_closed.is_set():
                self._reconcile_menu_event(event, handler, allow_unknown=True, send=send_with_menu)
        return send(None)[0] if delivered is None else delivered

    def section_actions(self, event: IncomingEvent) -> tuple[ReplyAction, ...] | None:
        """Читает действия раздела через проверенный снимок без исполнения команд.

        Args:
            event: Адрес и явный автор запроса; текст игнорируется.

        Returns:
            Действия раздела, включая пустой набор, либо None при отсутствии
            проекции раздела. Ошибка источника передаётся вызывающему.
        """
        with self._menu_lock:
            snapshot = self._read_menu_snapshot(event, allow_unknown=True, read_only=True)
        return snapshot[0].menu.section_actions if snapshot is not None else None

    def __call__(self, incoming_message: IncomingEvent,
                 completion_handler: ChannelCompletionHandler | None = None,
                 menu_update_handler: CommandMenuUpdateHandler | None = None, *,
                 defer_origin_menu: bool = False) -> IncomingResponse:
        """Обрабатывает событие в коротком core scope.

        Args:
            incoming_message: Событие с identity и адресом исходного чата.
            completion_handler: Доставка последующего ответа каналу.
            menu_update_handler: Необязательная доставка изменившегося меню.
            defer_origin_menu: Сверить меню текущего адреса при доставке ответа.

        Returns:
            Текстовый ответ независимо от результата обновления меню.
        """
        return self._process(incoming_message, completion_handler, menu_update_handler,
                             defer_origin_menu=defer_origin_menu)

    def select_action(self, event: IncomingEvent, token: str,
                      completion_handler: ChannelCompletionHandler | None = None,
                      menu_update_handler: CommandMenuUpdateHandler | None = None, *,
                      defer_origin_menu: bool = False) -> IncomingResponse:
        """Передаёт callback адаптера общему безопасному маршруту выбора.

        Args:
            event: Проверенные адаптером канал, чат, явный автор и ID callback.
                Текст игнорируется; при отсутствии текста можно передать token.
            token: Только непрозрачный token из response.bound_actions.
            completion_handler: Доставка фоновых ответов с тем же контрактом.
            menu_update_handler: Публикация изменившегося меню.
            defer_origin_menu: Сверить меню текущего адреса при доставке ответа.

        Returns:
            Обычный ответ с новыми действиями либо безопасный отказ.
        """
        return self._process(event, completion_handler, menu_update_handler,
                             token=token if isinstance(token, str) else "",
                             defer_origin_menu=defer_origin_menu)

    def _process(self, incoming_message: IncomingEvent,
                 completion_handler: ChannelCompletionHandler | None,
                 menu_update_handler: CommandMenuUpdateHandler | None,
                 *, token: str | None = None, defer_origin_menu: bool = False) -> IncomingResponse:
        action_store = self._response_actions
        generation = action_store.event_generation()
        menu_updates: list[CommandMenuUpdate] = []
        origin = IncomingEvent(
            incoming_message.channel, incoming_message.chat_id, incoming_message.message_id,
            "ответ", incoming_message.external_user_id, action_layout=incoming_message.action_layout,
        )

        def refresh_deferred_menu(address: ReplyAddress) -> None:
            if menu_update_handler is None:
                return
            with self._menu_lock:
                if self._deferred_menu_closed.is_set():
                    return
                # Сверка берёт актуального владельца и состояние по сохранённому адресу.
                event = IncomingEvent(address.channel, address.chat_id, "menu-reconcile", "/help")
                self._reconcile_menu_event(event, menu_update_handler, allow_unknown=False)

        with connect_database(self._database_path) as connection:
            dispatcher = IncomingMessageDispatcher(
                registry=self._registry,
                pending_inputs=self._pending_inputs,
                deferred_actions=self._deferred_actions,
                deferred_access=self._deferred_access,
                deferred_menu_refresh=refresh_deferred_menu if menu_update_handler is not None else None,
                user_identity_service=create_user_identity_service(connection),
                chat_mode_repository=SQLiteChatModeRepository(connection),
                capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
            )
            try:
                original_context = dispatcher.action_context(incoming_message)
            except Exception:
                original_context = None
            completion_context = original_context

            def deliver(reply: BackgroundReply) -> None:
                """Связывает фоновые действия только с неизменившимся исходным чатом."""
                response = replace(reply.response, bound_actions=(), after_delivery=None)
                if completion_context is not None and reply.address == origin.reply_address:
                    try:
                        with connect_database(self._database_path) as background_connection:
                            background_dispatcher = IncomingMessageDispatcher(
                                registry=self._registry, pending_inputs=self._pending_inputs,
                                deferred_actions=self._deferred_actions,
                                user_identity_service=create_user_identity_service(background_connection),
                                chat_mode_repository=SQLiteChatModeRepository(background_connection),
                                capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(background_connection),
                            )
                            if background_dispatcher.action_context(origin) == completion_context:
                                response = background_dispatcher.resume_navigation(origin, response)
                                response = background_dispatcher.bind_response_actions(
                                    origin, response, action_store, view_origin=completion_context,
                                    generation=generation, expected_context=completion_context,
                                )
                    except Exception:
                        LOGGER.exception("Could not bind background response actions")
                response = replace(response, suggested_actions=tuple(
                    action for action in response.suggested_actions if action.url is None
                ))
                completion_handler(BackgroundReply(reply.address, response))

            completion = deliver if completion_handler is not None else None
            menu_handler = menu_updates.append if menu_update_handler is not None else None
            if token is None:
                response = dispatcher.execute(incoming_message, completion, menu_handler)
            elif token.startswith("notice.") and original_context is not None and (
                    destination := dispatcher.notification_destination(incoming_message, token)) is not None:
                capability_id, entry_command, section_command = destination
                # Переход вызывает те же вход и команду раздела, что и ручная навигация.
                response = dispatcher.execute(replace(incoming_message, text=entry_command), completion, menu_handler)
                entered = dispatcher.action_context(incoming_message)
                if (response.error is None and entered.app_user_id == original_context.app_user_id
                        and entered.mode_id == capability_id
                        and entered.mode_revision == original_context.mode_revision + 1):
                    original_context = entered
                    response = dispatcher.execute(replace(incoming_message, text=section_command), completion, menu_handler)
            else:
                response = dispatcher.select_action(
                    incoming_message, token, action_store, completion, menu_handler,
                )
            if response.after_delivery is not None and original_context is not None:
                # Принятый фоновый ввод уже погашен, когда итог готов к доставке.
                completion_context = replace(original_context, pending_request_id=None)
            if (response.view is None and response.error is None and original_context is not None
                    and original_context.mode_id is not None):
                close_views = response.close_managed_views
                if not close_views:
                    try:
                        current = dispatcher.action_context(origin)
                        close_views = (current.app_user_id, current.mode_id, current.mode_revision) != (
                            original_context.app_user_id, original_context.mode_id, original_context.mode_revision,
                        )
                    except Exception:
                        LOGGER.exception("Could not validate managed view transition")
                if close_views:
                    response = replace(response, retire_views_after_delivery=lambda: action_store.retire_views(
                        origin, original_context, generation,
                    ))
            response = dispatcher.bind_response_actions(
                incoming_message, response, action_store, view_origin=original_context,
                generation=generation,
            )
        if menu_update_handler is not None:
            with self._menu_lock:
                # Первое сообщение личного чата также требует публикации:
                # до него у адаптера ещё нет снимка, даже если команда не меняла меню.
                addresses = list(dict.fromkeys(update.address for update in menu_updates))
                if (incoming_message.reply_address not in addresses
                        and incoming_message.reply_address not in self._published_menus):
                    addresses.append(incoming_message.reply_address)
                for address in addresses:
                    if defer_origin_menu and address == incoming_message.reply_address:
                        continue
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
    ai_config: AiConfig | None = None,
    messenger_dialogs: Mapping[str, str | None] | None = None,
) -> Iterator[ChannelMessageProcessor]:
    """Создаёт процессные входы и закрывает принадлежащие им клиенты при выходе.

    Args:
        database_path: Общий путь SQLite для событий и предметных операций.
        runtime: Процессные зависимости Obsidian либо `None`.
        obsidian_config: Профиль настроек включённой возможности.
        use_case_factory: Подменная фабрика article pipeline для тестов.
        analysis_use_case_factory: Подменная фабрика анализа для тестов.
        enabled_capabilities: Явно включённые ID.
        ai_config: Профиль ИИ либо None для чтения ASK_* при включении.
        messenger_dialogs: Публичные диалоги либо None для чтения настроек установки.

    Yields:
        Обработчик всех сообщений процесса с одним реестром.

    Raises:
        ValueError: Список включённых ID или профиль возможности некорректен.
    """
    try:
        validate_enabled_capabilities(enabled_capabilities)
        admin_id = AdminCapability.descriptor.capability_id
        admin_enabled = admin_id in enabled_capabilities
        trusted_admin_ids = load_admin_profile() if admin_enabled else frozenset()
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
        with (
            create_admin_runtime(enabled=admin_enabled) as admin,
            create_ai_runtime(enabled=AI_CAPABILITY_ID in enabled_capabilities, config=ai_config) as ai,
        ):
            capabilities = (entry,) if entry is not None else ()
            disabled = () if entry is not None else (
                obsidian_descriptor(),
            )
            if ai is not None:
                capabilities += (ai,)
            else:
                disabled += (AiCapability.descriptor,)
            if admin is not None:
                capabilities += (admin,)
            else:
                disabled += (AdminCapability.descriptor,)
            sources = ()
            if FamilyCapability.descriptor.capability_id in enabled_capabilities:
                def family_member_names(app_user_ids: tuple[int, ...]) -> Mapping[int, str | None]:
                    """Передаёт имена профилей, уже проверенных семейным сценарием."""
                    with connect_database(database_path) as connection:
                        profiles = SQLiteAppUserRepository(connection)
                        return {
                            app_user_id: profile.display_name
                            for app_user_id in app_user_ids
                            if (profile := profiles.get_by_id(app_user_id)) is not None
                        }

                def receipt_settings() -> tuple[str, str, str, float]:
                    """Загружает профиль только при запросе распознавания расхода."""
                    profile = ai_config or load_ai_config()
                    return profile.base_url, profile.api_key, profile.model, receipt_timeout_seconds()

                capabilities += (create_family_capability(database_path, family_member_names,
                                                          ReceiptRecognizer(receipt_settings)),)
                sources = (("family", create_shopping_notifications(database_path, family_member_names)),)
            else:
                disabled += (FamilyCapability.descriptor,)
            processor = ChannelMessageProcessor(
                database_path=database_path,
                messenger_dialogs=load_messenger_dialogs() if messenger_dialogs is None else messenger_dialogs,
                capabilities=capabilities,
                notification_sources=sources,
                disabled_capabilities=disabled,
                profile_access={admin_id: trusted_admin_ids.__contains__} if admin_enabled else None,
            )
            try:
                yield processor
            finally:
                processor.close_deferred_actions()
    finally:
        if runtime is not None:
            runtime.close()
