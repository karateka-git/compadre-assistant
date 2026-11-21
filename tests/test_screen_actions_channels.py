"""Один владелец экрана проходит настоящие входы и обе проекции каналов."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
import json
import logging
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiogram

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_channel_message_processor, create_user_identity_service
from compadre_assistant.capabilities.family.bootstrap import create_family_capability
from compadre_assistant.capabilities.ai.domain.errors import AiModelError
from compadre_assistant.capabilities.ai.domain.ocr import OcrResult
from compadre_assistant.capabilities.ai.domain.use_cases.extract_image_text import ExtractImageText
from compadre_assistant.capabilities.ai.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ai.presentation.handler import AiCapability
from compadre_assistant.capabilities.admin.domain.cost_report import CostSourceError, CostStatistics, GetGatewayCostReport
from compadre_assistant.capabilities.admin.presentation.handler import AdminCapability
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import ObsidianConfirmationResult, ObsidianConfirmationStatus
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import GitHubGatewayError
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_selection import GitHubAccountNotConnectedError, VaultSelectionStatus
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityHelp, CapabilityResult, ConfirmationQueryResult, HelpSection,
    IncomingResponse, PendingConfirmationQueryPort, ReplyAction, ResponseView, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.telegram.bot import (
    _create_telegram_menu_update_handler, _register_handlers, _send_telegram_notification,
)
from compadre_assistant.presentation.vk.bot import _create_vk_menu_update_handler, _handle_update, _send_vk_notification
from tests.test_incoming_boundary import FakeCapability
from tests.test_telegram_bot import FakeAiogram
from tests.test_vk_screen_actions import ProjectionClient
from tests.test_process_incoming_message import (
    FakeArticleUrlUseCase, FakeAnalysisUseCase, FakeVaultSelectionManager, FakeVaultSyncManager,
    FakeGitHubConnectionStarter, _test_vault, _proposal,
)


class NeutralScreenCapability(FakeCapability, SectionActionsQueryPort, PendingConfirmationQueryPort):
    """Описывает меню, страницы и выбор без ветвлений по названиям каналов."""

    def __init__(self):
        """Регистрирует команды и отдельные снимки доставленного выбора."""
        super().__init__("screens", "/screens")
        self._descriptor = replace(self.descriptor, help=CapabilityHelp((HelpSection(
            "Примеры", tuple(CapabilityCommand(command, (), "тестовый шаг")
                             for command in ("/objects", "/page", "/pick", "/menu", "/note")),
        ),)))
        self.items = [(f"id-{number}", f"Объект {number}") for number in range(1, 10)]
        self.delivered = {}
        self.selected = []
        self.pages = []
        self.section = (ReplyAction("Открыть выбор", "/objects", scope=ActionScope.SECTION),
                        ReplyAction("Показать текст", "/note", scope=ActionScope.SECTION), HELP_SECTION_ACTION)

    def section_actions(self, app_user, address, mode_revision, pending):
        """Возвращает тот же неизменяемый набор, что основной экран."""
        return self.section

    def list_pending_confirmations(self, context, origin):
        """Подтверждает отсутствие адресных подтверждений в тестовом сценарии."""
        return ConfirmationQueryResult("complete")

    def enter(self, context, event, arguments):
        """Открывает меню после штатного допуска ядром."""
        return self._menu()

    def _menu(self):
        """Собирает основной экран из единого описания раздела."""
        return CapabilityResult(IncomingResponse(
            text_content("Раздел примеров. Выбор: /objects. Текст: /note."),
            suggested_actions=self.section, close_managed_views=True,
        ))

    def handle(self, context, event):
        """Обрабатывает ручную команду, кнопку или номер одним маршрутом."""
        key = (context.app_user.id, event.reply_address, context.mode_revision)
        command, _, argument = event.text.partition(" ")
        if command == "/menu":
            return self._menu()
        if command == "/note":
            return CapabilityResult(IncomingResponse(text_content("Обычный результат без действий.")))
        if command in {"/objects", "/page"}:
            capacity = context.action_layout.selection_capacity(reserved_rows=2, reserved_buttons=2)
            size = min(6, capacity) if capacity else 6
            page = max(1, min(int(argument or "1"), (len(self.items) + size - 1) // size or 1))
            start = (page - 1) * size
            shown = tuple((index + 1, *item) for index, item in enumerate(self.items) if start <= index < start + size)
            self.pages.append((event.reply_address, page, size, shown))
            actions = [ReplyAction(f"{number}. {label}", f"/pick {identifier}", row=row,
                                   purpose=ActionPurpose.SELECTION)
                       for row, (number, identifier, label) in enumerate(shown)]
            if start + size < len(self.items):
                actions.append(ReplyAction("Дальше", f"/page {page + 1}", row=len(shown),
                                           purpose=ActionPurpose.NAVIGATION,
                                           destination=ScreenDestination("objects", page + 1)))
            actions.append(ReplyAction("Назад", "/menu", row=len(shown) + 1,
                                       purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("menu")))
            content = "\n".join(f"{number}. {label}" for number, _, label in shown)
            content += "\nВведи номер из этой страницы. Вернуться: /menu."
            return CapabilityResult(IncomingResponse(
                text_content(content), suggested_actions=(*actions, HELP_SECTION_ACTION),
                view=ResponseView("objects", update=command == "/page",
                                  unavailable=text_content("Открой /objects заново."),
                                  on_delivered=lambda: self.delivered.__setitem__(key, shown), cleanup_previous=True),
                command_menu_delivery=SectionMenuDelivery.PRESERVE,
            ))
        if command.isdecimal():
            number = int(command)
            shown = self.delivered.get(key, ())
            match = next((identifier for visible, identifier, _ in shown if visible == number), None)
            if match is None or number > len(self.items) or self.items[number - 1][0] != match:
                return CapabilityResult(IncomingResponse(text_content("Список изменился. Открой /objects.")))
            argument = match
        elif command != "/pick":
            return CapabilityResult(IncomingResponse(text_content("Открой /objects.")))
        if argument not in {identifier for identifier, _ in self.items}:
            return CapabilityResult(IncomingResponse(text_content("Объект недоступен. Открой /objects.")))
        self.selected.append((event.reply_address, argument))
        self.items = [item for item in self.items if item[0] != argument]
        self.delivered.pop(key, None)
        return CapabilityResult(IncomingResponse(text_content(f"Выбран {argument}. Открыть /objects."),
                                                 close_managed_views=True))


class ObservedVkClient(ProjectionClient):
    """Сохраняет фактический текст send/edit по ID сообщения."""

    def __init__(self):
        """Готовит журнал текста для сравнения обоих каналов."""
        super().__init__()
        self.texts = {}
        self.peer_ids = {}

    def send_message(self, **kwargs):
        """Запоминает текст подтверждённой публикации."""
        message_id = super().send_message(**kwargs)
        self.texts[message_id] = kwargs["text"]
        self.peer_ids[message_id] = kwargs["peer_id"]
        return message_id

    def edit_message(self, **kwargs):
        """Запоминает текст подтверждённого редактирования."""
        super().edit_message(**kwargs)
        self.texts[kwargs["conversation_message_id"]] = kwargs["text"]
        self.peer_ids[kwargs["conversation_message_id"]] = kwargs["peer_id"]


class ScreenActionsChannelsTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет одинаковый сценарий через Telegram SDK и VK Long Poll."""

    async def asyncSetUp(self):
        """Создаёт два независимых чата на актуальной временной схеме."""
        self.path = Path(self.enterContext(TemporaryDirectory(prefix="screen-actions-channels-"))) / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            for channel in ("telegram", "vk"):
                for chat in ("1001", "1002"):
                    service.register_with_name(IncomingIdentity(channel, chat, chat), display_name="Тест")
        self.owner = NeutralScreenCapability()
        self.open_channels({"telegram": "https://t.me/example_chat_bot", "vk": "https://vk.com/write-731"})

    def open_channels(self, dialogs, *, capabilities=None, profile_access=None, processors=None):
        """Подключает два локальных клиента к процессору с публичными адресами установки."""
        if processors is None:
            self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=capabilities or (self.owner,),
                                                    messenger_dialogs=dialogs, profile_access=profile_access)
            self.addCleanup(self.processor.close_deferred_actions)
            telegram_processor = self.processor
        else:
            telegram_processor, self.processor = processors
        self.logger = logging.getLogger("test.screen-actions.channels")
        self.ids = count(42)
        self.telegram = []
        self.telegram_cleared = []
        self.menus = []

        async def send(**kwargs):
            message_id = next(self.ids)
            self.telegram.append((message_id, kwargs))
            return SimpleNamespace(message_id=message_id)

        async def edit(**kwargs):
            self.telegram.append((kwargs["message_id"], kwargs))
            return SimpleNamespace(message_id=kwargs["message_id"])

        async def clear(**kwargs):
            self.telegram_cleared.append(kwargs["message_id"])

        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                   edit_message_text=AsyncMock(side_effect=edit),
                                   set_my_commands=AsyncMock(return_value=True),
                                   edit_message_reply_markup=AsyncMock(side_effect=clear))
        publish = _create_telegram_menu_update_handler(self.bot, loop=asyncio.get_running_loop(),
                                                       aiogram=aiogram, logger=self.logger)

        def menu_update(update):
            accepted = publish(update)
            if accepted:
                self.menus.append(update)
            return accepted

        sdk = FakeAiogram()
        _register_handlers(sdk.dispatcher, sdk, incoming_message_processor=telegram_processor,
                           logger=self.logger, menu_update_handler=menu_update)
        self.router = sdk.dispatcher.included_router
        self.vk = ObservedVkClient()
        self.vk_menu = _create_vk_menu_update_handler(self.vk, logger=self.logger)

    async def send(self, channel, text, *, chat="1001", menu=False, author=None, message_id=1):
        """Передаёт сообщение реальному входному обработчику нужного канала."""
        author = author or chat
        if channel == "telegram":
            async def answer(value, **kwargs):
                return await self.bot.send_message(chat_id=int(chat), text=value, **kwargs)

            await self.router.handler(SimpleNamespace(
                chat=SimpleNamespace(id=int(chat)), from_user=SimpleNamespace(id=int(author)),
                message_id=message_id, text=text, bot=self.bot, answer=answer,
            ))
        else:
            _handle_update({"type": "message_new", "object": {"message": {
                "peer_id": int(chat), "from_id": int(author), "id": message_id, "text": text,
                "payload": '{"menu":true}' if menu else None,
            }}}, incoming_message_processor=self.processor, client=self.vk, logger=self.logger,
                menu_update_handler=self.vk_menu)

    async def press(self, channel, token, message_id, *, chat="1001", author=None):
        """Передаёт callback с независимыми адресом и ID исходного сообщения."""
        author = author or chat
        if channel == "telegram":
            callback = SimpleNamespace(
                id="click", data=token, from_user=SimpleNamespace(id=int(author)),
                message=SimpleNamespace(chat=SimpleNamespace(id=int(chat)), message_id=message_id),
                bot=self.bot, answer=AsyncMock(),
            )
            await self.router.callback_handler(callback)
        else:
            _handle_update({"type": "message_event", "object": {
                "peer_id": int(chat), "user_id": int(author), "event_id": "click",
                "conversation_message_id": message_id, "payload": {"token": token},
            }}, incoming_message_processor=self.processor, client=self.vk, logger=self.logger,
                menu_update_handler=self.vk_menu)

    def last(self, channel):
        """Возвращает последний текст, inline-кнопки и ID для сравнения каналов."""
        if channel == "telegram":
            message_id, kwargs = self.telegram[-1]
            keyboard = kwargs.get("reply_markup")
            actions = [(button.text, button.callback_data or button.url)
                       for row in keyboard.inline_keyboard for button in row] if keyboard else []
            return kwargs["text"], actions, message_id
        event = next(item for item in reversed(self.vk.events) if item[0] in {"send", "edit"})
        text = self.vk.texts[event[1]]
        keyboard = event[2]
        actions = ([(button["action"]["label"], button["action"].get("link") or json.loads(button["action"]["payload"])["token"])
                    for row in keyboard["buttons"] for button in row]
                   if keyboard is not None and keyboard["inline"] else [])
        return text, actions, event[1]

    def bottom(self, chat=None):
        """Читает последнюю доставленную нижнюю клавиатуру VK."""
        keyboard = next(k for _, message_id, k in reversed(self.vk.events)
                        if k and not k["inline"] and (chat is None or str(self.vk.peer_ids[message_id]) == chat))
        return {button["action"]["label"]: button["action"] for row in keyboard["buttons"] for button in row}

    async def choose_section(self, channel, label, chat):
        """Нажимает реально показанное действие раздела по правилам канала."""
        if channel == "vk":
            self.assertIn(label, self.bottom(chat))
            await self.send(channel, label, chat=chat, menu=True)
        else:
            _, actions, message_id = self.last(channel)
            await self.press(channel, dict(actions)[label], message_id, chat=chat)

    def inline_action(self, channel, label):
        """Читает кнопку на последнем ответе с inline-клавиатурой в канале."""
        if channel == "telegram":
            _, actions, message_id = self.last(channel)
            return dict(actions)[label], message_id
        _, message_id, keyboard = next(event for event in reversed(self.vk.events)
                                       if event[2] and event[2]["inline"])
        actions = {button["action"]["label"]: button["action"] for row in keyboard["buttons"] for button in row}
        action = actions[label]
        return action.get("link") or json.loads(action["payload"])["token"], message_id

    async def test_admin_timezone_help_retry_cancel_expiry_and_access_in_both_channels(self):
        """Отчёт проходит ввод IANA, справку и возврат с повторной проверкой допуска."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                with connect_database(self.path) as connection:
                    owner = create_user_identity_service(connection).find_known_private_chat(channel, "1001")[0]
                allowed = {owner.id}
                reader = Mock()
                reader.read_period.return_value = CostStatistics(Decimal("0.10"))
                reader.read_request_count.return_value = 2
                report = GetGatewayCostReport(reader, currency="RUB", count_reader=reader,
                                              clock=lambda: datetime(2026, 1, 2, 18, tzinfo=UTC))
                self.open_channels({}, capabilities=(AdminCapability(report),), profile_access={"admin": allowed.__contains__})
                now = datetime.now(UTC)
                self.processor._pending_inputs._clock = lambda: now
                address = ReplyAddress(channel, "1001")

                async def choose(label):
                    await self.choose_section(channel, label, "1001")

                def report_text():
                    """Находит отчёт перед техническим сообщением обновления нижнего меню VK."""
                    values = list(self.vk.texts.values()) if channel == "vk" else [entry[1]["text"] for entry in self.telegram]
                    return next(text for text in reversed(values) if "Расходы ИИ по статистике" in text)

                def latest_text():
                    """Читает содержательный ответ перед уведомлением нижнего меню."""
                    values = list(self.vk.texts.values()) if channel == "vk" else [entry[1]["text"] for entry in self.telegram]
                    return next(text for text in reversed(values) if text not in {"Что сделать дальше?", "Выбери действие."})

                await self.send(channel, "/start")
                await choose("Администрирование")
                self.assertNotRegex(latest_text(), r"/(?:admin|llm_costs|llm_timezone|exit)\b")
                await choose("Показать расходы ИИ")
                await self.press(channel, *self.inline_action(channel, "Текущий месяц"))
                self.assertNotIn("Сегодня —", report_text())
                await choose("Часовой пояс")
                pending = self.processor._pending_inputs.get(address)
                self.assertEqual(pending.external_user_id, "1001")
                await choose("Помощь")
                help_text = latest_text()
                self.assertIn("/llm_timezone", help_text)
                self.assertNotIn("/llm_costs", help_text)
                await self.send(channel, "/help")
                self.assertEqual(help_text, latest_text())
                self.assertIs(pending, self.processor._pending_inputs.get(address))
                reader.reset_mock()
                for value in ("неизвестно", "Europe/Moscow DAY", " "):
                    await self.send(channel, value)
                    self.assertIn("Ожидание продолжается", latest_text())
                    self.assertNotIn("/llm_", latest_text())
                    self.assertIs(pending, self.processor._pending_inputs.get(address))
                reader.read_period.assert_not_called()
                await self.send(channel, "Europe/Moscow")
                self.assertIsNone(self.processor._pending_inputs.get(address))
                self.assertIn("Europe/Moscow", report_text())
                self.assertNotIn("Сегодня —", report_text())
                self.assertEqual(reader.read_period.call_count, 1)
                await self.press(channel, *self.inline_action(channel, "Сводка"))
                self.assertIn("Сегодня —", report_text())
                self.assertIn("Europe/Moscow", report_text())
                await choose("Часовой пояс")
                await choose("Отмена")
                self.assertIsNone(self.processor._pending_inputs.get(address))
                await choose("Вернуться" if channel == "telegram" else "Показать расходы ИИ")
                self.assertIn("Europe/Moscow", report_text())
                self.assertIn("Сегодня —", report_text())
                await choose("Часовой пояс")
                pending = self.processor._pending_inputs.get(address)
                now = pending.expires_at + timedelta(seconds=1)
                reader.reset_mock()
                await self.send(channel, "Asia/Tokyo")
                self.assertIn("Срок ожидания ввода истёк", latest_text())
                reader.read_period.assert_not_called()
                if channel == "telegram":
                    await self.press(channel, *self.inline_action(channel, "Начать заново"))
                else:
                    await choose("Часовой пояс")
                self.assertNotEqual(pending.request_id, self.processor._pending_inputs.get(address).request_id)
                await self.send(channel, "Asia/Tokyo")
                self.assertIn("Asia/Tokyo", report_text())
                self.assertIn("Сегодня —", report_text())
                await choose("Часовой пояс")
                reader.reset_mock()
                allowed.clear()
                await self.send(channel, "Europe/Moscow")
                self.assertIsNone(self.processor._pending_inputs.get(address))
                self.assertIn("нет доступа", latest_text())
                reader.read_period.assert_not_called()
                for command in ("/admin", "/llm_timezone", "/llm_costs"):
                    await self.send(channel, command, chat="1002")
                    self.assertIn("нет доступа", latest_text())
                await self.send(channel, "/help", chat="1002")
                self.assertNotIn("/llm_timezone", latest_text())
                allowed.add(owner.id)
                group = "-1001" if channel == "telegram" else "2000000101"
                for command in ("/admin", "/llm_timezone", "/llm_costs"):
                    await self.send(channel, command, chat=group, author="1001")
                    self.assertIn("Открой личный чат", latest_text())
                self.assertIsNone(self.processor._pending_inputs.get(ReplyAddress(channel, group)))
                reader.read_period.assert_not_called()

    async def test_admin_report_failure_and_unconfigured_source_keep_navigation(self):
        """Недоступная статистика оставляет повтор, пояс, помощь и выход без команд в тексте."""
        for channel in ("telegram", "vk"):
            for configured in (True, False):
                with self.subTest(channel=channel, configured=configured):
                    reader = Mock()
                    reader.read_period.side_effect = CostSourceError("private-detail")
                    report = GetGatewayCostReport(reader if configured else None, currency="RUB" if configured else None,
                                                  clock=lambda: datetime(2026, 1, 2, 18, tzinfo=UTC))
                    self.open_channels({}, capabilities=(AdminCapability(report),), profile_access={"admin": lambda _: True})
                    await self.send(channel, "/admin")
                    await self.choose_section(channel, "Показать расходы ИИ", "1001")
                    self.assertIn("временно недоступна" if configured else "не настроена", self.last(channel)[0])
                    self.assertNotIn("/llm_", self.last(channel)[0])
                    self.assertNotIn("private-detail", self.last(channel)[0])
                    await self.choose_section(channel, "Часовой пояс", "1001")
                    self.assertIn("Europe/Moscow", self.last(channel)[0])
                    await self.choose_section(channel, "Отмена", "1001")
                    await self.choose_section(channel, "Вернуться" if channel == "telegram" else "Показать расходы ИИ", "1001")
                    await self.choose_section(channel, "Помощь", "1001")
                    self.assertIn("/llm_costs", self.last(channel)[0])

    def enable_obsidian(self, channel):
        """Включает тестовую возможность для обоих зарегистрированных профилей."""
        with connect_database(self.path) as connection:
            service = create_user_identity_service(connection)
            users = [service.resolve_without_touch(IncomingIdentity(channel, chat, chat)) for chat in ("1001", "1002")]
            for user in users:
                SQLiteUserCapabilityPreferenceRepository(connection).upsert(UserCapabilityPreference(user.id, "obsidian", True))
        return users[0]

    async def test_obsidian_vault_article_and_reanalysis_actions_in_both_channels(self):
        """Хранилище и статьи продолжаются кнопками в личном чате и группе."""
        for channel, chat in (("telegram", "1001"), ("vk", "1001"),
                              ("telegram", "-1001"), ("vk", "2000000101")):
            with self.subTest(channel=channel, chat=chat):
                user = self.enable_obsidian(channel)
                selection, sync = FakeVaultSelectionManager(), FakeVaultSyncManager()
                articles, analysis = FakeArticleUrlUseCase(), FakeAnalysisUseCase()
                confirmations = Mock()
                confirmations.list_pending.return_value = ()
                confirmations.get_active_for_article.return_value = None
                handler = ObsidianMessageHandler(
                    article_url_use_case=articles, article_analysis_use_case=analysis,
                    vault_selection_manager=selection, vault_sync_manager=sync,
                    obsidian_confirmation_service=confirmations, github_connection_starter=FakeGitHubConnectionStarter(),
                )
                self.open_channels({}, capabilities=(handler,))
                private = chat == "1001"

                async def choose(label):
                    if private:
                        await self.choose_section(channel, label, chat)
                    else:
                        await self.press(channel, *self.inline_action(channel, label), chat=chat, author="1001")

                async def send(text):
                    await self.send(channel, text, chat=chat, author="1001")

                await send("/start")
                await choose("Статьи в Obsidian")
                await choose("Помощь")
                self.assertNotIn("/vault_sync", self.last(channel)[0])
                await send("https://github.com/owner/repository vault/path")
                self.assertEqual(selection.calls[-1], (user.id, "https://github.com/owner/repository", "vault/path"))
                self.assertNotIn("/vault", self.last(channel)[0])
                await choose("Проверить состояние")
                await choose("Помощь")
                self.assertIn("/vault_sync", self.last(channel)[0])
                await choose("Синхронизировать")
                sync.sync = Mock(side_effect=GitHubGatewayError("offline"))
                await choose("Синхронизировать")
                self.assertIn("Не удалось", self.last(channel)[0])
                self.assertNotIn("/vault_sync", self.last(channel)[0])
                sync.sync = FakeVaultSyncManager().sync
                await choose("Синхронизировать")
                await send("https://example.com/article скопированный пример /confirm obsidian proposal 1 1 yes")
                self.assertIn("Новый анализ", self.last(channel)[0])
                self.assertEqual(len(analysis.commands), 1)
                confirmations.confirm.assert_not_called()
                await choose("Повторить анализ")
                pending = self.processor._pending_inputs.get(ReplyAddress(channel, chat))
                self.assertEqual(pending.external_user_id, "1001")
                await choose("Помощь")
                help_text = self.last(channel)[0]
                self.assertIn("/reanalyze", help_text)
                self.assertNotIn("/vault_sync", help_text)
                await send("/help")
                self.assertEqual(help_text, self.last(channel)[0])
                self.assertIs(pending, self.processor._pending_inputs.get(ReplyAddress(channel, chat)))
                if not private:
                    for text in ("/obsidian", "/reanalyze", "7", "/cancel"):
                        await self.send(channel, text, chat=chat, author="1002")
                        self.assertIs(pending, self.processor._pending_inputs.get(ReplyAddress(channel, chat)))
                await send("не число")
                self.assertNotIn("/cancel", self.last(channel)[0])
                await send("7")
                self.assertEqual(len(analysis.commands), 2)
                selection.status = VaultSelectionStatus.REPLACEMENT_CONFIRMATION_REQUIRED
                select = selection.select

                def request_replacement(**kwargs):
                    """Возвращает созданное тестовым менеджером адресное ожидание."""
                    result = select(**kwargs)
                    return replace(result, confirmation=selection.get_pending(kwargs["app_user_id"], kwargs["origin"]))

                selection.select = request_replacement
                await send("https://github.com/owner/other")
                cancel_token, replacement_message = self.inline_action(channel, "Сохранить подключение")
                await choose("Помощь")
                self.assertIn("/confirm obsidian replace", self.last(channel)[0])
                await self.press(channel, cancel_token, replacement_message, chat=chat, author="1001")
                self.assertIn("Замена отменена", self.last(channel)[0])
                await send("https://github.com/owner/other")
                await self.press(channel, *self.inline_action(channel, "Подтвердить замену"), chat=chat, author="1001")
                self.assertIn("Хранилище заменено", self.last(channel)[0])
                await choose("Отключить хранилище")
                token, message_id = self.inline_action(channel, "Подтвердить отключение")
                if not private:
                    await self.press(channel, token, message_id, chat=chat, author="1002")
                    self.assertIsNotNone(selection.selected)
                await send("/help")
                self.assertIn("/confirm obsidian disconnect", self.last(channel)[0])
                await self.press(channel, token, message_id, chat=chat, author="1001")
                self.assertIsNone(selection.selected)
                await choose("Помощь")
                self.assertNotIn("/reanalyze", self.last(channel)[0])

    async def test_obsidian_authorization_links_and_failed_snapshot_remain_recoverable(self):
        """Код GitHub и ссылки работают рядом со справкой, неполный список допускает перечитывание."""
        for channel, chat in (("telegram", "1001"), ("vk", "1001"),
                              ("telegram", "-1001"), ("vk", "2000000101")):
            with self.subTest(channel=channel, chat=chat):
                self.enable_obsidian(channel)
                starter = FakeGitHubConnectionStarter()
                selection = FakeVaultSelectionManager(error=GitHubAccountNotConnectedError())
                service = Mock()
                service.list_pending.side_effect = RuntimeError("temporarily unavailable")
                handler = ObsidianMessageHandler(article_url_use_case=FakeArticleUrlUseCase(),
                                                 vault_selection_manager=selection, github_connection_starter=starter,
                                                 obsidian_confirmation_service=service)
                self.open_channels({}, capabilities=(handler,))

                async def choose(label):
                    if chat == "1001":
                        await self.choose_section(channel, label, chat)
                    else:
                        await self.press(channel, *self.inline_action(channel, label), chat=chat, author="1001")

                with self.assertLogs("compadre_assistant", level="ERROR"):
                    await self.send(channel, "/start", chat=chat, author="1001")
                    await choose("Статьи в Obsidian")
                    await choose("Подтверждения")
                    self.assertIn("Не удалось получить полный список", self.last(channel)[0])
                    await choose("Подтверждения")
                    self.assertIn("Не удалось получить полный список", self.last(channel)[0])
                service.list_pending.side_effect = None
                service.list_pending.return_value = ()
                await self.send(channel, "https://github.com/owner/repository vault/path", chat=chat, author="1001")
                self.assertEqual(self.inline_action(channel, "Авторизовать GitHub")[0], "https://github.com/login/device")
                texts = "\n".join(self.vk.texts.values()) if channel == "vk" else self.last(channel)[0]
                self.assertIn("ABCD-EFGH", texts)
                self.assertNotIn("device-secret", texts)
                await choose("Помощь")
                self.assertNotIn("/vault_sync", self.last(channel)[0])

    async def test_obsidian_saved_proposal_retry_checks_author_version_and_access(self):
        """Сохранённое решение переживает сбой, помощь и обновление версии в обоих каналах."""
        for channel, chat in (("telegram", "1001"), ("vk", "1001"),
                              ("telegram", "-1001"), ("vk", "2000000101")):
            with self.subTest(channel=channel, chat=chat):
                user = self.enable_obsidian(channel)
                proposal = replace(_proposal(), app_user_id=user.id, origin=PendingOrigin(channel, chat),
                                   reasoning="Данные статьи: /exit и /confirm obsidian proposal 1 1 yes")
                service = Mock()
                current = [proposal]
                service.list_pending.side_effect = lambda user_id, origin: tuple(
                    item for item in current if item.app_user_id == user_id and item.origin == origin)
                service.confirm.side_effect = lambda *args, **kwargs: ObsidianConfirmationResult(
                    ObsidianConfirmationStatus.FAILED, current[0], GitHubGatewayError("offline"))
                selection = FakeVaultSelectionManager(selected=_test_vault(user.id))
                handler = ObsidianMessageHandler(article_url_use_case=FakeArticleUrlUseCase(),
                                                 vault_selection_manager=selection, obsidian_confirmation_service=service)
                self.open_channels({}, capabilities=(handler,))

                async def choose(label):
                    if chat == "1001":
                        await self.choose_section(channel, label, chat)
                    else:
                        await self.press(channel, *self.inline_action(channel, label), chat=chat, author="1001")

                await self.send(channel, "/start", chat=chat, author="1001")
                await choose("Статьи в Obsidian")
                self.assertIn(proposal.reasoning, "\n".join(self.vk.texts.values()) if channel == "vk" else self.last(channel)[0])
                label = "Завершить разбор №7"
                token, message_id = self.inline_action(channel, label)
                await self.press(channel, token, message_id, chat=chat, author="1002")
                service.confirm.assert_not_called()
                await self.send(channel, "/help", chat=chat, author="1001")
                self.assertIn("/confirm obsidian proposal 1 1 yes", self.last(channel)[0])
                await self.press(channel, token, message_id, chat=chat, author="1001")
                self.assertIn("Предложение и текст статьи сохранены", self.last(channel)[0])
                self.assertNotIn("/confirm", self.last(channel)[0])
                self.assertEqual(service.confirm.call_count, 1)
                await choose("Подтверждения")
                stale_token, stale_message = self.inline_action(channel, label)
                current[0] = replace(proposal, version=proposal.version + 1)
                await self.press(channel, stale_token, stale_message, chat=chat, author="1001")
                self.assertEqual(service.confirm.call_count, 1)
                # Повторное чтение возвращает новую версию, не исполняя старую команду.
                await self.send(channel, "/confirm", chat=chat, author="1001")
                token, message_id = self.inline_action(channel, label)
                service.confirm.side_effect = None
                service.confirm.return_value = ObsidianConfirmationResult(ObsidianConfirmationStatus.APPLIED, current[0])
                await self.press(channel, token, message_id, chat=chat, author="1001")
                self.assertEqual(service.confirm.call_args.kwargs["version"], 2)
                self.assertEqual(service.confirm.call_count, 2)
                with connect_database(self.path) as connection:
                    SQLiteUserCapabilityPreferenceRepository(connection).upsert(UserCapabilityPreference(user.id, "obsidian", False))
                await self.press(channel, token, message_id, chat=chat, author="1001")
                self.assertEqual(service.confirm.call_count, 2)

    async def test_shopping_digest_between_telegram_and_vk(self):
        """Два участника проходят путь от добавления товаров до нажатия полученной кнопки."""
        processors = {channel: self.enterContext(create_channel_message_processor(
            database_path=self.path, enabled_capabilities=("family",),
        )) for channel in ("telegram", "vk")}
        self.open_channels({}, processors=(processors["telegram"], processors["vk"]))
        loop = asyncio.get_running_loop()

        for sender, recipient, chat in (("telegram", "vk", "1001"), ("vk", "telegram", "1002")):
            with self.subTest(sender=sender, recipient=recipient):
                await self.send(sender, "/family", chat=chat)
                await self.send(sender, "/family_create", chat=chat)
                await self.send(sender, "Дом", chat=chat)
                await self.send(sender, "/family_invite create", chat=chat)
                invitation = re.search(r"Код: (?:<code>)?([A-Z2-9-]+)", self.last(sender)[0])
                self.assertIsNotNone(invitation)
                await self.send(recipient, "/family", chat=chat)
                await self.send(recipient, "/family_join", chat=chat)
                await self.send(recipient, invitation[1], chat=chat)
                await self.send(recipient, "/settings", chat=chat)
                await self.send(sender, "/family_shopping", chat=chat)
                for first in (1, 11):
                    names = "\n".join(f"Продукт {n:02}" for n in range(first, first + 10))
                    await self.send(sender, f"/add {names}", chat=chat, message_id=10 + first)

                def send_digest(address, response):
                    self.assertEqual(address, ReplyAddress(recipient, chat))
                    if recipient == "telegram":
                        task = _send_telegram_notification(self.bot, aiogram, address, response)
                        asyncio.run_coroutine_threadsafe(task, loop).result(timeout=5)
                    else:
                        _send_vk_notification(self.vk, address, response)

                send_once = Mock(side_effect=send_digest)
                due = datetime.now(UTC) + timedelta(minutes=3)
                for channel in (sender, recipient, recipient):
                    await asyncio.to_thread(processors[channel].dispatch_notifications, channel, send_once, now=due)
                send_once.assert_called_once()
                text, actions, message_id = self.last(recipient)
                self.assertIn("Новых позиций: 20.", text)
                self.assertIn("Продукт 01, Продукт 02, Продукт 03, Продукт 04, Продукт 05 и ещё 15", text)
                self.assertEqual(text.count("Продукт"), 5)
                token = dict(actions)["Открыть покупки"]
                await self.send(recipient, "/help", chat=chat)
                self.assertIn("/connect", self.last(recipient)[0])
                await self.press(recipient, token, message_id, chat=chat)
                self.assertIn("10. Продукт 10", self.last(recipient)[0])
                await self.send(recipient, "/family_shopping page 2", chat=chat)
                self.assertIn("20. Продукт 20", self.last(recipient)[0])

    async def test_family_buttons_help_and_membership_decisions_in_both_channels(self):
        """Проходит семью без команд, сохраняя исходные кнопки при открытии справки."""
        self.open_channels({}, capabilities=(create_family_capability(self.path, lambda ids: {
            identifier: f"Участник {identifier}" for identifier in ids
        }),))
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                def check_reply(chat):
                    text, actions, _ = self.last(channel)
                    labels = [label for label, _ in actions]
                    if channel == "vk":
                        labels.extend(self.bottom(chat))
                    # Исключаем закрывающие HTML-теги Telegram из пользовательского текста.
                    for visible in (text, *labels):
                        self.assertNotRegex(visible, r"(?<![<\w])/[a-z][a-z_]*\b")

                async def section(label, chat="1001"):
                    await self.choose_section(channel, label, chat)
                    check_reply(chat)

                async def inline(label, chat="1001"):
                    token, message_id = self.inline_action(channel, label)
                    await self.press(channel, token, message_id, chat=chat)
                    check_reply(chat)

                await self.send(channel, "/start")
                await section("«Семья»")
                await section("Создать семью")
                await self.send(channel, "Дом")
                await section("Пригласить")
                create_token, invitation_id = self.inline_action(channel, "Создать код")
                await self.choose_section(channel, "Помощь", "1001")
                help_text = self.last(channel)[0]
                self.assertIn("/family_invite", help_text)
                self.assertNotIn("/family_remove", help_text)
                await self.send(channel, "/help")
                self.assertEqual(help_text, self.last(channel)[0])
                await self.press(channel, create_token, invitation_id)
                code = re.search(r"Код: (?:<code>)?([A-Z2-9-]+)", self.last(channel)[0])[1]
                back_token, code_id = self.inline_action(channel, "Назад")
                await self.send(channel, "/start", chat="1002")
                await section("«Семья»", "1002")
                await section("Вступить по коду", "1002")
                await self.send(channel, "неверный код", chat="1002")
                self.assertNotIn("/cancel", self.last(channel)[0])
                await self.send(channel, code, chat="1002")
                self.assertIn("Ты вступил в семью", self.last(channel)[0])
                # Вернуться к ранее выданному коду и его кнопкам после действий другого профиля.
                await self.press(channel, back_token, code_id)
                await inline("Назад")
                await section("Участники")
                if channel == "telegram":
                    labels = dict(self.last(channel)[1])
                else:
                    keyboard = next(event[2] for event in reversed(self.vk.events) if event[2] and event[2]["inline"])
                    labels = {button["action"]["label"] for row in keyboard["buttons"] for button in row}
                await inline(next(label for label in labels if label.startswith("2. ")))
                remove_token, card_id = self.inline_action(channel, "Исключить")
                await self.choose_section(channel, "Помощь", "1001")
                self.assertIn("/family_remove", self.last(channel)[0])
                self.assertNotIn("/family_invite", self.last(channel)[0])
                await self.press(channel, remove_token, card_id)
                self.assertIn("Подтверждение действует", self.last(channel)[0])
                approve_token, confirmation_id = self.inline_action(channel, "Исключить")
                check_reply("1001")
                await self.choose_section(channel, "Помощь", "1001")
                help_text = self.last(channel)[0]
                for decision in ("yes", "no"):
                    self.assertRegex(help_text, rf"/confirm family remove \d+ \d+ {decision}")
                self.assertNotIn("/confirm family close", help_text)
                await self.send(channel, "/help")
                self.assertEqual(help_text, self.last(channel)[0])
                await self.press(channel, approve_token, confirmation_id, author="1002")
                self.assertNotIn("Участник исключён", self.last(channel)[0])
                await self.press(channel, approve_token, confirmation_id)
                self.assertIn("Участник исключён", self.last(channel)[0])
                check_reply("1001")
                await self.choose_section(channel, "Помощь", "1001")
                self.assertNotIn("/confirm family remove", self.last(channel)[0])
                await section("Участники")
                await inline("Закрыть семью")
                await inline("Отменить")
                await section("Участники")
                await inline("Закрыть семью")
                await inline("Закрыть семью")
                self.assertIn("Семья закрыта", self.last(channel)[0])

    async def test_profile_name_and_capabilities_follow_settings_screens(self):
        """Меняет имя, выключает возможность и включает её в обоих каналах."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                chat = "1001"
                await self.send(channel, "/start", chat=chat)
                await self.choose_section(channel, "Настройки", chat)
                await self.choose_section(channel, "Изменить имя", chat)
                await self.choose_section(channel, "Помощь", chat)
                help_text = self.last(channel)[0]
                self.assertIn("/name", help_text)
                self.assertNotIn("/connect", help_text)
                await self.send(channel, "https://example.com/invalid-name", chat=chat)
                self.assertIn("Имя не принято", self.last(channel)[0])
                await self.send(channel, "Новое имя", chat=chat)
                self.assertIn("Новое имя", self.last(channel)[0])
                self.assertNotRegex(self.last(channel)[0], r"/[a-z_]+")
                await self.choose_section(channel, "Личные возможности", chat)
                self.assertNotRegex(self.last(channel)[0], r"/[a-z_]+")
                await self.press(channel, *self.inline_action(channel, "Выключить возможность"), chat=chat)
                self.assertNotRegex(self.last(channel)[0], r"/[a-z_]+")
                await self.press(channel, *self.inline_action(channel, "Выключить screens"), chat=chat)
                self.assertIn("выключена", self.last(channel)[0])
                await self.choose_section(channel, "Помощь", chat)
                help_text = self.last(channel)[0]
                self.assertIn("/enable", help_text)
                self.assertNotIn("/disable", help_text)
                self.assertNotIn("/name", help_text)
                await self.choose_section(channel, "Личные возможности", chat)
                await self.press(channel, *self.inline_action(channel, "Включить возможность"), chat=chat)
                await self.press(channel, *self.inline_action(channel, "Включить screens"), chat=chat)
                self.assertIn("включена", self.last(channel)[0])
                await self.choose_section(channel, "Назад", chat)
                self.assertIn("Новое имя", self.last(channel)[0])

    async def test_private_registration_and_contextual_help_from_buttons(self):
        """Регистрирует профиль кнопками, сохраняя ожидаемый ввод при справке."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                chat = "3003"
                await self.send(channel, "/start", chat=chat)
                self.assertNotRegex(self.last(channel)[0], r"/[a-z_]+")
                await self.choose_section(channel, "Помощь", chat)
                self.assertIn("/register", self.last(channel)[0])
                self.assertIn("/link", self.last(channel)[0])
                await self.choose_section(channel, "Создать профиль", chat)
                prompt, actions, source = self.last(channel)
                self.assertIn("10 минут", prompt)
                self.assertNotRegex(prompt, r"/[a-z_]+")
                await self.choose_section(channel, "Помощь", chat)
                help_text = self.last(channel)[0]
                self.assertIn("/cancel", help_text)
                self.assertNotIn("/register", help_text)
                self.assertNotIn("/link", help_text)
                await self.send(channel, "/help", chat=chat)
                self.assertEqual(self.last(channel)[0], help_text)
                if channel == "telegram":
                    self.assertNotIn(source, self.telegram_cleared)
                    await self.press(channel, dict(actions)["Отмена"], source, chat=chat)
                else:
                    await self.choose_section(channel, "Отмена", chat)
                self.assertIn("отменён", self.last(channel)[0])
                await self.choose_section(channel, "Создать профиль", chat)
                await self.choose_section(channel, "Помощь", chat)
                await self.send(channel, "Новый профиль", chat=chat)
                self.assertIn("Регистрация завершена", self.last(channel)[0])
                self.assertNotRegex(self.last(channel)[0], r"/[a-z_]+")
                await self.choose_section(channel, "Помощь", chat)
                self.assertIn("/settings", self.last(channel)[0])
                self.assertNotIn("/register", self.last(channel)[0])
                if channel == "telegram":
                    for call in self.bot.set_my_commands.await_args_list:
                        self.assertEqual([item.command for item in call.kwargs["commands"]], ["help"])

    async def test_group_registration_is_bound_to_the_initiating_author(self):
        """В группе другой участник не отменяет ввод и не подставляет имя."""
        for channel, chat in (("telegram", "-413"), ("vk", "2000000413")):
            with self.subTest(channel=channel):
                author, stranger = "3003", "3004"
                await self.send(channel, "/start", chat=chat, author=author)
                text, actions, source = self.last(channel)
                self.assertNotRegex(text, r"/[a-z_]+")
                self.assertEqual(set(dict(actions)), {"Создать профиль", "Помощь"})
                await self.press(channel, dict(actions)["Создать профиль"], source, chat=chat, author=stranger)
                self.assertIn("больше недоступна", self.last(channel)[0])
                await self.send(channel, "/link чужой-код", chat=chat, author=stranger)
                self.assertIn("личный чат", self.last(channel)[0])
                await self.press(channel, dict(actions)["Создать профиль"], source, chat=chat, author=author)
                _, actions, source = self.last(channel)
                await self.press(channel, dict(actions)["Отмена"], source, chat=chat, author=stranger)
                self.assertIn("больше недоступна", self.last(channel)[0])
                await self.send(channel, "/cancel", chat=chat, author=stranger)
                self.assertIn("нечего отменять", self.last(channel)[0])
                await self.send(channel, "Чужое имя", chat=chat, author=stranger)
                self.assertNotIn("Регистрация завершена", self.last(channel)[0])
                await self.send(channel, "/register", chat=chat, author=stranger)
                self.assertIn("другой участник", self.last(channel)[0])
                await self.press(channel, dict(actions)["Помощь"], source, chat=chat, author=author)
                self.assertIn("/cancel", self.last(channel)[0])
                self.assertNotIn("/link", self.last(channel)[0])
                await self.send(channel, "Моё имя", chat=chat, author=author)
                text, actions, _ = self.last(channel)
                self.assertIn("Моё имя", text)
                self.assertIn("Регистрация завершена", text)
                self.assertIn("Настройки", dict(actions))
                self.assertNotIn("У меня есть код", dict(actions))

    async def test_messenger_transfer_in_both_directions(self):
        """Переносит зарегистрированный аккаунт в оба канала после отказа и справки."""
        for source, target, title, chat, url in (
            ("telegram", "vk", "VK", "1001", "https://vk.com/write-731"),
            ("vk", "telegram", "Telegram", "1002", "https://t.me/example_chat_bot"),
        ):
            with self.subTest(target=target):
                await self.send(source, "/start", chat=chat)
                await self.choose_section(source, "Настройки", chat)
                await self.choose_section(source, "Мессенджеры", chat)
                self.assertIn(f"{title} — не привязан", self.last(source)[0])
                await self.choose_section(source, f"Привязать {title}", chat)
                prompt, buttons, _ = self.last(source)
                code = re.search(r"Код привязки: (?:<code>)?([\w-]+)", prompt).group(1)
                self.assertEqual(dict(buttons)["Открыть бота"], url)
                self.assertNotIn("/link", prompt)
                await self.send(target, "/start", chat=chat)
                await self.choose_section(target, "Настройки", chat)
                await self.choose_section(target, "Мессенджеры", chat)
                await self.choose_section(target, "Помощь", chat)
                self.assertIn("/connect", self.last(target)[0])
                self.assertIn("/link", self.last(target)[0])
                self.assertNotIn("/name", self.last(target)[0])
                await self.choose_section(target, f"Привязать {title} к другому профилю", chat)
                await self.choose_section(target, "Помощь", chat)
                self.assertIn("/link", self.last(target)[0])
                self.assertIn("/cancel", self.last(target)[0])
                self.assertNotIn("/connect", self.last(target)[0])
                await self.send(target, "неверный-код", chat=chat)
                self.assertIn("Код привязки не принят", self.last(target)[0])
                self.assertNotRegex(self.last(target)[0], r"/[a-z_]+")
                await self.send(target, code, chat=chat)
                reject = self.inline_action(target, "Отменить перенос")
                self.assertNotIn("/confirm", self.last(target)[0])
                await self.press(target, *self.inline_action(target, "Помощь"), chat=chat)
                self.assertRegex(self.last(target)[0], r"/confirm core identity \d+ \d+ yes")
                await self.press(target, *reject, chat=chat)
                self.assertIn("оставляю текущую привязку", self.last(target)[0])
                await self.choose_section(target, f"Привязать {title} к другому профилю", chat)
                await self.send(target, code, chat=chat)
                accept = self.inline_action(target, "Перенести аккаунт")
                await self.press(target, *accept, chat=chat)
                self.assertIn("перепривязал", self.last(target)[0])
                await self.press(target, *accept, chat=chat)
                self.assertIn("больше недоступна", self.last(target)[0])
                await self.choose_section(source, "Назад", chat)
                await self.choose_section(source, "Мессенджеры", chat)
                self.assertIn(f"{title} — привязан", self.last(source)[0])
                if source == "vk":
                    self.assertIn("Открыть бота", self.bottom(chat))
                delivered_url = (self.bottom(chat)["Открыть бота"]["link"] if source == "vk"
                                 else dict(self.last(source)[1])["Открыть бота"])
                self.assertEqual(delivered_url, url)

    async def test_group_settings_name_and_capability_input_belong_to_the_author(self):
        """Кнопки группы меняют профиль автора и сохраняют личный характер привязки."""
        for channel, chat in (("telegram", "-413"), ("vk", "2000000413")):
            with self.subTest(channel=channel):
                author, stranger = "1001", "1002"
                await self.send(channel, "/start", chat=chat, author=author)
                await self.press(channel, *self.inline_action(channel, "Настройки"), chat=chat, author=author)
                self.assertNotIn("Мессенджеры", dict(self.last(channel)[1]))
                change_name = self.inline_action(channel, "Изменить имя")
                await self.press(channel, *change_name, chat=chat, author=stranger)
                self.assertIn("больше недоступна", self.last(channel)[0])
                await self.press(channel, *change_name, chat=chat, author=author)
                cancel = self.inline_action(channel, "Отмена")
                await self.press(channel, *cancel, chat=chat, author=stranger)
                self.assertIn("больше недоступна", self.last(channel)[0])
                await self.send(channel, "/settings", chat=chat, author=stranger)
                await self.send(channel, "/name", chat=chat, author=stranger)
                self.assertIn("другой участник", self.last(channel)[0])
                await self.send(channel, "Чужое имя", chat=chat, author=stranger)
                self.assertNotIn("буду обращаться", self.last(channel)[0])
                await self.send(channel, "Имя автора", chat=chat, author=author)
                self.assertIn("Имя автора", self.last(channel)[0])
                await self.press(channel, *self.inline_action(channel, "Личные возможности"), chat=chat, author=author)
                await self.press(channel, *self.inline_action(channel, "Выключить возможность"), chat=chat, author=author)
                self.assertIn("ID из списка", self.last(channel)[0])
                await self.send(channel, "screens", chat=chat, author=stranger)
                self.assertNotIn("выключена", self.last(channel)[0])
                await self.send(channel, "screens", chat=chat, author=author)
                self.assertIn("выключена", self.last(channel)[0])
                await self.press(channel, *self.inline_action(channel, "Личные возможности"), chat=chat, author=author)
                await self.press(channel, *self.inline_action(channel, "Включить возможность"), chat=chat, author=author)
                await self.send(channel, "screens", chat=chat, author=author)
                self.assertIn("включена", self.last(channel)[0])

    async def test_expired_name_can_be_restarted_from_the_current_reply(self):
        """После срока Telegram и VK дают продолжить из новой клавиатуры."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                self.open_channels({})
                await self.send(channel, "/start")
                await self.choose_section(channel, "Настройки", "1001")
                await self.choose_section(channel, "Изменить имя", "1001")
                store = self.processor._pending_inputs
                address = next(address for address in store.addresses() if address.channel == channel)
                expires = store.get(address).expires_at
                with patch.object(store, "_clock", return_value=expires):
                    if channel == "telegram":
                        self.processor.reconcile_menus(self.menus.append)
                    else:
                        self.processor.reconcile_menus(
                            self.vk_menu, notify_expiry=self.vk_menu.send_expiry,
                        )
                    if channel == "telegram":
                        await self.send(channel, "Просроченное имя")
                    self.assertIn("Срок ожидания ввода истёк", self.last(channel)[0])
                    self.assertNotRegex(self.last(channel)[0], r"/[a-z_]+")
                    if channel == "telegram":
                        await self.choose_section(channel, "Начать заново", "1001")
                    else:
                        await self.choose_section(channel, "Изменить имя", "1001")
                    self.assertEqual(store.get(address).expires_at, expires + timedelta(minutes=10))
                    await self.send(channel, "Актуальное имя")
                    self.assertIn("Актуальное имя", self.last(channel)[0])

    async def test_revoked_selection_recovers_without_reopening_the_disabled_mode(self):
        """Устаревший выбор после отзыва доступа возвращает рабочие общие кнопки."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                chat = "1001"
                await self.send(channel, "/start", chat=chat)
                await self.choose_section(channel, self.owner.descriptor.entry.description, chat)
                await self.choose_section(channel, "Открыть выбор", chat)
                selection = self.inline_action(channel, "1. Объект 1")
                with connect_database(self.path) as connection:
                    identity = IncomingIdentity(channel, chat, chat)
                    user = create_user_identity_service(connection).resolve_without_touch(identity)
                    SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                        UserCapabilityPreference(user.id, self.owner.descriptor.capability_id, False),
                    )
                await self.press(channel, *selection, chat=chat)
                text, actions, _ = self.last(channel)
                self.assertIn("больше недоступна", text)
                self.assertNotRegex(text, r"/[a-z_]+")
                labels = set(dict(actions)) if channel == "telegram" else set(self.bottom(chat))
                self.assertTrue({"Настройки", "Помощь"} <= labels)
                self.assertNotIn(self.owner.descriptor.entry.description, labels)
                await self.choose_section(channel, "Настройки", chat)
                self.assertIn("Настройки", self.last(channel)[0])
                self.assertEqual(self.owner.selected, [])

    async def test_ai_group_question_actions_and_waiting_input_belong_to_the_author(self):
        """В группе вопрос, помощь и отмена доступны только автору исходного действия."""
        for channel, chat in (("telegram", "-1001"), ("vk", "2000000101")):
            with self.subTest(channel=channel):
                model = Mock()
                model.answer.return_value = "Ответ на групповой вопрос"
                self.open_channels({}, capabilities=(AiCapability(AskQuestion(model=model, max_question_chars=100), Mock()),))

                async def press(label, *, author="1001"):
                    await self.press(channel, *self.inline_action(channel, label), chat=chat, author=author)

                await self.send(channel, "/start", chat=chat, author="1001")
                await press("Режим ИИ")
                ask_token, entry_id = self.inline_action(channel, "Задать вопрос")
                await self.press(channel, ask_token, entry_id, chat=chat, author="1002")
                self.assertIn("больше недоступна", self.last(channel)[0])
                await self.press(channel, ask_token, entry_id, chat=chat, author="1001")
                help_token, prompt_id = self.inline_action(channel, "Помощь")
                address = ReplyAddress(channel, chat)
                pending = self.processor._pending_inputs.get(address)
                self.assertEqual(pending.external_user_id, "1001")
                for text in ("/cancel", "чужой вопрос", "/ai", "/ask"):
                    await self.send(channel, text, chat=chat, author="1002")
                    self.assertIs(self.processor._pending_inputs.get(address), pending)
                await self.press(channel, help_token, prompt_id, chat=chat, author="1001")
                self.assertIn("/ask", self.last(channel)[0])
                self.assertNotIn("/get_text_from_image", self.last(channel)[0])
                help_text = self.last(channel)[0]
                await self.send(channel, "/help", chat=chat, author="1001")
                self.assertEqual(self.last(channel)[0], help_text)
                self.assertIs(self.processor._pending_inputs.get(address), pending)
                await self.send(channel, "Мой вопрос", chat=chat, author="1001")
                async with asyncio.timeout(5):
                    while "Ответ на групповой вопрос" not in self.last(channel)[0]:
                        await asyncio.sleep(0.01)
                self.assertIn("Извлечь текст с фото", dict(self.last(channel)[1]))
                model.answer.assert_called_once()
                self.assertEqual(model.answer.call_args.kwargs["question"], "Мой вопрос")

    async def test_ai_photo_retry_cancellation_and_help_in_private_and_group_chats(self):
        """Фото проходит кнопочный ввод, повтор и отмену в обоих видах чата и каналах."""
        for channel, chat in (("telegram", "1001"), ("vk", "1001"),
                              ("telegram", "-1001"), ("vk", "2000000101")):
            with self.subTest(channel=channel, chat=chat):
                entered, release = Event(), Event()
                model = Mock()

                def recognize(*, image, instruction):
                    if image.data.endswith(b"slow"):
                        entered.set()
                        release.wait(5)
                        return OcrResult("Отменённый OCR")
                    return OcrResult("Текст фото с /ask как данными")

                model.recognize_text.side_effect = recognize
                self.open_channels({}, capabilities=(AiCapability(AskQuestion(model=Mock(), max_question_chars=100), ExtractImageText(model=model)),))
                private = chat == "1001"

                async def press(label):
                    if private:
                        await self.choose_section(channel, label, chat)
                    else:
                        await self.press(channel, *self.inline_action(channel, label), chat=chat, author="1001")

                async def photo(data):
                    if channel == "telegram":
                        async def download(file_id, *, destination, seek):
                            destination.write(data)
                        self.bot.download = download
                        async def answer(value, **kwargs):
                            return await self.bot.send_message(chat_id=int(chat), text=value, **kwargs)
                        await self.router.handler(SimpleNamespace(
                            chat=SimpleNamespace(id=int(chat)), from_user=SimpleNamespace(id=1001),
                            message_id=next(self.ids), text=None, caption="/exit", bot=self.bot, answer=answer,
                            photo=[SimpleNamespace(file_id="photo", file_size=None, width=40, height=40)],
                        ))
                    else:
                        with patch("compadre_assistant.presentation.vk.photos._download", new=AsyncMock(return_value=data)):
                            _handle_update({"type": "message_new", "object": {"message": {
                                "peer_id": int(chat), "from_id": 1001, "id": next(self.ids), "text": "/exit",
                                "attachments": [{"type": "photo", "photo": {"sizes": [{
                                    "url": "https://sun1.userapi.com/photo", "width": 40, "height": 40,
                                }]}}],
                            }}}, incoming_message_processor=self.processor, client=self.vk, logger=self.logger,
                                menu_update_handler=self.vk_menu)
                            # Дождаться чтения внутри worker, пока действует подмена загрузки.
                            async with asyncio.timeout(5):
                                while self.processor._deferred_actions.get(ReplyAddress(channel, chat)) is not None and not entered.is_set():
                                    await asyncio.sleep(0.01)

                async def wait_text(fragment):
                    async with asyncio.timeout(5):
                        while fragment not in self.last(channel)[0]:
                            await asyncio.sleep(0.01)

                try:
                    await self.send(channel, "/start", chat=chat, author="1001")
                    await press("Режим ИИ")
                    await press("Извлечь текст с фото")
                    address = ReplyAddress(channel, chat)
                    pending = self.processor._pending_inputs.get(address)
                    await self.send(channel, "вместо фото", chat=chat, author="1001")
                    self.assertNotIn("/cancel", self.last(channel)[0])
                    await press("Помощь")
                    self.assertIn("/get_text_from_image", self.last(channel)[0])
                    self.assertNotIn("/ask", self.last(channel)[0])
                    self.assertIs(self.processor._pending_inputs.get(address), pending)
                    await photo(b"invalid")
                    await wait_text("Не удалось прочитать фото")
                    self.assertNotIn("/get_text_from_image", self.last(channel)[0])
                    await press("Извлечь текст с фото")
                    await photo(b"\xff\xd8\xffslow")
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    cancel = self.inline_action(channel, "Отмена") if channel == "telegram" or not private else None
                    if not private:
                        await self.send(channel, "/cancel", chat=chat, author="1002")
                        self.assertIsNotNone(self.processor._deferred_actions.get(address))
                    if cancel is None:
                        await press("Отмена")
                    else:
                        await self.press(channel, *cancel, chat=chat, author="1001")
                    self.assertIn("Обработка отменена", self.last(channel)[0])
                    release.set()
                    await press("Извлечь текст с фото")
                    entered.clear()
                    await photo(b"\xff\xd8\xffready")
                    await wait_text("Текст фото с /ask как данными")
                    await press("Задать вопрос")
                    all_texts = ([item[1]["text"] for item in self.telegram] if channel == "telegram" else self.vk.texts.values())
                    self.assertFalse(any("Отменённый OCR" in text for text in all_texts))
                finally:
                    release.set()

    async def test_background_cancellation_help_result_and_failure_in_both_channels(self):
        """Справка, отмена и ответы фоновой работы возвращают актуальные действия."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                entered, release = Event(), Event()
                entered_second, release_second = Event(), Event()
                model = Mock()

                def answer(question, instruction):
                    if question == "Первый вопрос":
                        entered.set()
                        release.wait(5)
                        return "Отменённый результат"
                    if question == "Второй вопрос":
                        entered_second.set()
                        release_second.wait(5)
                    if question == "Сбой модели":
                        raise AiModelError("offline")
                    return "Ответ модели с /ask как данными"

                model.answer.side_effect = answer
                use_case = AskQuestion(model=model, max_question_chars=100)
                self.open_channels({}, capabilities=(AiCapability(use_case, Mock()),))

                async def wait_for_reply(fragment):
                    async with asyncio.timeout(5):
                        while True:
                            texts = ([kwargs["text"] for _, kwargs in self.telegram] if channel == "telegram"
                                     else list(self.vk.texts.values()))
                            if any(fragment in text for text in texts):
                                return
                            await asyncio.sleep(0.01)

                try:
                    await self.send(channel, "/start")
                    await self.choose_section(channel, "Режим ИИ", "1001")
                    await self.choose_section(channel, "Задать вопрос", "1001")
                    await self.send(channel, "Первый вопрос")
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    if channel == "telegram":
                        earlier_cancel = self.inline_action(channel, "Отмена")
                    await self.choose_section(channel, "Помощь", "1001")
                    self.assertIn("/cancel", self.last(channel)[0])
                    self.assertNotIn("/ask", self.last(channel)[0])
                    await self.choose_section(channel, "Отмена", "1001")
                    cancellation = ([kwargs["text"] for _, kwargs in self.telegram] if channel == "telegram"
                                    else list(self.vk.texts.values()))
                    self.assertTrue(any("Обработка отменена" in text for text in cancellation))
                    await self.choose_section(channel, "Задать вопрос", "1001")
                    release.set()
                    await self.send(channel, "Второй вопрос")
                    self.assertTrue(await asyncio.to_thread(entered_second.wait, 5))
                    if channel == "telegram":
                        await self.press(channel, *earlier_cancel)
                        self.assertIn("больше недоступна", self.last(channel)[0])
                    release_second.set()
                    await wait_for_reply("Ответ модели с /ask как данными")
                    await self.choose_section(channel, "Помощь", "1001")
                    self.assertIn("/ask", self.last(channel)[0])
                    await self.choose_section(channel, "Задать вопрос", "1001")
                    await self.send(channel, "Сбой модели")
                    await wait_for_reply("Не удалось получить ответ модели")
                    await self.choose_section(channel, "Задать вопрос", "1001")
                    replies = ([kwargs["text"] for _, kwargs in self.telegram] if channel == "telegram"
                               else list(self.vk.texts.values()))
                    self.assertFalse(any("Отменённый результат" in reply for reply in replies))
                finally:
                    release.set()
                    release_second.set()

    async def test_unregistered_channel_accepts_plain_code_in_both_directions(self):
        """Привязывает новый канал к профилю без ввода команды."""
        for source, target, title in (("telegram", "vk", "VK"), ("vk", "telegram", "Telegram")):
            with self.subTest(target=target):
                await self.send(source, "/start")
                await self.choose_section(source, "Настройки", "1001")
                await self.choose_section(source, "Мессенджеры", "1001")
                await self.choose_section(source, f"Привязать {title}", "1001")
                code = re.search(r"Код привязки: (?:<code>)?([\w-]+)", self.last(source)[0]).group(1)
                await self.send(target, "/start", chat="3005")
                await self.choose_section(target, "У меня есть код", "3005")
                await self.send(target, code, chat="3005")
                self.assertIn("привязал", self.last(target)[0])

    async def test_first_entry_and_missing_public_address_keep_manual_link(self):
        """Без публичного имени Telegram ручной путь работает до регистрации."""
        self.open_channels({"telegram": None, "vk": "https://vk.com/write-731"})
        await self.send("vk", "/settings")
        await self.send("vk", "/connect telegram")
        prompt, actions, _ = self.last("vk")
        self.assertIn("Адрес бота не указан", prompt)
        self.assertNotIn("Открыть бота", dict(actions))
        code = re.search(r"Код привязки: (?:<code>)?([\w-]+)", prompt).group(1)
        await self.send("telegram", "/start", chat="3003")
        self.assertEqual([name for name, _ in self.last("telegram")[1]], ["Создать профиль", "У меня есть код", "Помощь"])
        await self.choose_section("telegram", "У меня есть код", "3003")
        await self.send("telegram", f"/link {code}", chat="3003")
        self.assertIn("привязал", self.last("telegram")[0])

    async def test_core_catalog_entry_and_same_section_description_reach_both_channels(self):
        """Вход ядра, меню владельца и обычный текст имеют разные проекции."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                await self.send(channel, "/start")
                if channel == "telegram":
                    self.assertIn("/screens", {item.command for item in self.menus[-1].menu.items})
                else:
                    bottom = json.loads(self.vk.keyboards[-1])
                    self.assertIn("тестовая возможность", {button["action"]["label"] for row in bottom["buttons"] for button in row})
                await self.send(channel, "/screens")
                if channel == "telegram":
                    self.assertEqual([label for label, _ in self.last(channel)[1]], ["Открыть выбор", "Показать текст", "Помощь"])
                else:
                    self.assertFalse(json.loads(self.vk.keyboards[-1])["inline"])
                    self.assertEqual(len(self.vk.messages), 2)
                await self.send(channel, "/note")
                text, actions, _ = self.last(channel)
                self.assertEqual(text, "Обычный результат без действий.")
                self.assertEqual(actions, [])
                if channel == "vk":
                    self.assertIsNone(self.vk.keyboards[-1])

    async def test_page_numbers_stable_targets_and_replay_follow_the_same_scenario(self):
        """Каждый канал сохраняет номера своей страницы и точную цель кнопки."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                self.owner.items = [(f"id-{number}", f"Объект {number}") for number in range(1, 10)]
                await self.send(channel, "/screens")
                await self.send(channel, "/objects")
                text, actions, message_id = self.last(channel)
                size = self.owner.pages[-1][2]
                self.assertEqual(size, 6 if channel == "telegram" else 4)
                self.assertEqual(len(actions), size + (3 if channel == "telegram" else 2))
                await self.press(channel, dict(actions)["Дальше"], message_id)
                text, actions, message_id = self.last(channel)
                first_label, first_token = actions[0]
                self.assertEqual(first_label, f"{size + 1}. Объект {size + 1}")
                self.assertIn(first_label, text)
                pages_before_help = list(self.owner.pages)
                await self.choose_section(channel, "Помощь", "1001")
                self.assertIn("/objects", self.last(channel)[0])
                self.assertEqual(self.owner.pages, pages_before_help)
                before = len(self.owner.selected)
                await self.press(channel, first_token, message_id, chat="1002")
                self.assertEqual(len(self.owner.selected), before)
                await self.press(channel, first_token, message_id)
                self.assertEqual(self.owner.selected[-1][1], f"id-{size + 1}")
                await self.press(channel, first_token, message_id)
                self.assertEqual(len(self.owner.selected), before + 1)
                self.assertIn("больше недоступна", self.last(channel)[0])

    async def test_manual_number_without_buttons_uses_delivered_snapshot_and_rejects_shift(self):
        """Отключённые кнопки сохраняют ручной выбор; сдвиг номеров не меняет цель."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                self.owner.items = [(f"id-{number}", f"Объект {number}") for number in range(1, 10)]
                await self.send(channel, "/screens")
                optional = ("compadre_assistant.presentation.telegram.bot._optional_telegram_inline_keyboard"
                            if channel == "telegram" else "compadre_assistant.presentation.vk.bot._optional_vk_inline_keyboard")
                with patch(optional, return_value=None):
                    await self.send(channel, "/objects")
                self.assertEqual(self.last(channel)[1], [])
                self.assertIn("Введи номер", self.last(channel)[0])
                await self.send(channel, "2")
                self.assertEqual(self.owner.selected[-1][1], "id-2")
                await self.send(channel, "/objects")
                self.owner.items.pop(0)
                count_before = len(self.owner.selected)
                await self.send(channel, "1")
                self.assertEqual(len(self.owner.selected), count_before)
                self.assertIn("Список изменился", self.last(channel)[0])

    async def test_leaving_and_reentering_mode_invalidates_old_choice_in_both_channels(self):
        """Даже тот же режим после повторного входа имеет новую ревизию."""
        for channel in ("telegram", "vk"):
            with self.subTest(channel=channel):
                await self.send(channel, "/screens")
                await self.send(channel, "/objects")
                _, actions, message_id = self.last(channel)
                token = actions[0][1]
                await self.send(channel, "/exit")
                await self.send(channel, "/screens")
                before = len(self.owner.selected)
                await self.press(channel, token, message_id)
                self.assertEqual(len(self.owner.selected), before)
                self.assertIn("больше недоступна", self.last(channel)[0])


if __name__ == "__main__":
    unittest.main()
