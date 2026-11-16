"""Проверяет общий список через обработчики Telegram и VK с локальными fake API."""

from io import BytesIO
from itertools import count
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

from compadre_assistant.bootstrap import (
    ChannelMessageProcessor, create_channel_message_processor, create_user_identity_service,
)
from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.ports import CommandMenuUpdate
from compadre_assistant.presentation.telegram.bot import _register_handlers
from compadre_assistant.presentation.vk.bot import VkApiClient, _create_vk_menu_update_handler, _handle_update
from tests.test_telegram_bot import FakeAiogram


def _runtime(test: unittest.TestCase, channel: str, user_id: int) -> ChannelMessageProcessor:
    database = Path(test.enterContext(TemporaryDirectory(prefix="family-shopping-channel-"))) / "app.db"
    with connect_database(database) as connection:
        apply_migrations(connection)
        create_user_identity_service(connection).register_with_name(
            IncomingIdentity(channel, str(user_id), str(user_id)), display_name="Лена",
        )
    return test.enterContext(create_channel_message_processor(database_path=database, enabled_capabilities=("family",)))


class TelegramFamilyShoppingTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет выбор из последнего списка через Telegram Bot API."""

    def setUp(self) -> None:
        """Подключает настоящие обработчики к fake Bot API."""
        self.processor = _runtime(self, "telegram", 101)
        self.chat = SimpleNamespace(id=101)
        self.author = SimpleNamespace(id=101)
        self.updates = count(200)
        self.messages = count(100)
        self.last_sent = 0
        self.bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=self._send_message),
            edit_message_text=AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(message_id=kwargs["message_id"])),
            edit_message_reply_markup=AsyncMock(),
        )
        self.logger = logging.getLogger("test.family.telegram")
        self.menus: list[set[str]] = []
        aiogram = FakeAiogram()
        _register_handlers(
            aiogram.dispatcher, aiogram, incoming_message_processor=self.processor,
            logger=self.logger, menu_update_handler=self._menu,
        )
        self.router = aiogram.dispatcher.included_router

    def _menu(self, update: CommandMenuUpdate) -> bool:
        self.menus.append({item.command for item in update.menu.items})
        return True

    def _send_message(self, **_) -> SimpleNamespace:
        self.last_sent = next(self.messages)
        return SimpleNamespace(message_id=self.last_sent)

    async def _send(self, text: str) -> None:
        async def answer(value, **kwargs):
            return await self.bot.send_message(chat_id=self.chat.id, text=value, **kwargs)

        await self.router.handler(SimpleNamespace(
            chat=self.chat, message_id=next(self.updates), from_user=self.author,
            text=text, bot=self.bot, answer=answer,
        ))

    async def _press(self, token: str, message_id: int) -> None:
        callback = SimpleNamespace(
            id=str(next(self.updates)), from_user=self.author, bot=self.bot, data=token,
            message=SimpleNamespace(chat=self.chat, message_id=message_id), answer=AsyncMock(),
        )
        await self.router.callback_handler(callback)
        callback.answer.assert_awaited_once()

    @staticmethod
    def _actions(api_call) -> dict[str, str]:
        keyboard = api_call.kwargs["reply_markup"].inline_keyboard
        return {button.text: button.callback_data for row in keyboard for button in row}

    async def _open(self) -> None:
        await self._send("/family")
        await self._send("/family_create")
        await self._send("Дом")
        self.assertFalse({"/add", "/buy", "/delete"} & self.menus[-1])
        await self._send("/family_shopping")
        self.assertTrue({"/add", "/buy", "/delete"} <= self.menus[-1])

    async def test_inline_shopping_commands_reflect_items_and_cancel_add(self) -> None:
        """Покупки доступны кнопками, а отмена пошагового ввода сохраняет список."""
        await self._open()
        empty_list = self.last_sent
        empty_controls = self._actions(self.bot.send_message.await_args)
        self.assertEqual(list(empty_controls), ["Добавить", "Назад"])
        await self._press(empty_controls["Добавить"], empty_list)
        self.assertIn("/cancel", self.bot.send_message.await_args.kwargs["text"])
        prompt_controls = self._actions(self.bot.send_message.await_args)
        await self._press(prompt_controls["Отмена"], self.last_sent)
        self.assertIn("Ввод отменён", self.bot.send_message.await_args.kwargs["text"])

        await self._send("/add Хлеб")
        active_list = self.last_sent
        controls = self._actions(self.bot.send_message.await_args)
        self.assertEqual(list(controls), ["Добавить", "Купить", "Удалить", "Назад"])
        await self._press(controls["Удалить"], active_list)
        delete_pick = self.bot.edit_message_text.await_args
        self.assertEqual(delete_pick.kwargs["message_id"], active_list)
        self.assertIn("Выбери, что удалить", delete_pick.kwargs["text"])
        await self._press(self._actions(delete_pick)["Назад"], active_list)
        await self._press(self._actions(self.bot.edit_message_text.await_args)["Купить"], active_list)
        buy_pick = self.bot.edit_message_text.await_args
        self.assertEqual(buy_pick.kwargs["message_id"], active_list)
        await self._press(self._actions(buy_pick)["1. Хлеб"], active_list)
        await self._press(self._actions(self.bot.edit_message_text.await_args)["Назад"], active_list)
        self.assertEqual(list(self._actions(self.bot.edit_message_text.await_args)),
                         ["Добавить", "Очистить купленное", "Назад"])

    async def test_choice_back_delete_and_clear_edit_the_newest_list(self) -> None:
        """Покупка и удаление, возврат и очистка меняют текст и кнопки одного сообщения."""
        await self._open()
        older_list = self.last_sent
        await self._send("/add")
        await self._send("Хлеб\nМолоко, 2 л\nЯблоки")
        active_list = self.last_sent
        self.assertNotEqual(older_list, active_list)
        sent = self.bot.send_message.await_count

        await self._send("/buy")
        first_edit = self.bot.edit_message_text.await_args
        self.assertEqual(first_edit.kwargs["message_id"], active_list)
        self.assertEqual(self.bot.send_message.await_count, sent)
        self.assertIn("Например, <code>2</code>", first_edit.kwargs["text"])
        buying = self._actions(first_edit)
        self.assertEqual(list(buying), ["1. Хлеб", "2. Молоко, 2 л", "3. Яблоки", "Назад"])
        await self._press(buying["1. Хлеб"], older_list)
        self.bot.edit_message_text.assert_awaited_once()

        await self._press(buying["1. Хлеб"], active_list)
        bought = self.bot.edit_message_text.await_args
        self.assertNotIn("Купленные товары", bought.kwargs["text"])
        self.assertEqual(list(self._actions(bought)), ["1. Молоко, 2 л", "2. Яблоки", "Назад"])
        await self._press(buying["2. Молоко, 2 л"], active_list)
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        self.assertIn("больше недоступна", self.bot.send_message.await_args.kwargs["text"])
        await self._press(self._actions(bought)["Назад"], active_list)

        await self._send("/delete")
        deleting = self._actions(self.bot.edit_message_text.await_args)
        self.assertEqual(list(deleting), ["1. Молоко, 2 л", "2. Яблоки", "Назад"])
        await self._press(deleting["2. Яблоки"], active_list)
        deleted = self.bot.edit_message_text.await_args
        self.assertNotIn("Яблоки", deleted.kwargs["text"])
        await self._press(self._actions(deleted)["Назад"], active_list)
        ordinary = self._actions(self.bot.edit_message_text.await_args)
        await self._press(ordinary["Очистить купленное"], active_list)
        cleared = self.bot.edit_message_text.await_args
        self.assertIn("1. Молоко, 2 л", cleared.kwargs["text"])
        self.assertNotIn("Купленные товары", cleared.kwargs["text"])
        self.assertNotIn("Очистить купленное", self._actions(cleared))
        self.assertTrue(all(call.kwargs["message_id"] == active_list
                            for call in self.bot.edit_message_text.await_args_list))
        self.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_failed_edit_requires_reopen_and_preserves_manual_path(self) -> None:
        """Неудавшийся edit не повторяет покупку, после переоткрытия работают номера."""
        await self._open()
        await self._send("/add Хлеб\nМолоко")
        active_list = self.last_sent
        await self._send("/buy")
        choice = self._actions(self.bot.edit_message_text.await_args)["1. Хлеб"]
        self.bot.edit_message_text.side_effect = RuntimeError("fake edit rejection")
        buy_items = SQLiteFamilyRepository.buy_items
        with patch.object(SQLiteFamilyRepository, "buy_items", autospec=True, side_effect=buy_items) as write:
            with self.assertLogs(self.logger, level="ERROR"):
                await self._press(choice, active_list)
            self.assertIn("/family_shopping", self.bot.send_message.await_args.kwargs["text"])
            await self._send("1")
            self.assertIn("Список для выбора недоступен", self.bot.send_message.await_args.kwargs["text"])
            await self._press(choice, active_list)
            await self._send("/delete")
            self.assertIn("Не удалось обновить список", self.bot.send_message.await_args.kwargs["text"])
            self.assertEqual(self.bot.edit_message_text.await_count, 2)
            write.assert_called_once()
        await self._send("/family_shopping")
        self.assertIn("Купленные товары\n1. Хлеб", self.bot.send_message.await_args.kwargs["text"])
        self.bot.edit_message_text.side_effect = lambda **kwargs: SimpleNamespace(message_id=kwargs["message_id"])
        await self._send("/buy")
        await self._send("1")
        await self._send("/family_shopping")
        result = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Купленные товары\n1. Хлеб\n2. Молоко", result)

    async def test_shopping_without_inline_keyboard_keeps_text_commands(self) -> None:
        """Список и ручные операции доступны при отключённых inline-кнопках."""
        with patch("compadre_assistant.presentation.telegram.bot._optional_telegram_inline_keyboard", return_value=None):
            await self._open()
            await self._send("/add Хлеб\nМолоко")
            self.assertNotIn("reply_markup", self.bot.send_message.await_args.kwargs)
            await self._send("/buy")
            await self._send("1")
            await self._send("/delete")
            await self._send("1")
            self.assertNotIn("Молоко", self.bot.edit_message_text.await_args.kwargs["text"])
            await self._send("/family_clear_bought")
            self.assertIn("Список пуст", self.bot.send_message.await_args.kwargs["text"])


class VkFamilyShoppingTest(unittest.TestCase):
    """Проверяет нижнее меню, inline-клавиатуру и один cmid через VK Long Poll."""

    def setUp(self) -> None:
        """Настраивает VK-клиент с ответами локального обработчика HTTP."""
        self.processor = _runtime(self, "vk", 201)
        self.client = VkApiClient(token="fake-vk-token")
        self.logger = logging.getLogger("test.family.vk")
        self.menu = _create_vk_menu_update_handler(self.client, logger=self.logger)
        self.updates = count(100)
        self.messages = count(42)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.last_cmid = 0
        self.sent_messages: dict[int, dict[str, str | int]] = {}
        self.enterContext(patch("compadre_assistant.presentation.vk.bot.urlopen", side_effect=self._fake_api))

    def _fake_api(self, request, *, timeout):
        method = request.full_url.rsplit("/", 1)[-1]
        params = {key: value[0] for key, value in parse_qs(request.data.decode()).items()}
        self.calls.append((method, params))
        if method == "messages.send":
            self.last_cmid = next(self.messages)
            message_id = 9000 + self.last_cmid
            self.sent_messages[self.last_cmid] = {"id": message_id, "text": params["message"]}
            if "peer_ids" in params:
                response = [{"peer_id": 201, "message_id": message_id, "conversation_message_id": self.last_cmid}]
            else:
                response = message_id
        elif method == "messages.getByConversationMessageId":
            response = {"items": [self.sent_messages[int(params["conversation_message_ids"])]]}
        elif method in ("messages.edit", "messages.sendMessageEventAnswer"):
            response = 1
        else:
            self.fail(f"Неожиданный метод VK API {method}")
        return BytesIO(json.dumps({"response": response}).encode())

    def _handle(self, update: dict) -> None:
        _handle_update(
            update, incoming_message_processor=self.processor, client=self.client,
            logger=self.logger, menu_update_handler=self.menu,
        )

    def _send(self, text: str, *, payload: str | dict | None = None, peer_id: int = 201) -> None:
        self._handle({"type": "message_new", "object": {"message": {
            "peer_id": peer_id, "from_id": 201, "id": next(self.updates), "text": text, "payload": payload,
        }}})

    def _press(self, token: str, cmid: int) -> None:
        self._handle({"type": "message_event", "object": {
            "peer_id": 201, "user_id": 201, "event_id": str(next(self.updates)),
            "conversation_message_id": cmid, "payload": {"token": token},
        }})

    def _latest_edit(self) -> dict[str, str]:
        return next(params for method, params in reversed(self.calls) if method == "messages.edit")

    def _visible_menu(self) -> dict[str, str | None]:
        for method, params in reversed(self.calls):
            if method == "messages.send" and (serialized := params.get("keyboard")):
                keyboard = json.loads(serialized)
                if not keyboard["inline"]:
                    return {button["action"]["label"]: button["action"].get("payload")
                            for row in keyboard["buttons"] for button in row}
        self.fail("Нижнее меню VK не было опубликовано")

    def _menu_press(self, label: str) -> None:
        self._send(label, payload=self._visible_menu()[label])

    def _single_reply(self, action, text: str, *, labels: list[str] | None = None) -> dict[str, str]:
        previous = len(self.calls)
        action()
        sent = [params for method, params in self.calls[previous:] if method == "messages.send"]
        self.assertEqual(len(sent), 1, self.calls[previous:])
        self.assertIn(text, sent[0]["message"])
        if labels is not None:
            self.assertFalse(json.loads(sent[0]["keyboard"])["inline"])
            self.assertEqual(list(self._visible_menu()), labels)
        return sent[0]

    def _actions(self, params: dict[str, str]) -> dict[str, str]:
        keyboard = json.loads(params["keyboard"])
        self.assertTrue(keyboard["inline"])
        self.assertLessEqual(len(keyboard["buttons"]), 6)
        buttons = [button["action"] for row in keyboard["buttons"] for button in row]
        self.assertTrue(all(len(button["label"]) <= 40 for button in buttons))
        return {button["label"]: json.loads(button["payload"])["token"] for button in buttons}

    def test_paginated_selection_clear_and_text_command_use_vk_cmid(self) -> None:
        """Страницы и выбор длинных названий помещаются в клавиатуру одного сообщения."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        self._menu_press("Покупки")
        self._send("/add " + "\n".join([">" * 110, *(f"Товар {number}" for number in range(2, 11))]))
        self._single_reply(lambda: self._menu_press("Следующая страница"), "2/2",
                           labels=["Добавить", "Купить", "Удалить", "Предыдущая страница", "Назад"])

        menu_before_selection = self._visible_menu()
        first = self._single_reply(lambda: self._menu_press("Купить"), "1/3")
        cmid = self.last_cmid
        buttons = self._actions(first)
        self.assertEqual(self._visible_menu(), menu_before_selection)
        self.assertIn("→", buttons)
        self.assertTrue(next(label for label in buttons if label.startswith("1. ")).endswith("…"))
        self.assertIn("/family_shopping item 1", first["message"])
        self._press(buttons["→"], cmid)
        second = self._latest_edit()
        self.assertIn("2/3", second["message"])
        rows = [[button["action"]["label"] for button in row] for row in json.loads(second["keyboard"])["buttons"]]
        self.assertEqual(rows, [[f"{number}. Товар {number}"] for number in range(5, 9)] + [["←", "→"], ["Назад"]])
        self._send("8")
        bought = self._latest_edit()
        self.assertNotIn("8. Товар 8", self._actions(bought))
        self.assertNotIn("Купленные товары", bought["message"])
        self.assertEqual(self._visible_menu(), menu_before_selection)
        self._single_reply(lambda: self._press(self._actions(bought)["Назад"], cmid), "Купленные товары\n1. Товар 8",
                           labels=["Добавить", "Купить", "Удалить", "Очистить купленное", "Предыдущая страница", "Назад"])
        cleared = self._single_reply(lambda: self._menu_press("Очистить купленное"), "Список покупок")
        self.assertNotIn("Купленные товары", cleared["message"])
        self.assertNotIn("Очистить купленное", self._visible_menu())
        self.assertTrue(all(params["cmid"] == str(cmid) for method, params in self.calls if method == "messages.edit"))

        delete = self._single_reply(lambda: self._menu_press("Удалить"), "Выбери, что удалить")
        delete_cmid = self.last_cmid
        self.assertNotEqual(delete_cmid, cmid)
        before = len(self.calls)
        self._press(self._actions(delete)["2. Товар 2"], delete_cmid)
        self.assertEqual([method for method, _ in self.calls[before:]], ["messages.sendMessageEventAnswer", "messages.edit"])
        self.assertEqual(self._latest_edit()["cmid"], str(delete_cmid))
        self.assertNotIn("Товар 2", self._latest_edit()["message"])
        self._single_reply(lambda: self._send("/delete 4"), "/delete без аргументов")
        self._send("/delete")
        self._send("3")
        self._single_reply(lambda: self._menu_press("Назад"), "Список покупок")
        published = self._single_reply(lambda: self._send("/family_shopping"), "Список покупок")
        self.assertIn("3. Товар 5", published["message"])
        self.assertNotIn("Товар 4", published["message"])

    def test_family_menu_and_pending_add_use_one_reply(self) -> None:
        """Карточка, участники, приглашение, покупки, подтверждение и отмена не дублируют ответы."""
        no_family = ["Создать семью", "Вступить по коду"]
        home = ["Покупки", "Участники", "Пригласить", "Выйти из режима"]
        shopping = ["Добавить", "Купить", "Удалить", "Назад"]
        self._single_reply(lambda: self._send("/family"), "Семья", labels=no_family)
        self._single_reply(lambda: self._menu_press("Создать семью"), "Пришли название семьи", labels=["Отмена"])
        self._single_reply(lambda: self._send("Дом"), "Семья «Дом»", labels=home)
        roster = self._single_reply(lambda: self._menu_press("Участники"), "Участники семьи «Дом»")
        self.assertEqual(self._visible_menu().keys(), set(home))
        self.assertIn("Закрыть семью", self._actions(roster))
        self._single_reply(lambda: self._menu_press("Пригласить"), "одноразовый код")
        self._single_reply(lambda: self._menu_press("Покупки"), "Список пуст", labels=shopping)
        empty = self._single_reply(lambda: self._menu_press("Купить"), "Добавь покупку через /add")
        self.assertEqual(list(self._actions(empty)), ["Назад"])
        self._single_reply(lambda: self._menu_press("Назад"), "Список покупок")
        self._single_reply(lambda: self._menu_press("Добавить"), "Напиши, что купить", labels=["Отмена"])
        self._single_reply(lambda: self._menu_press("Отмена"), "Ввод отменён", labels=shopping)
        self._single_reply(lambda: self._menu_press("Назад"), "Семья «Дом»", labels=home)
        confirmation = self._single_reply(lambda: self._send("/family_close"), "Подтверждение действует")
        self.assertEqual(list(self._actions(confirmation)), ["Закрыть семью", "Отменить"])
        self._single_reply(lambda: self._press(self._actions(confirmation)["Отменить"], self.last_cmid), "Действие отменено")
        self._single_reply(lambda: self._menu_press("Выйти из режима"), "Режим этого чата завершён")
        self._single_reply(lambda: self._send("/family"), "Семья «Дом»", labels=home)

    def test_marked_labels_are_exact_private_and_never_become_pending_input(self) -> None:
        """Меню принимает только точный актуальный ярлык и игнорирует команды из payload."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        marker = self._visible_menu()["Покупки"]
        self._single_reply(lambda: self._send("Покупки", payload=marker, peer_id=2_000_000_001), "кнопка меню устарела")
        self._send("Покупки", payload=json.loads(marker))
        self._menu_press("Добавить")
        for label in ("Покупки", "Отмена ", "/exit"):
            self._single_reply(lambda: self._send(label, payload=marker), "кнопка меню устарела")
        self._send("Отмена", payload={"command": "/exit"})
        saved = self._single_reply(lambda: self._send("/family_shopping"), "1. Отмена")
        self.assertNotIn("Покупки\n", saved["message"])
        self.assertNotIn("2.", saved["message"])
