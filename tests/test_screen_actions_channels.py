"""Один владелец экрана проходит настоящие входы и обе проекции каналов."""

import asyncio
from dataclasses import replace
from itertools import count
import json
import logging
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiogram

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityHelp, CapabilityResult, ConfirmationQueryResult, HelpSection,
    IncomingResponse, PendingConfirmationQueryPort, ReplyAction, ResponseView, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.telegram.bot import _create_telegram_menu_update_handler, _register_handlers
from compadre_assistant.presentation.vk.bot import _create_vk_menu_update_handler, _handle_update
from tests.test_incoming_boundary import FakeCapability
from tests.test_telegram_bot import FakeAiogram
from tests.test_vk_screen_actions import ProjectionClient


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

    def send_message(self, **kwargs):
        """Запоминает текст подтверждённой публикации."""
        message_id = super().send_message(**kwargs)
        self.texts[message_id] = kwargs["text"]
        return message_id

    def edit_message(self, **kwargs):
        """Запоминает текст подтверждённого редактирования."""
        super().edit_message(**kwargs)
        self.texts[kwargs["conversation_message_id"]] = kwargs["text"]


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

    def open_channels(self, dialogs):
        """Подключает два локальных клиента к процессору с публичными адресами установки."""
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(self.owner,), messenger_dialogs=dialogs)
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
        _register_handlers(sdk.dispatcher, sdk, incoming_message_processor=self.processor,
                           logger=self.logger, menu_update_handler=menu_update)
        self.router = sdk.dispatcher.included_router
        self.vk = ObservedVkClient()
        self.vk_menu = _create_vk_menu_update_handler(self.vk, logger=self.logger)

    async def send(self, channel, text, *, chat="1001", menu=False, author=None):
        """Передаёт сообщение реальному входному обработчику нужного канала."""
        author = author or chat
        if channel == "telegram":
            async def answer(value, **kwargs):
                return await self.bot.send_message(chat_id=int(chat), text=value, **kwargs)

            await self.router.handler(SimpleNamespace(
                chat=SimpleNamespace(id=int(chat)), from_user=SimpleNamespace(id=int(author)),
                message_id=1, text=text, bot=self.bot, answer=answer,
            ))
        else:
            _handle_update({"type": "message_new", "object": {"message": {
                "peer_id": int(chat), "from_id": int(author), "id": 1, "text": text,
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

    def bottom(self):
        """Читает последнюю доставленную нижнюю клавиатуру VK."""
        keyboard = next(k for _, _, k in reversed(self.vk.events) if k and not k["inline"])
        return {button["action"]["label"]: button["action"] for row in keyboard["buttons"] for button in row}

    async def choose_section(self, channel, label, chat):
        """Нажимает реально показанное действие раздела по правилам канала."""
        if channel == "vk":
            self.assertIn(label, self.bottom())
            await self.send(channel, label, chat=chat, menu=True)
        else:
            _, actions, message_id = self.last(channel)
            await self.press(channel, dict(actions)[label], message_id, chat=chat)

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
        """Проверяет оба диалога, реальные клавиатуры переноса и восстановление по команде."""
        for source, target, title, chat, url in (
            ("telegram", "vk", "VK", "1001", "https://vk.com/write-731"),
            ("vk", "telegram", "Telegram", "1002", "https://t.me/example_chat_bot"),
        ):
            with self.subTest(target=target):
                await self.send(source, "/settings", chat=chat)
                await self.choose_section(source, "Мессенджеры", chat)
                self.assertIn(f"{title} — не привязан", self.last(source)[0])
                await self.choose_section(source, f"Привязать {title}", chat)
                prompt, buttons, _ = self.last(source)
                code = re.search(r"/link ([\w-]+)", prompt).group(1)
                self.assertEqual(dict(buttons)["Открыть бота"], url)
                await self.send(target, "/settings", chat=chat)
                await self.choose_section(target, "Мессенджеры", chat)
                await self.choose_section(target, f"Привязать {title} к другому профилю", chat)
                await self.send(target, "неверный-код", chat=chat)
                self.assertIn(f"/connect {target}", self.last(target)[0])
                await self.send(target, code, chat=chat)
                # VK может отдельно доставить изменившееся нижнее меню.
                if target == "vk":
                    item = next(item for item in reversed(self.vk.events) if item[2] and item[2]["inline"])
                    choose = next(button["action"] for row in item[2]["buttons"] for button in row
                                  if button["action"]["label"] == "Перенести аккаунт")
                    token, message_id = json.loads(choose["payload"])["token"], item[1]
                else:
                    _, actions, message_id = self.last(target)
                    token = dict(actions)["Перенести аккаунт"]
                await self.press(target, token, message_id, chat=chat)
                self.assertIn("перепривязал", self.last(target)[0])
                await self.send(source, "/connect", chat=chat)
                self.assertIn(f"{title} — привязан", self.last(source)[0])
                if source == "vk":
                    self.assertIn("Открыть бота", self.bottom())
                delivered_url = (self.bottom()["Открыть бота"]["link"] if source == "vk"
                                 else dict(self.last(source)[1])["Открыть бота"])
                self.assertEqual(delivered_url, url)

    async def test_first_entry_and_missing_public_address_keep_manual_link(self):
        """Без публичного имени Telegram ручной путь работает до регистрации."""
        self.open_channels({"telegram": None, "vk": "https://vk.com/write-731"})
        await self.send("vk", "/settings")
        await self.send("vk", "/connect telegram")
        prompt, actions, _ = self.last("vk")
        self.assertIn("Адрес бота не указан", prompt)
        self.assertNotIn("Открыть бота", dict(actions))
        code = re.search(r"/link ([\w-]+)", prompt).group(1)
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
