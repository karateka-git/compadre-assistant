"""Проверяет общий список через обработчики Telegram и VK с локальными fake API."""

import asyncio
from datetime import UTC, datetime
from functools import partial
from html import unescape
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
from compadre_assistant.capabilities.family.domain.shopping import ManageShopping, ShoppingItem, ShoppingItemStatus
from compadre_assistant.capabilities.family.presentation.shopping import (
    ShoppingSection, choice_page_for_list, selection_page_size, shopping_list, shopping_page, shopping_section_actions,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionLayoutLimits, ActionPurpose, ActionScope, ScreenDestination,
)
from compadre_assistant.core.presentation.incoming.action_selection import ResponseActionStore
from compadre_assistant.presentation.telegram.bot import (
    _create_telegram_menu_update_handler, _register_handlers, safe_send_telegram_reply,
)
from compadre_assistant.presentation.vk.bot import (
    VkApiClient, _create_vk_menu_update_handler, _handle_update, _safe_send_vk_message,
)
from tests.response_assertions import response_text
from tests.test_telegram_bot import FakeAiogram


_SHOPPING_HELP_COMMANDS = (
    "/add [<items>]", "/buy", "/delete", "/family_clear_bought",
    "/family_shopping page <number>", "/family_shopping buy <page>", "/family_shopping delete <page>",
    "/family_shopping back", "/family_shopping home",
)
_SHOPPING_TEXT_DUPLICATES = (
    "/add", "/buy", "/delete", "/family_clear_bought", "/family_shopping page", "/family_shopping home",
    "/family_shopping item",
)
_SELECTION_TEXT_DUPLICATES = (
    "Например", "/cancel", "/family_shopping buy", "/family_shopping delete", "/family_shopping back",
    "/family_shopping item",
)
_TITLE_AT_PREVIEW_LIMIT = "А" * 90
_TITLE_OVER_PREVIEW_LIMIT = "Б" * 90 + "Я"
_PREVIEW_LINES = [f"1. {_TITLE_AT_PREVIEW_LIMIT}", f"2. {_TITLE_OVER_PREVIEW_LIMIT[:90]}..."]


def _runtime(test: unittest.TestCase, channel: str, user_id: int) -> tuple[ChannelMessageProcessor, Path]:
    database = Path(test.enterContext(TemporaryDirectory(prefix="family-shopping-channel-"))) / "app.db"
    with connect_database(database) as connection:
        apply_migrations(connection)
        create_user_identity_service(connection).register_with_name(
            IncomingIdentity(channel, str(user_id), str(user_id)), display_name="Лена",
        )
    processor = test.enterContext(create_channel_message_processor(database_path=database, enabled_capabilities=("family",)))
    return processor, database


def _seed_long_bought(database: Path, channel: str, user_id: str) -> tuple[str, ...]:
    """Добавляет покупки на две страницы и длинный купленный блок."""
    names = tuple(f"Купленный товар {index:02d} " + "Ж<&" * 22 for index in range(1, 45))
    pending = tuple(f"Нужный товар {index:02d}" for index in range(1, 12))
    with connect_database(database) as connection:
        user = create_user_identity_service(connection).resolve_without_touch(IncomingIdentity(channel, user_id, user_id))
        repository = SQLiteFamilyRepository(connection)
        family = repository.find_for_member(user.id)
        actor = next(member for member in repository.list_members(user.id, family.id) if member.app_user_id == user.id)
        shopping = ManageShopping(repository)
        shopping.add(user.id, family.id, "\n".join((*pending, *names)), actor_joined_at=actor.joined_at)
        shopping.buy(user.id, family.id, tuple(range(12, 12 + len(names))), actor_joined_at=actor.joined_at)
    return names


class ShoppingPageTest(unittest.TestCase):
    """Проверяет логические страницы, общие для Telegram и VK."""

    def test_item_counts_and_groups_on_each_page(self) -> None:
        """Только «Нужно купить» задаёт размер и число страниц."""
        variants = ((5, 8, (5,)), (10, 3, (10,)), (13, 8, (10, 3)), (20, 4, (10, 10)),
                    (23, 0, (10, 10, 3)), (0, 6, (0,)), (0, 0, (0,)))
        now = datetime.now(UTC)
        section = ShoppingSection(1, 1, now)
        for needed, bought, counts in variants:
            with self.subTest(needed=needed, bought=bought):
                items = tuple(
                    ShoppingItem(number, f"{prefix}{number:02d}", status, 1, now, None, None)
                    for first, end, prefix, status in (
                        (1, needed + 1, "Нужный-", ShoppingItemStatus.NEEDED),
                        (needed + 1, needed + bought + 1, "Купленный-", ShoppingItemStatus.BOUGHT),
                    )
                    for number in range(first, end)
                )
                for page, count_items in enumerate(counts, 1):
                    content = response_text(shopping_list(items, section.ordinary_view(items, page)))
                    self.assertEqual(content.count(". Нужный-"), count_items)
                    if count_items:
                        first = (page - 1) * 10 + 1
                        self.assertIn(f"{first}. Нужный-{first:02d}", content)
                    if len(counts) == 1:
                        self.assertNotIn(" · 1/", content)
                    else:
                        self.assertIn(f" · {page}/{len(counts)}", content)
                    if bought and page == len(counts):
                        self.assertIn(f"Куплено\n1. Купленный-{needed + 1:02d}", content)
                        self.assertEqual(content.count(". Купленный-"), bought)
                    else:
                        self.assertNotIn("Куплено", content)
                    self.assertNotIn("Смотри другие страницы", content)
                    if not needed:
                        self.assertIn("Нужно купить\nСписок пуст." if bought else "Список покупок пуст.", content)

    def test_unknown_items_keep_separate_numbering_on_last_page(self) -> None:
        """Неизвестный статус сохраняет собственную группу и не занимает место покупок."""
        now = datetime.now(UTC)
        section = ShoppingSection(1, 1, now)
        items = tuple(
            ShoppingItem(number, f"Товар {number}", ShoppingItemStatus.NEEDED, 1, now, None, None)
            for number in range(1, 12)
        ) + (ShoppingItem(12, "Другой товар", ShoppingItemStatus.UNKNOWN, 1, now, None, None),)
        self.assertNotIn("с неизвестным состоянием", response_text(shopping_list(items, section.ordinary_view(items, 1))))
        final = response_text(shopping_list(items, section.ordinary_view(items, 2)))
        self.assertIn("11. Товар 11\n\nПокупки с неизвестным состоянием\n1. Другой товар", final)
        self.assertNotIn("Куплено", final)

    def test_section_actions_describe_availability_and_navigation(self) -> None:
        """Обычный список описывает доступные действия по статусам и фактической странице."""
        now = datetime.now(UTC)
        section = ShoppingSection(1, 1, now)
        empty = shopping_section_actions(section.ordinary_view((), 1))
        self.assertEqual([action.label for action in empty], ["Добавить", "Назад"])
        self.assertEqual(empty[-1].destination, ScreenDestination("family-home"))
        bought = ShoppingItem(22, "Куплено", ShoppingItemStatus.BOUGHT, 1, now, None, None)
        bought_only = shopping_section_actions(section.ordinary_view((bought,), 1))
        self.assertEqual([action.label for action in bought_only], ["Добавить", "Очистить купленное", "Назад"])
        needed = tuple(
            ShoppingItem(number, f"Товар {number}", ShoppingItemStatus.NEEDED, 1, now, None, None)
            for number in range(1, 22)
        )
        actions = shopping_section_actions(section.ordinary_view(needed + (bought,), 2))
        self.assertTrue(all(action.scope is ActionScope.SECTION for action in actions))
        self.assertEqual([action.label for action in actions], [
            "Добавить", "Купить", "Удалить", "Очистить купленное", "Предыдущая страница", "Следующая страница", "Назад",
        ])
        self.assertEqual(actions[1].command, f"/family_shopping section {section.action_key} choose buy 2")
        self.assertIs(actions[1].purpose, ActionPurpose.NAVIGATION)
        self.assertEqual(actions[4].destination, ScreenDestination("shopping-list", 1))
        self.assertEqual(actions[5].destination, ScreenDestination("shopping-list", 3))

    def test_selection_capacity_preserves_positions_and_text_fallback(self) -> None:
        """Размер зависит от обоих ограничений, а отсутствие кнопок допускает ручной номер."""
        now = datetime.now(UTC)
        items = tuple(
            ShoppingItem(2 * number, f"Товар {number}", ShoppingItemStatus.NEEDED, 1, now, None, None)
            for number in range(1, 14)
        )
        variants = (
            (ActionLayoutLimits(100, 100, 8, 64), 5, 3, (11, 12, 13)),
            (ActionLayoutLimits(30, 6, 5, 40), 4, 3, (9, 10, 11, 12)),
            (ActionLayoutLimits(6, 6, 5, 40), 3, 4, (10, 11, 12)),
            (ActionLayoutLimits(), 5, 3, (11, 12, 13)),
        )
        for layout, expected_size, expected_page, numbers in variants:
            with self.subTest(layout=layout):
                size = selection_page_size(layout)
                self.assertEqual(size, expected_size)
                page = shopping_page(items, choice_page_for_list(2, size), selection_size=size)
                self.assertEqual(page.number, expected_page)
                self.assertEqual(tuple((position, item.number) for position, item in page.visible),
                                 tuple((number, 2 * number) for number in numbers))


class TelegramFamilyShoppingTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет выбор из последнего списка через Telegram Bot API."""

    async def asyncSetUp(self) -> None:
        """Подключает настоящие обработчики к fake Bot API."""
        self.processor, self.database = _runtime(self, "telegram", 101)
        self.chat = SimpleNamespace(id=101)
        self.author = SimpleNamespace(id=101)
        self.updates = count(200)
        self.messages = count(100)
        self.last_sent = 0
        self.bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=self._send_message),
            edit_message_text=AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(message_id=kwargs["message_id"])),
            edit_message_reply_markup=AsyncMock(),
            set_my_commands=AsyncMock(),
        )
        self.logger = logging.getLogger("test.family.telegram")
        aiogram = FakeAiogram()
        publish_menu = _create_telegram_menu_update_handler(
            self.bot, loop=asyncio.get_running_loop(), aiogram=__import__("aiogram"), logger=self.logger,
        )
        _register_handlers(
            aiogram.dispatcher, aiogram, incoming_message_processor=self.processor,
            logger=self.logger, menu_update_handler=publish_menu,
        )
        self.router = aiogram.dispatcher.included_router

    def _menu(self) -> set[str]:
        published = self.bot.set_my_commands.await_args.kwargs
        self.assertEqual(published["scope"].chat_id, self.chat.id)
        return {f"/{item.command}" for item in published["commands"]}

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
        self.assertFalse({"/add", "/buy", "/delete"} & self._menu())
        await self._send("/family_shopping")
        self.assertEqual(self._menu(), {"/help"})

    async def test_menu_help_without_text_duplicates_preserves_pending_add(self) -> None:
        """Telegram публикует /help, а вызов справки сохраняет ожидание ввода."""
        await self._open()
        title = "длинное название " * 6
        await self._send("/add " + "\n".join([title, *(f"Товар {index}" for index in range(2, 12))]))
        ordinary = self.bot.send_message.await_args
        ordinary_id = self.last_sent
        for duplicate in _SHOPPING_TEXT_DUPLICATES:
            self.assertNotIn(duplicate, ordinary.kwargs["text"])
        controls = self._actions(ordinary)
        self.assertEqual(list(controls), ["Добавить", "Купить", "Удалить", "Следующая страница", "Назад", "Помощь"])

        await self._press(controls["Купить"], ordinary_id)
        selection = self.bot.edit_message_text.await_args
        self.assertIn("пришли номер товара", selection.kwargs["text"])
        for duplicate in _SELECTION_TEXT_DUPLICATES:
            self.assertNotIn(duplicate, selection.kwargs["text"])
        self.assertIn("Следующая страница", self._actions(selection))
        await self._press(self._actions(selection)["Назад"], ordinary_id)
        controls = self._actions(self.bot.edit_message_text.await_args)

        await self._send("/help")
        help_text = unescape(self.bot.send_message.await_args.kwargs["text"])
        for command in _SHOPPING_HELP_COMMANDS:
            self.assertIn(command + "</code>", help_text)
        await self._press(controls["Добавить"], ordinary_id)
        self.assertEqual(self._menu(), {"/help"})
        await self._send("/help")
        self.assertIn("Ожидается", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self._menu(), {"/help"})
        await self._send("Морковь")
        self.assertIn("/help", self._menu())
        await self._send("/family_shopping page 2")
        self.assertIn("12. Морковь", self.bot.send_message.await_args.kwargs["text"])

    async def test_long_titles_in_list_and_selection(self) -> None:
        """Telegram показывает сокращение в тексте и сохраняет выбор товара кнопкой."""
        await self._open()
        await self._send(f"/add {_TITLE_AT_PREVIEW_LIMIT}\n{_TITLE_OVER_PREVIEW_LIMIT}")
        ordinary = self.bot.send_message.await_args
        active_list = self.last_sent
        self.assertEqual(ordinary.kwargs["text"].splitlines()[3:], _PREVIEW_LINES)
        actions = self._actions(ordinary)
        self.assertEqual(list(actions), ["Добавить", "Купить", "Удалить", "Назад", "Помощь"])

        await self._press(actions["Купить"], active_list)
        choice = self.bot.edit_message_text.await_args
        self.assertEqual(choice.kwargs["text"].splitlines()[3:5], _PREVIEW_LINES)
        buy = self._actions(choice)
        first, second, back, help_label = buy
        self.assertEqual(back, "Назад")
        self.assertTrue(first.startswith("1. "))
        self.assertTrue(second.startswith("2. "))
        await self._press(buy[second], active_list)
        await self._press(self._actions(self.bot.edit_message_text.await_args)["Назад"], active_list)

        ordinary = self.bot.edit_message_text.await_args
        self.assertIn(f"Куплено\n1. {_TITLE_OVER_PREVIEW_LIMIT[:90]}...", ordinary.kwargs["text"])
        await self._press(self._actions(ordinary)["Удалить"], active_list)
        delete = self._actions(self.bot.edit_message_text.await_args)
        await self._press(delete[first], active_list)
        await self._press(self._actions(self.bot.edit_message_text.await_args)["Назад"], active_list)
        self.assertNotIn(_TITLE_AT_PREVIEW_LIMIT, self.bot.edit_message_text.await_args.kwargs["text"])

    async def test_inline_shopping_commands_reflect_items_and_cancel_add(self) -> None:
        """Покупки доступны кнопками, а отмена пошагового ввода сохраняет список."""
        await self._open()
        empty_list = self.last_sent
        empty_controls = self._actions(self.bot.send_message.await_args)
        self.assertEqual(list(empty_controls), ["Добавить", "Назад", "Помощь"])
        await self._press(empty_controls["Добавить"], empty_list)
        self.assertIn("На ввод 10 минут", self.bot.send_message.await_args.kwargs["text"])
        self.assertNotIn("/cancel", self.bot.send_message.await_args.kwargs["text"])
        prompt_controls = self._actions(self.bot.send_message.await_args)
        await self._press(prompt_controls["Отмена"], self.last_sent)
        self.assertIn("Ввод отменён", self.bot.send_message.await_args.kwargs["text"])

        await self._send("/add Хлеб")
        active_list = self.last_sent
        controls = self._actions(self.bot.send_message.await_args)
        self.assertEqual(list(controls), ["Добавить", "Купить", "Удалить", "Назад", "Помощь"])
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
                         ["Добавить", "Очистить купленное", "Назад", "Помощь"])

    async def test_choice_back_delete_and_clear_edit_the_newest_list(self) -> None:
        """Покупка и удаление, возврат и очистка меняют текст и кнопки одного сообщения."""
        await self._open()
        older_list = self.last_sent
        await self._send("/add")
        prompt_id = self.last_sent
        await self._send("Хлеб\nМолоко, 2 л\nЯблоки")
        active_list = self.last_sent
        self.assertNotEqual(older_list, active_list)
        sent = self.bot.send_message.await_count

        await self._send("/buy")
        first_edit = self.bot.edit_message_text.await_args
        self.assertEqual(first_edit.kwargs["message_id"], active_list)
        self.assertEqual(self.bot.send_message.await_count, sent)
        self.assertIn("пришли номер товара", first_edit.kwargs["text"])
        buying = self._actions(first_edit)
        self.assertEqual(list(buying), ["1. Хлеб", "2. Молоко, 2 л", "3. Яблоки", "Назад", "Помощь"])
        await self._press(buying["1. Хлеб"], older_list)
        self.bot.edit_message_text.assert_awaited_once()

        await self._press(buying["1. Хлеб"], active_list)
        bought = self.bot.edit_message_text.await_args
        self.assertNotIn("Куплено", bought.kwargs["text"])
        self.assertEqual(list(self._actions(bought)), ["1. Молоко, 2 л", "2. Яблоки", "Назад", "Помощь"])
        await self._press(buying["2. Молоко, 2 л"], active_list)
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        self.assertIn("больше недоступна", self.bot.send_message.await_args.kwargs["text"])
        await self._press(self._actions(bought)["Назад"], active_list)

        await self._send("/delete")
        deleting = self._actions(self.bot.edit_message_text.await_args)
        self.assertEqual(list(deleting), ["1. Молоко, 2 л", "2. Яблоки", "Назад", "Помощь"])
        await self._press(deleting["2. Яблоки"], active_list)
        deleted = self.bot.edit_message_text.await_args
        self.assertNotIn("Яблоки", deleted.kwargs["text"])
        await self._press(self._actions(deleted)["Назад"], active_list)
        ordinary = self._actions(self.bot.edit_message_text.await_args)
        await self._press(ordinary["Очистить купленное"], active_list)
        cleared = self.bot.edit_message_text.await_args
        self.assertIn("1. Молоко, 2 л", cleared.kwargs["text"])
        self.assertNotIn("Куплено", cleared.kwargs["text"])
        self.assertNotIn("Очистить купленное", self._actions(cleared))
        self.assertTrue(all(call.kwargs["message_id"] == active_list
                            for call in self.bot.edit_message_text.await_args_list))
        self.assertEqual([call.kwargs["message_id"] for call in self.bot.edit_message_reply_markup.await_args_list],
                         [older_list, prompt_id])

    async def test_add_cancel_and_mode_switch_remove_only_previous_keyboard(self) -> None:
        """Отмена даёт путь к списку, а смена режима снимает отслеживаемые кнопки."""
        await self._open()
        await self._send("/add Хлеб\nМолоко")
        active_list = self.last_sent
        await self._send("/buy")
        old_buy = self._actions(self.bot.edit_message_text.await_args)["1. Хлеб"]
        self.bot.edit_message_reply_markup.reset_mock()
        await self._send("/add")
        prompt_id = self.last_sent
        cancel = self._actions(self.bot.send_message.await_args)["Отмена"]
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=101, message_id=active_list, reply_markup=None)
        await self._press(old_buy, active_list)
        self.assertNotIn("/family_shopping", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self.bot.edit_message_reply_markup.await_count, 1)
        await self._press(cancel, prompt_id)
        self.assertIn("Ввод отменён", self.bot.send_message.await_args.kwargs["text"])
        self.assertNotIn("/family_shopping", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self.bot.edit_message_reply_markup.await_args.kwargs["message_id"], prompt_id)

        await self._send("/family_shopping")
        reopened = self.last_sent
        refreshed_buy = self._actions(self.bot.send_message.await_args)["Купить"]
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("1. Хлеб", text)
        self.assertNotIn("Куплено", text)
        await self._send("/settings")
        self.assertEqual(self.bot.edit_message_reply_markup.await_args.kwargs["message_id"], reopened)
        await self._press(refreshed_buy, reopened)
        self.assertIn("Семья", self._actions(self.bot.send_message.await_args))
        self.assertEqual(self.bot.edit_message_reply_markup.await_count, 3)

    async def test_failed_replacement_defers_removal_and_failed_cleanup_preserves_new_list(self) -> None:
        """Сбой отправки сохраняет старую клавиатуру, а сбой cleanup не отменяет новый список."""
        await self._open()
        old_id = self.last_sent
        old_button = self._actions(self.bot.send_message.await_args)["Добавить"]
        self.bot.send_message.side_effect = RuntimeError("fake send rejection")
        with self.assertLogs(self.logger, level="ERROR"):
            await self._send("/add Хлеб")
        self.bot.edit_message_reply_markup.assert_not_awaited()

        self.bot.send_message.side_effect = self._send_message
        self.bot.edit_message_reply_markup.side_effect = RuntimeError("fake cleanup rejection")
        with self.assertLogs(self.logger, level="ERROR"):
            await self._send("/family_shopping")
        current_id = self.last_sent
        self.assertIn("1. Хлеб", self.bot.send_message.await_args.kwargs["text"])
        buy = self._actions(self.bot.send_message.await_args)["Купить"]
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=101, message_id=old_id, reply_markup=None)
        await self._press(old_button, old_id)
        self.assertIn("больше недоступна", self.bot.send_message.await_args.kwargs["text"])
        await self._press(buy, current_id)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], current_id)
        self.assertEqual(self.bot.edit_message_reply_markup.await_count, 1)

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
            self.assertNotIn("/family_shopping", self.bot.send_message.await_args.kwargs["text"])
            self.assertEqual(set(self._actions(self.bot.send_message.await_args)), {"Покупки", "Помощь"})
            await self._send("1")
            self.assertIn("Список для выбора недоступен", self.bot.send_message.await_args.kwargs["text"])
            await self._press(choice, active_list)
            await self._send("/delete")
            self.assertIn("Не удалось обновить список", self.bot.send_message.await_args.kwargs["text"])
            self.assertEqual(self.bot.edit_message_text.await_count, 2)
            write.assert_called_once()
        await self._send("/family_shopping")
        self.assertIn("Куплено\n1. Хлеб", self.bot.send_message.await_args.kwargs["text"])
        self.bot.edit_message_text.side_effect = lambda **kwargs: SimpleNamespace(message_id=kwargs["message_id"])
        await self._send("/buy")
        await self._send("1")
        await self._send("/family_shopping")
        result = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Куплено\n1. Хлеб\n2. Молоко", result)

    async def test_shopping_without_inline_keyboard_keeps_text_commands(self) -> None:
        """Список и ручные операции доступны при отключённых inline-кнопках."""
        with patch("compadre_assistant.presentation.telegram.bot.TELEGRAM_INLINE_ACTIONS_ENABLED", False):
            await self._open()
            await self._send("/add")
            self.assertNotIn("/cancel", self.bot.send_message.await_args.kwargs["text"])
            self.assertNotIn("reply_markup", self.bot.send_message.await_args.kwargs)
            await self._send("/cancel")
            self.assertIn("Ввод отменён", self.bot.send_message.await_args.kwargs["text"])
            await self._send("/add Хлеб\nМолоко")
            self.assertNotIn("reply_markup", self.bot.send_message.await_args.kwargs)
            await self._send("/buy")
            await self._send("1")
            await self._send("/delete")
            await self._send("1")
            self.assertNotIn("Молоко", self.bot.edit_message_text.await_args.kwargs["text"])
            await self._send("/family_clear_bought")
            self.assertIn("Список покупок пуст", self.bot.send_message.await_args.kwargs["text"])

    async def test_long_page_has_navigation_on_last_message_and_short_replacement_is_sent(self) -> None:
        """Переход на длинную страницу переносит кнопки после доставки всего HTML."""
        await self._open()
        title = "<&" * 40 + " финиш"
        await self._send("/add " + "\n".join([*(f"Товар {index}" for index in range(1, 11)), title]))
        short_id = self.last_sent
        next_page = self._actions(self.bot.send_message.await_args)["Следующая страница"]
        self.bot.send_message.reset_mock()
        self.bot.edit_message_reply_markup.reset_mock()
        with patch(
            "compadre_assistant.presentation.telegram.bot.safe_send_telegram_reply",
            partial(safe_send_telegram_reply, limit=250),
        ):
            await self._press(next_page, short_id)
        chunks = [call.kwargs for call in self.bot.send_message.await_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk["text"].encode("utf-16-le")) // 2 <= 250 for chunk in chunks))
        joined = "".join(unescape(chunk["text"]) for chunk in chunks)
        self.assertIn(f"11. {title}", joined)
        self.assertTrue(all("reply_markup" not in chunk for chunk in chunks[:-1]))
        previous = self._actions(self.bot.send_message.await_args)["Предыдущая страница"]
        long_id = self.last_sent
        self.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=101, message_id=short_id, reply_markup=None,
        )
        self.bot.edit_message_text.assert_not_awaited()

        await self._press(previous, long_id)
        new_id = self.last_sent
        self.assertNotEqual(long_id, new_id)
        self.assertIn("1/2", self.bot.send_message.await_args.kwargs["text"])
        self.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(self.bot.edit_message_reply_markup.await_args.kwargs["message_id"], long_id)
        await self._press(self._actions(self.bot.send_message.await_args)["Купить"], new_id)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], new_id)

    async def test_partial_long_replacement_preserves_previous_keyboard(self) -> None:
        """При отказе части новый ID не подтверждается и прежние кнопки не снимаются."""
        await self._open()
        await self._send("/add " + "\n".join([*(f"Товар {index}" for index in range(1, 11)), "<" * 79]))
        previous_id = self.last_sent
        next_page = self._actions(self.bot.send_message.await_args)["Следующая страница"]
        self.bot.edit_message_reply_markup.reset_mock()
        self.bot.send_message.reset_mock()
        self.bot.send_message.side_effect = [
            SimpleNamespace(message_id=9001), RuntimeError("fake second part rejection"),
            SimpleNamespace(message_id=9002),
        ]
        with (
            patch("compadre_assistant.presentation.telegram.bot.safe_send_telegram_reply",
                  partial(safe_send_telegram_reply, limit=250)),
            self.assertLogs(self.logger, level="ERROR"),
        ):
            await self._press(next_page, previous_id)
        self.bot.edit_message_reply_markup.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()
        self.assertIn("Не удалось обновить список", self.bot.send_message.await_args.kwargs["text"])
        self.assertTrue(all("reply_markup" not in call.kwargs for call in self.bot.send_message.await_args_list[:-1]))
        self.assertIn("Помощь", self._actions(self.bot.send_message.await_args))
        self.bot.send_message.side_effect = self._send_message
        await self._send("/buy")
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], previous_id)

    async def test_entire_bought_group_stays_on_last_logical_telegram_page(self) -> None:
        """Большой купленный блок доставляется частями последней страницы."""
        await self._open()
        bought = _seed_long_bought(self.database, "telegram", "101")
        await self._send("/family_shopping")
        self.assertNotIn("Куплено", self.bot.send_message.await_args.kwargs["text"])
        current_id = self.last_sent
        next_page = self._actions(self.bot.send_message.await_args)["Следующая страница"]
        self.bot.send_message.reset_mock()
        await self._press(next_page, current_id)
        chunks = [call.kwargs for call in self.bot.send_message.await_args_list]
        self.assertGreater(len(chunks), 1)
        joined = "".join(unescape(chunk["text"]) for chunk in chunks)
        self.assertIn("Список покупок · 2/2\n\nНужно купить\n11. Нужный товар 11\n\nКуплено", joined)
        for position, name in enumerate(bought, 1):
            self.assertIn(f"{position}. {name}", joined)
        self.assertIn("Предыдущая страница", self._actions(self.bot.send_message.await_args))


class VkFamilyShoppingTest(unittest.TestCase):
    """Проверяет нижнее меню, inline-клавиатуру и один cmid через VK Long Poll."""

    def setUp(self) -> None:
        """Настраивает VK-клиент с ответами локального обработчика HTTP."""
        self.processor, self.database = _runtime(self, "vk", 201)
        self.client = VkApiClient(token="fake-vk-token")
        self.logger = logging.getLogger("test.family.vk")
        self.menu = _create_vk_menu_update_handler(self.client, logger=self.logger)
        self.updates = count(100)
        self.messages = count(42)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.last_cmid = 0
        self.sent_messages: dict[int, dict[str, str | int]] = {}
        self.delivered_menu: dict[str, str | None] | None = None
        self.reject_edits: set[int] = set()
        self.confirm_sends = True
        self.enterContext(patch("compadre_assistant.presentation.vk.bot.urlopen", side_effect=self._fake_api))

    def _fake_api(self, request, *, timeout):
        method = request.full_url.rsplit("/", 1)[-1]
        params = {key: value[0] for key, value in parse_qs(request.data.decode()).items()}
        self.calls.append((method, params))
        if method == "messages.send":
            if not self.confirm_sends:
                return BytesIO(b'{"response":null}')
            if (serialized := params.get("keyboard")) and not (keyboard := json.loads(serialized))["inline"]:
                self.delivered_menu = {button["action"]["label"]: button["action"].get("payload")
                                       for row in keyboard["buttons"] for button in row}
            self.last_cmid = next(self.messages)
            message_id = 9000 + self.last_cmid
            self.sent_messages[self.last_cmid] = {"id": message_id, "text": params["message"]}
            if "peer_ids" in params:
                response = [{"peer_id": 201, "message_id": message_id, "conversation_message_id": self.last_cmid}]
            else:
                response = message_id
        elif method == "messages.getByConversationMessageId":
            response = {"items": [self.sent_messages[int(params["conversation_message_ids"])]]}
        elif method == "messages.edit":
            cmid = int(params["cmid"])
            if cmid in self.reject_edits:
                return BytesIO(json.dumps({"error": {"error_code": 909, "error_msg": "Cannot edit"}}).encode())
            self.sent_messages[cmid]["text"] = params["message"]
            response = 1
        elif method == "messages.sendMessageEventAnswer":
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
        if self.delivered_menu is None:
            self.fail("Нижнее меню VK не было опубликовано")
        return self.delivered_menu

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

    def test_expense_entry_offers_stepwise_input_in_bottom_menu(self) -> None:
        """VK доставляет запрос чека и выбор способа ввода одним сообщением."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        self._menu_press("Расходы")
        home = next(params for method, params in reversed(self.calls) if method == "messages.send")
        add = self._actions(home)["Добавить расход"]
        self._single_reply(lambda: self._press(add, self.last_cmid), "одно фото чека",
                           labels=["Ввести пошагово", "Отмена", "Помощь"])
        payload = self._visible_menu()["Ввести пошагово"]
        self._single_reply(lambda: self._menu_press("Ввести пошагово"), "название покупки",
                           labels=["Отмена", "Помощь"])
        self._single_reply(lambda: self._send("Ввести пошагово", payload=payload), "устарела")
        self._single_reply(lambda: self._send("Такси"), "сумму в рублях")
        self._send("450")
        categories = next(p for method, p in reversed(self.calls)
                          if method == "messages.send" and "Выбери категорию" in p["message"])
        category_id = next(cmid for cmid, msg in self.sent_messages.items() if msg["text"] == categories["message"])
        self._single_reply(lambda: self._press(self._actions(categories)["Транспорт"], category_id), "Такси",
                           labels=["Сохранить", "Исправить", "Отменить", "Помощь"])
        old_save = self._visible_menu()["Сохранить"]
        with connect_database(self.database) as db, db:
            db.execute("UPDATE family_expense_categories SET hidden=1, revision=revision+1 WHERE name='Транспорт'")
        before = len(self.calls)
        self._menu_press("Сохранить")
        conflict = next(p for method, p in self.calls[before:] if method in ("messages.send", "messages.edit")
                        and "Категория позиции 1 недоступна" in p.get("message", ""))
        conflict_id = next(cmid for cmid, msg in self.sent_messages.items() if msg["text"] == conflict["message"])
        self.assertNotIn("Транспорт", self._actions(conflict))
        self._send("Сохранить", payload=old_save)
        with connect_database(self.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM family_expenses").fetchone()[0], 0)
        self._press(self._actions(conflict)["Дом и быт"], conflict_id)
        self._menu_press("Сохранить")
        with connect_database(self.database) as db:
            self.assertEqual(db.execute("SELECT c.name, i.amount FROM family_expense_items i JOIN "
                                        "family_expense_categories c ON c.id=i.category_id").fetchone()[:],
                             ("Дом и быт", 45000))

    def test_expense_review_uses_versioned_bottom_menu(self) -> None:
        """Фоновый чек и меню приходят вместе; старая кнопка не сохраняет новый чек."""
        from datetime import date
        from threading import Event
        from compadre_assistant.capabilities.family.domain.expenses import ExpenseItem, ExpensePurchase, ExpenseSource

        items = tuple(ExpenseItem("Продукты", f"Товар {index}", 10000) for index in range(8))
        receipt = ExpensePurchase(date(2026, 11, 24), ExpenseSource.TEXT, items)
        self.enterContext(patch("compadre_assistant.bootstrap.ReceiptRecognizer.recognize", return_value=receipt))
        ready = Event()
        deliver = self.processor.deliver_response_menu

        def notify(*args, **kwargs):
            result = deliver(*args, **kwargs)
            if "Сохранить" in (self.delivered_menu or {}):
                ready.set()
            return result

        self.enterContext(patch.object(self.processor, "deliver_response_menu", side_effect=notify))
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        for attempt in range(2):
            self._menu_press("Расходы")
            home = next(params for method, params in reversed(self.calls) if method == "messages.send")
            self._press(self._actions(home)["Добавить расход"], self.last_cmid)
            before = len(self.calls)
            ready.clear()
            self._send("Магазин")
            self.assertTrue(ready.wait(5))
            sent = [p for m, p in self.calls[before:] if m == "messages.send"]
            self.assertEqual(len(sent), 2)  # Статус распознавания и сам чек.
            self.assertIn("Страница 1 из 2", sent[-1]["message"])
            self.assertFalse(json.loads(sent[-1]["keyboard"])["inline"])
            self.assertEqual(set(self._visible_menu()), {"Сохранить", "Исправить", "Отменить", "Помощь",
                                                        "← Ранее", "Далее →"})
            cmid = self.last_cmid
            for button, page in (("Далее →", 2), ("← Ранее", 1)):
                self._menu_press(button)
                self.assertEqual(self.last_cmid, cmid)
                self.assertIn(f"Страница {page} из 2", self.sent_messages[cmid]["text"])
            if attempt == 0:
                old_save = self._visible_menu()["Сохранить"]
                self._menu_press("Исправить")
                self.assertIn("Что исправить?", self.sent_messages[cmid]["text"])
                self._single_reply(lambda: self._menu_press("Отменить"), "Ввод расхода отменён")
            else:
                self._single_reply(lambda: self._send("Сохранить", payload=old_save), "устарела")
                self._single_reply(lambda: self._menu_press("Сохранить"), "Расход сохранён")
            self.assertIn("Расходы", self._visible_menu())
            self.assertNotIn("Сохранить", self._visible_menu())
            with connect_database(self.database) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM family_expenses").fetchone()[0], attempt)

    def test_long_titles_in_list_and_selection(self) -> None:
        """VK показывает сокращение в тексте и сохраняет выбор товара кнопкой."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        self._menu_press("Покупки")
        ordinary = self._single_reply(
            lambda: self._send(f"/add {_TITLE_AT_PREVIEW_LIMIT}\n{_TITLE_OVER_PREVIEW_LIMIT}"),
            "Список покупок", labels=["Добавить", "Купить", "Удалить", "Назад", "Помощь"],
        )
        self.assertEqual(ordinary["message"].splitlines()[3:], _PREVIEW_LINES)

        choice = self._single_reply(lambda: self._menu_press("Купить"), "Выбери, что купил")
        cmid = self.last_cmid
        self.assertEqual(choice["message"].splitlines()[3:5], _PREVIEW_LINES)
        buy = self._actions(choice)
        first, second, back = buy
        self.assertEqual(back, "Назад")
        self.assertTrue(first.startswith("1. "))
        self.assertTrue(second.startswith("2. "))
        self._press(buy[second], cmid)
        self._single_reply(lambda: self._press(self._actions(self._latest_edit())["Назад"], cmid),
                           f"Куплено\n1. {_TITLE_OVER_PREVIEW_LIMIT[:90]}...")

        choice = self._single_reply(lambda: self._menu_press("Удалить"), "Выбери, что удалить")
        cmid = self.last_cmid
        self._press(self._actions(choice)[first], cmid)
        ordinary = self._single_reply(lambda: self._press(self._actions(self._latest_edit())["Назад"], cmid),
                                      "Нужно купить\nСписок пуст.")
        self.assertNotIn(_TITLE_AT_PREVIEW_LIMIT, ordinary["message"])

    def test_long_ordinary_page_reaches_vk_with_menu_on_last_message(self) -> None:
        """Все части списка доходят, а нижнее меню прикрепляется к последней."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        self._menu_press("Покупки")
        items = ["<" * 80, "Яблоки 🍎", "Молоко", "Рис"]
        self._send("/add " + "\n".join(items))
        self._send("/family_shopping home")
        boundary = len(self.calls)
        with patch(
            "compadre_assistant.presentation.vk.bot._safe_send_vk_message",
            partial(_safe_send_vk_message, limit=100),
        ):
            self._menu_press("Покупки")
        sent = [params for method, params in self.calls[boundary:] if method == "messages.send"]
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(part["message"].encode("utf-16-le")) // 2 <= 100 for part in sent))
        joined = "".join(part["message"] for part in sent)
        for position, item in enumerate(items, 1):
            self.assertIn(f"{position}. {item}", joined)
        self.assertTrue(all("keyboard" not in part for part in sent[:-1]))
        self.assertEqual(list(self._visible_menu()), ["Добавить", "Купить", "Удалить", "Назад", "Помощь"])
        self.assertIn("keyboard", sent[-1])

    def test_entire_bought_group_stays_on_last_logical_vk_page(self) -> None:
        """Большой купленный блок не добавляет страниц и сохраняет меню VK."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        bought = _seed_long_bought(self.database, "vk", "201")
        first = self._single_reply(lambda: self._menu_press("Покупки"), "1/2")
        self.assertNotIn("Куплено", first["message"])
        boundary = len(self.calls)
        self._menu_press("Следующая страница")
        sent = [params for method, params in self.calls[boundary:] if method == "messages.send"]
        self.assertGreater(len(sent), 1)
        joined = "".join(part["message"] for part in sent)
        self.assertIn("Список покупок · 2/2\n\nНужно купить\n11. Нужный товар 11\n\nКуплено", joined)
        for position, name in enumerate(bought, 1):
            self.assertIn(f"{position}. {name}", joined)
        self.assertTrue(all("keyboard" not in part for part in sent[:-1]))
        self.assertIn("keyboard", sent[-1])
        menu = self._visible_menu()
        self.assertIn("Предыдущая страница", menu)
        self.assertNotIn("Следующая страница", menu)

    def test_paginated_selection_clear_and_text_command_use_vk_cmid(self) -> None:
        """Страницы и выбор длинных названий помещаются в клавиатуру одного сообщения."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        self._menu_press("Покупки")
        self._send("/add " + "\n".join([">" * 110, *(f"Товар {number}" for number in range(2, 13))]))
        self._single_reply(lambda: self._menu_press("Следующая страница"), "2/2",
                           labels=["Добавить", "Купить", "Удалить", "Предыдущая страница", "Назад", "Помощь"])

        menu_before_selection = self._visible_menu()
        first = self._single_reply(lambda: self._menu_press("Купить"), "3/3")
        cmid = self.last_cmid
        buttons = self._actions(first)
        self.assertEqual(self._visible_menu(), menu_before_selection)
        self.assertNotIn("Следующая страница", buttons)
        self.assertIn("Предыдущая страница", buttons)
        self.assertIn("11. Товар 11", buttons)
        self._press(buttons["Предыдущая страница"], cmid)
        second = self._latest_edit()
        self.assertIn("2/3", second["message"])
        self.assertIn("пришли номер товара", second["message"])
        for duplicate in _SELECTION_TEXT_DUPLICATES:
            self.assertNotIn(duplicate, second["message"])
        rows = [[button["action"]["label"] for button in row] for row in json.loads(second["keyboard"])["buttons"]]
        navigation = ["Предыдущая страница", "Следующая страница"]
        self.assertEqual(rows, [[f"{number}. Товар {number}"] for number in range(5, 9)] + [navigation, ["Назад"]])
        before = len(self.calls)
        self._single_reply(lambda: self._press(buttons["11. Товар 11"], cmid), "больше недоступна")
        self.assertEqual([method for method, _ in self.calls[before:]], ["messages.sendMessageEventAnswer", "messages.send"])
        self._send("8")
        bought = self._latest_edit()
        self.assertNotIn("8. Товар 8", self._actions(bought))
        self.assertNotIn("Куплено", bought["message"])
        self.assertEqual(self._visible_menu(), menu_before_selection)
        self._single_reply(lambda: self._press(self._actions(bought)["Назад"], cmid), "Куплено\n1. Товар 8",
                           labels=["Добавить", "Купить", "Удалить", "Очистить купленное", "Предыдущая страница", "Назад", "Помощь"])
        cleared = self._single_reply(lambda: self._menu_press("Очистить купленное"), "Список покупок")
        self.assertNotIn("Куплено", cleared["message"])
        self.assertNotIn("Очистить купленное", self._visible_menu())
        self.assertTrue(all(params["cmid"] == str(cmid) for method, params in self.calls if method == "messages.edit"))

        delete = self._single_reply(lambda: self._menu_press("Удалить"), "Выбери, что удалить")
        delete_cmid = self.last_cmid
        self.assertNotEqual(delete_cmid, cmid)
        self._press(self._actions(delete)["Предыдущая страница"], delete_cmid)
        delete_page = self._latest_edit()
        delete_rows = json.loads(delete_page["keyboard"])["buttons"]
        self.assertEqual([button["action"]["label"] for button in delete_rows[-2]], navigation)
        self._press(self._actions(delete_page)["Предыдущая страница"], delete_cmid)
        delete = self._latest_edit()
        self.assertTrue(next(label for label in self._actions(delete) if label.startswith("1. ")).endswith("…"))
        self.assertIn(f"1. {'>' * 90}...", delete["message"].splitlines())
        before = len(self.calls)
        self._press(self._actions(delete)["2. Товар 2"], delete_cmid)
        self.assertEqual([method for method, _ in self.calls[before:]], ["messages.sendMessageEventAnswer", "messages.edit"])
        self.assertEqual(self._latest_edit()["cmid"], str(delete_cmid))
        self.assertNotIn("Товар 2", self._latest_edit()["message"])
        self._single_reply(lambda: self._send("/delete 4"), "Открой выбор")
        self._send("/delete")
        self._send("3")
        self._single_reply(lambda: self._menu_press("Назад"), "Список покупок")
        self._single_reply(lambda: self._menu_press("Назад"), "Семья «Дом»")
        published = self._single_reply(lambda: self._send("/family_shopping"), "Список покупок")
        self.assertIn("3. Товар 5", published["message"])
        self.assertNotIn("Товар 4", published["message"])

    def test_all_back_paths_return_to_origin_before_leaving_shopping(self) -> None:
        """Нижнее и экранное «Назад» и ручной back возвращают на исходную страницу."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        self._menu_press("Покупки")
        self._send("/add " + "\n".join(f"Товар {number}" for number in range(1, 13)))
        self._menu_press("Следующая страница")
        original_menu = self._visible_menu()

        for path in ("lower", "inline", "text"):
            with self.subTest(path=path):
                selection = self._single_reply(lambda: self._menu_press("Купить"), "3/3")
                cmid = self.last_cmid
                self.assertEqual(self._visible_menu(), original_menu)
                help_reply = self._single_reply(lambda: self._send("/help"), "/family_shopping back")
                self.assertNotIn("keyboard", help_reply)
                if path == "lower":
                    action = lambda: self._menu_press("Назад")
                elif path == "inline":
                    action = lambda: self._press(self._actions(selection)["Назад"], cmid)
                else:
                    action = lambda: self._send("/family_shopping back")
                ordinary = self._single_reply(action, "Список покупок · 2/2")
                self.assertIn("11. Товар 11", ordinary["message"])
                self.assertNotIn("keyboard", ordinary)
                self.assertEqual(self._visible_menu(), original_menu)

        self._single_reply(lambda: self._menu_press("Назад"), "Семья «Дом»",
                           labels=["Покупки", "Расходы", "Участники", "Пригласить", "Помощь", "Выйти из режима"])

    def test_shopping_without_inline_keyboard_keeps_text_commands(self) -> None:
        """VK сохраняет отдельное меню и подтверждает текстовый выбор по номеру."""
        with patch("compadre_assistant.presentation.vk.bot.VK_INLINE_ACTIONS_ENABLED", False):
            self._send("/family")
            self._send("/family_create")
            self._send("Дом")
            self._menu_press("Покупки")
            self._single_reply(lambda: self._send("/add"), "На ввод 10 минут", labels=["Отмена", "Помощь"])
            self._single_reply(lambda: self._send("/cancel"), "Ввод отменён", labels=["Добавить", "Назад", "Помощь"])
            self._single_reply(lambda: self._send("/add Хлеб\nМолоко"), "1. Хлеб\n2. Молоко")
            previous_menu = self._visible_menu()
            choosing = self._single_reply(lambda: self._send("/buy"), "пришли номер товара")
            self.assertNotIn("keyboard", choosing)
            self._send("1")
            self.assertIn("1. Молоко", self._latest_edit()["message"])
            self.assertEqual(self._visible_menu(), previous_menu)
            ordinary = self._single_reply(lambda: self._send("/family_shopping back"), "Куплено\n1. Хлеб")
            self.assertFalse(json.loads(ordinary["keyboard"])["inline"])
            self.assertIn("Очистить купленное", self._visible_menu())

    def test_family_menu_and_pending_add_use_one_reply(self) -> None:
        """Карточка, участники, приглашение, покупки, подтверждение и отмена не дублируют ответы."""
        no_family = ["Создать семью", "Вступить по коду", "Выйти из режима", "Помощь"]
        home = ["Покупки", "Расходы", "Участники", "Пригласить", "Помощь", "Выйти из режима"]
        shopping = ["Добавить", "Назад", "Помощь"]
        self._single_reply(lambda: self._send("/family"), "Семья", labels=no_family)
        self._single_reply(lambda: self._menu_press("Создать семью"), "Пришли название семьи", labels=["Отмена", "Помощь"])
        self._single_reply(lambda: self._send("Дом"), "Семья «Дом»", labels=home)
        roster = self._single_reply(lambda: self._menu_press("Участники"), "Участники семьи «Дом»")
        self.assertEqual(self._visible_menu().keys(), set(home))
        self.assertIn("Закрыть семью", self._actions(roster))
        invitation = self._single_reply(lambda: self._menu_press("Пригласить"), "Приглашение в семью «Дом»")
        self.assertEqual(list(self._actions(invitation)), ["Создать код", "Назад"])
        self._single_reply(lambda: self._menu_press("Покупки"), "Список покупок пуст", labels=shopping)
        empty = self._single_reply(lambda: self._send("/buy"), "В «Нужно купить» пока ничего нет.")
        self.assertNotIn("/add", empty["message"])
        self.assertEqual(list(self._actions(empty)), ["Назад"])
        self._single_reply(lambda: self._press(self._actions(empty)["Назад"], self.last_cmid), "Список покупок")
        self._single_reply(lambda: self._menu_press("Добавить"), "Напиши, что купить", labels=["Отмена", "Помощь"])
        self._single_reply(lambda: self._menu_press("Отмена"), "Ввод отменён", labels=shopping)
        self._single_reply(lambda: self._menu_press("Назад"), "Семья «Дом»", labels=home)
        confirmation = self._single_reply(lambda: self._send("/family_close"), "Подтверждение действует")
        self.assertEqual(list(self._actions(confirmation)), ["Закрыть семью", "Отменить"])
        self._single_reply(lambda: self._press(self._actions(confirmation)["Отменить"], self.last_cmid), "Действие отменено")
        self._single_reply(lambda: self._menu_press("Выйти из режима"), "Режим этого чата завершён")
        self._single_reply(lambda: self._send("/family"), "Семья «Дом»", labels=home)

    def test_shopping_to_invitation_and_members_keeps_inline_actions_with_new_bottom_menu(self) -> None:
        """VK возвращает нижнее меню семьи вместе с подтверждёнными кнопками нового экрана."""
        home = ["Покупки", "Расходы", "Участники", "Пригласить", "Помощь", "Выйти из режима"]
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        for command, title, inline_labels in (
            ("/family_invite", "Приглашение в семью «Дом»", ["Создать код", "Назад"]),
            ("/family_members", "Участники семьи «Дом»", ["1. Лена", "Закрыть семью", "Назад"]),
        ):
            with self.subTest(command=command):
                self._menu_press("Покупки")
                self.assertEqual(list(self._visible_menu()), ["Добавить", "Назад", "Помощь"])
                previous = len(self.calls)
                self._send(command)
                sent = [params for method, params in self.calls[previous:] if method == "messages.send"]
                self.assertEqual(len(sent), 2)
                self.assertIn(title, sent[0]["message"])
                actions = self._actions(sent[0])
                self.assertEqual(list(actions), inline_labels)
                self.assertIn("Помощь", sent[1]["message"])
                self.assertNotIn("/help", sent[1]["message"])
                self.assertEqual(list(self._visible_menu()), home)
                source = next(cmid for cmid, message in reversed(self.sent_messages.items()) if title in message["text"])
                if command == "/family_invite":
                    self._single_reply(lambda: self._press(actions["Создать код"], source), "Код:")
                else:
                    self._single_reply(lambda: self._press(actions["1. Лена"], source), "Владелец семьи «Дом».")

    def test_join_retry_and_cancel_keep_the_same_waiting_input_path(self) -> None:
        """Отмена по кнопке VK завершает ввод кода и возвращает начальное меню семьи."""
        no_family = ["Создать семью", "Вступить по коду", "Выйти из режима", "Помощь"]
        self._single_reply(lambda: self._send("/family"), "Семья", labels=no_family)
        self._single_reply(lambda: self._menu_press("Вступить по коду"), "На ввод 10 минут", labels=["Отмена", "Помощь"])
        self._single_reply(lambda: self._send("недействительный код"), "Для отмены нажми")
        self.assertEqual(list(self._visible_menu()), ["Отмена", "Помощь"])
        self._single_reply(lambda: self._menu_press("Отмена"), "Ввод отменён", labels=no_family)
        self._single_reply(lambda: self._send("другой текст"), "Семья")

    def test_family_menu_help_and_typed_help_preserve_shopping_context(self) -> None:
        """Кнопка VK открывает справку из семьи, а ручная команда не отменяет выбор."""
        self._send("/family")
        no_family_help = self._single_reply(lambda: self._send("/help"), "Команды режима family")
        self.assertNotIn("/add", no_family_help["message"])
        self.assertIn("Помощь", self._visible_menu())

        self._send("/family_create")
        self._send("Дом")
        home_menu = self._visible_menu()
        home_help = self._single_reply(lambda: self._menu_press("Помощь"), "/family_shopping")
        self.assertNotIn("/add", home_help["message"])
        self.assertEqual(self._visible_menu(), home_menu)
        self._single_reply(lambda: self._send("/add Хлеб"), "Открой список")
        self._single_reply(lambda: self._menu_press("Покупки"), "Список покупок пуст")

        title = "длинное название " * 6
        added = self._single_reply(
            lambda: self._send("/add " + "\n".join(["Молоко", title, *(f"Товар {i}" for i in range(3, 12))])),
            "Список покупок · 1/2",
        )
        for duplicate in _SHOPPING_TEXT_DUPLICATES:
            self.assertNotIn(duplicate, added["message"])
        shopping_menu = self._visible_menu()
        self.assertEqual(list(shopping_menu), ["Добавить", "Купить", "Удалить", "Следующая страница", "Назад", "Помощь"])
        help_from_list = self._single_reply(lambda: self._send("/help"), "Команды режима family")
        for command in _SHOPPING_HELP_COMMANDS:
            self.assertIn(command, help_from_list["message"])
        self.assertEqual(self._visible_menu(), shopping_menu)

        self._single_reply(lambda: self._menu_press("Купить"), "пришли номер товара")
        selection_cmid = self.last_cmid
        self._single_reply(lambda: self._send("/help"), "Ожидается")
        self.assertEqual(self._visible_menu(), shopping_menu)
        self._send("1")
        edited = self._latest_edit()
        self.assertEqual(edited["cmid"], str(selection_cmid))
        self.assertNotIn("Молоко", edited["message"])
        final_page = self._single_reply(lambda: self._send("/family_shopping page 2"), "Куплено\n1. Молоко")
        for duplicate in _SHOPPING_TEXT_DUPLICATES:
            self.assertNotIn(duplicate, final_page["message"])
        self.assertIn("Очистить купленное", self._visible_menu())

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

    def test_add_and_back_retire_vk_choice_after_response_and_expired_press_preserves_current(self) -> None:
        """Старый выбор чистится после send, истёкшая кнопка не чистит текущий cmid."""
        self._send("/family")
        self._send("/family_create")
        self._send("Дом")
        self._menu_press("Покупки")
        self._send("/add Хлеб\nМолоко")
        now = [0.0]
        self.processor._response_actions = ResponseActionStore(ttl_seconds=10, clock=lambda: now[0])
        first = self._single_reply(lambda: self._menu_press("Купить"), "Выбери, что купил")
        first_cmid = self.last_cmid
        old_buy = self._actions(first)["1. Хлеб"]
        before = len(self.calls)
        self._menu_press("Добавить")
        self.assertEqual([method for method, _ in self.calls[before:]],
                         ["messages.send", "messages.getByConversationMessageId", "messages.edit"])
        clear = self._latest_edit()
        self.assertEqual(clear["cmid"], str(first_cmid))
        self.assertEqual(json.loads(clear["keyboard"])["buttons"], [])
        self._single_reply(lambda: self._press(old_buy, first_cmid), "больше недоступна")
        self._single_reply(lambda: self._menu_press("Отмена"), "Ввод отменён")
        self._send("/family_shopping")

        second = self._single_reply(lambda: self._menu_press("Удалить"), "Выбери, что удалить")
        second_cmid = self.last_cmid
        delete = self._actions(second)["1. Хлеб"]
        self.confirm_sends = False
        before = len(self.calls)
        menu_before = self._visible_menu()
        self._single_reply(lambda: self._menu_press("Добавить"), "Напиши, что купить")
        self.assertEqual([method for method, _ in self.calls[before:]], ["messages.send"])
        self.assertEqual(self._visible_menu(), menu_before)
        self.confirm_sends = True
        # Чат уже ждёт добавления. Ручное открытие списка отменяет ввод и снова
        # подтверждает доставку замены, после которой можно снять старую клавиатуру.
        self.reject_edits.add(second_cmid)
        with self.assertLogs(self.logger, level="WARNING"):
            self._single_reply(lambda: self._send("/family_shopping"), "1. Хлеб")
        self._single_reply(lambda: self._press(delete, second_cmid), "больше недоступна")

        third = self._single_reply(lambda: self._menu_press("Купить"), "Выбери, что купил")
        third_cmid = self.last_cmid
        now[0] = 10
        before = len(self.calls)
        self._single_reply(lambda: self._press(self._actions(third)["1. Хлеб"], third_cmid), "больше недоступна")
        self.assertEqual([method for method, _ in self.calls[before:]], ["messages.sendMessageEventAnswer", "messages.send"])
        self._send("/family_shopping")
        self.assertIn("1. Хлеб\n2. Молоко", self.calls[-1][1]["message"])

        fourth = self._single_reply(lambda: self._menu_press("Купить"), "Выбери, что купил")
        fourth_cmid = self.last_cmid
        self.processor = self.enterContext(create_channel_message_processor(
            database_path=self.processor._database_path, enabled_capabilities=("family",),
        ))
        stale = self._actions(fourth)["1. Хлеб"]
        before = len(self.calls)
        self._press(stale, fourth_cmid)
        sent = [params for method, params in self.calls[before:] if method == "messages.send"]
        self.assertIn("больше недоступна", sent[0]["message"])
        self.assertIn("Семья", self._actions(sent[-1]))
        self.assertIn("Помощь", self._visible_menu())
        self.assertEqual([method for method, _ in self.calls[before:]],
                         ["messages.sendMessageEventAnswer", "messages.send", "messages.send"])
