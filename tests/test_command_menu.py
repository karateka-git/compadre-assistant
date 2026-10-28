"""Матрица меню и независимость действий ответа от полного каталога."""

from dataclasses import replace
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from compadre_assistant.bootstrap import ChannelMessageProcessor
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.chat_modes import ChatMode
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CommandParameter, ConfirmationQueryResult, IncomingResponse,
    ReplyAction,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.message_content import text_content
from tests import test_contextual_help as help_tests
from tests.test_incoming_boundary import _event
from tests.response_assertions import response_text


class CommandMenuTest(unittest.TestCase):
    """Проверяет меню через публичный вход с настоящими правилами допуска."""

    def setUp(self):
        """Использует ту же матрицу профиля и каталога, что текстовая справка."""
        self.fixture = help_tests.ContextualHelpTest()
        self.fixture.setUp()

    def menu(self, chat="chat-1", channel="telegram"):
        """Читает меню без исполнения текста события."""
        return self.fixture.dispatcher.command_menu(_event("/exit", chat, channel))

    def commands(self, **kwargs):
        """Выделяет точные команды для проверки состава меню."""
        return tuple(item.command for item in self.menu(**kwargs).items)

    def test_unknown_registration_and_link_overlay(self):
        """Регистрация заменяет меню, а ввод кода сохраняет исходные два входа."""
        fixture = self.fixture
        fixture.identity.resolve.return_value = None
        self.assertEqual(self.commands(), ("/register", "/link"))
        fixture.send("/register")
        self.assertEqual(self.commands(), ("/cancel",))
        self.assertEqual(self.menu().pending_input_label, "имя или название профиля")
        fixture.send("/link")
        self.assertEqual(self.commands(), ("/register", "/link", "/cancel"))
        self.assertEqual(self.menu().pending_input_label, "код привязки")
        fixture.identity.register_with_name.assert_not_called()

    def test_main_uses_catalog_and_executable_input_entries(self):
        """Параметры не становятся заполнителями, недоступные входы скрыты."""
        fixture = self.fixture
        commands = self.commands()
        for command in ("/name", "/enable", "/disable", "/link", "/ask", "/obsidian", "/other"):
            self.assertIn(command, commands)
        self.assertNotIn("/reanalyze", commands)
        self.assertNotIn("/confirm", commands)
        self.assertNotIn("/cancel", commands)
        self.assertTrue(all(" " not in command for command in commands))
        self.assertEqual(len(commands), len(set(commands)))
        fixture.preferences.rows[(23, "other")] = False
        for command, kind in (("/name", "profile_name"), ("/enable", "enable"), ("/disable", "disable"), ("/link", "link_code")):
            fixture.send(command)
            self.assertEqual(fixture.store.get(_event("x").reply_address).input_kind, kind)
        fixture.preferences.rows[(23, "other")] = False
        self.assertNotIn("/other", self.commands())
        fixture.registry = CapabilityRegistry((fixture.obsidian,), (fixture.ask.descriptor,))
        fixture.dispatcher = fixture.build()
        self.assertNotIn("/ask", self.commands())

    def test_active_mode_is_subject_commands_exit_and_effective_ask(self):
        """Меню режима не копирует общие указатели справки или другие входы."""
        fixture = self.fixture
        fixture.modes.save(ChatMode(fixture.key, "obsidian"))
        self.assertEqual(self.commands(), ("/github_status", "/github_sync", "/github_disconnect", "/reanalyze", "/exit", "/ask"))
        self.assertEqual(fixture.modes.get(fixture.key).capability_id, "obsidian")
        fixture.send("/ask")
        self.assertIn("/cancel", self.commands())
        self.assertEqual(self.menu().pending_input_label, "вопрос для Ask")
        self.assertNotIn("/cancel", self.commands(chat="other"))
        self.assertNotIn("/reanalyze", self.commands(channel="vk"))
        fixture.preferences.rows[(23, "ask")] = False
        self.assertNotIn("/ask", self.commands())
        self.assertNotIn("/cancel", self.commands())

    def test_unavailable_modes_only_offer_exit_and_alternatives(self):
        """Недоступный или устаревший режим не выглядит активной возможностью."""
        fixture = self.fixture
        for mode in ("obsidian", "missing", "ask"):
            with self.subTest(mode=mode):
                fixture.modes.save(ChatMode(fixture.key, mode))
                fixture.preferences.rows[(23, "obsidian")] = False
                expected = ("/exit", "/other") if mode == "ask" else ("/exit", "/ask", "/other")
                self.assertEqual(self.commands(), expected)

    def test_pending_expiration_and_context_validation_match_help(self):
        """Чтение меню не продлевает ввод, неверный контекст удаляет отмену."""
        fixture = self.fixture
        fixture.modes.save(ChatMode(fixture.key, "obsidian"))
        for invalidation in ("ttl", "mode", "access"):
            with self.subTest(invalidation=invalidation):
                fixture.preferences.rows[(23, "obsidian")] = True
                fixture.modes.save(ChatMode(fixture.key, "obsidian"))
                pending = fixture.store.begin(_event("x").reply_address, "obsidian", "article",
                    app_user_id=23, required_mode="obsidian", value_label="ID статьи")
                self.assertIn("/cancel", self.commands())
                self.assertEqual(fixture.store.get(pending.chat_key), pending)
                if invalidation == "ttl": fixture.now += timedelta(minutes=10)
                elif invalidation == "mode": fixture.modes.clear(fixture.key)
                else: fixture.preferences.rows[(23, "obsidian")] = False
                self.assertNotIn("/cancel", self.commands())
                self.assertIsNone(self.menu().pending_input_label)
                self.assertIsNone(fixture.store.get(pending.chat_key))

    def test_confirm_requires_registered_complete_nonempty_snapshot(self):
        """Частичный снимок сохраняет адресные действия ответа, но не пункт меню."""
        fixture = self.fixture
        fixture.modes.save(ChatMode(fixture.key, "obsidian"))
        self.assertNotIn("/confirm", self.commands())
        fixture.rebind()
        self.assertIn("/confirm", self.commands())
        fixture.obsidian.query.return_value = ConfirmationQueryResult("unavailable")
        self.assertNotIn("/confirm", self.commands())
        response = fixture.dispatcher.execute(_event("/help"))
        self.assertEqual(tuple(action.command for action in response.actions), (
            "/confirm core identity 4 1 yes", "/confirm core identity 4 1 no"))
        self.assertEqual(response.suggested_actions, ())
        fixture.obsidian.query.side_effect = RuntimeError("source unavailable")
        with self.assertLogs("compadre_assistant.core.presentation.incoming.dispatcher", level="ERROR"):
            self.assertNotIn("/confirm", self.commands())
        fixture.identity.resolve.return_value = None
        self.assertNotIn("/confirm", self.commands())
        fixture.identity.confirm_rebind.assert_not_called()

    def test_required_parameter_without_input_path_is_not_an_executable_item(self):
        """Меню не выдаёт обязательный заполнитель за готовую команду."""
        fixture = self.fixture
        fixture.other._descriptor = replace(fixture.other.descriptor, entry=CapabilityCommand(
            "/other", (CommandParameter("значение", "<значение>", True),), "требует значения"))
        fixture.registry = CapabilityRegistry((fixture.other,))
        fixture.dispatcher = fixture.build()
        self.assertNotIn("/other", self.commands())
        self.assertIn("/other <значение>", fixture.send())

    def test_response_actions_are_explicit_and_keep_exact_text_commands(self):
        """Сборка меню не меняет ответы и не копирует подтверждения в подсказки."""
        fixture = self.fixture
        fixture.rebind()
        confirmation = fixture.dispatcher.execute(_event("/confirm"))
        suggested = ReplyAction("Показать состояние", "/status")
        response = IncomingResponse(text_content("Проверить: /status"),
            actions=confirmation.actions, suggested_actions=(suggested,))
        self.menu()
        self.assertEqual(response.suggested_actions, (suggested,))
        self.assertEqual(response.actions, confirmation.actions)
        self.assertEqual(IncomingResponse(text_content("Готово")).suggested_actions, ())
        self.assertEqual(fixture.dispatcher.execute(_event("/status")).suggested_actions, ())


class CommandMenuUpdateTest(unittest.TestCase):
    """Проверяет нейтральную передачу только изменившегося меню исходного чата."""

    def setUp(self):
        """Создаёт доступные возможности и отдельные чаты одного профиля."""
        self.fixture = help_tests.ContextualHelpTest()
        self.fixture.setUp()
        self.updates = []

    def send(self, text, chat="chat-1", channel="telegram", handler=None):
        """Исполняет сообщение с наблюдаемым каналовым callback меню."""
        return self.fixture.dispatcher.execute(_event(text, chat, channel),
            menu_update_handler=handler or self.updates.append)

    def test_pending_replacement_completion_cancel_and_unchanged_actions(self):
        """Создание, замена и закрытие ввода меняют меню один раз на переход."""
        self.send("/link")
        self.assertEqual(len(self.updates), 1)
        self.assertIn("/cancel", [item.command for item in self.updates[-1].menu.items])
        self.send("/help")
        self.send("/link")
        self.assertEqual(len(self.updates), 1)
        self.send("/name")
        self.assertEqual(len(self.updates), 2)
        self.assertEqual(self.updates[-1].menu.pending_input_label, "имя")
        self.send("/cancel")
        self.assertEqual(len(self.updates), 3)
        self.assertNotIn("/cancel", [item.command for item in self.updates[-1].menu.items])
        self.send("/ask")
        self.assertEqual(len(self.updates), 4)
        self.send("вопрос")
        self.assertEqual(len(self.updates), 5)

    def test_mode_and_preference_update_only_source_chat(self):
        """Режим и личный выбор передают адрес исходного чата без соседнего."""
        self.send("/other")
        self.assertEqual(self.updates[-1].address, _event("x").reply_address)
        self.assertIn("/exit", [item.command for item in self.updates[-1].menu.items])
        self.assertNotIn("/name", [item.command for item in self.updates[-1].menu.items])
        self.send("/exit")
        self.send("/disable ask")
        self.assertNotIn("/ask", [item.command for item in self.updates[-1].menu.items])
        count = len(self.updates)
        self.send("/status", chat="other")
        self.assertEqual(len(self.updates), count)
        self.assertTrue(all(update.address == _event("x").reply_address for update in self.updates))

    def test_registration_and_link_recompute_menu(self):
        """Появление профиля после регистрации и привязки открывает основной каталог."""
        fixture = self.fixture
        current = [None]
        fixture.identity.resolve.side_effect = lambda _identity: current[0]
        def register(_identity, *, display_name):
            current[0] = AppUser(23, display_name)
            return current[0]
        fixture.identity.register_with_name.side_effect = register
        self.send("/register")
        self.assertEqual([item.command for item in self.updates[-1].menu.items], ["/cancel"])
        self.send("Имя")
        self.assertIn("/obsidian", [item.command for item in self.updates[-1].menu.items])
        current[0] = None
        def link(*, code, identity):
            current[0] = AppUser(23, "Имя")
            return current[0]
        fixture.identity.link.side_effect = link
        self.send("/link ABC")
        self.assertEqual(self.updates[-1].address, _event("x").reply_address)

    def test_delivery_failure_keeps_command_and_help_text(self):
        """Исключение адаптера не отменяет команду и не повреждает /help."""
        def fail(_update):
            raise RuntimeError("menu transport unavailable")

        with self.assertLogs("compadre_assistant.core.presentation.incoming.dispatcher", level="ERROR"):
            response = self.send("/other", handler=fail)
        self.assertIn("Актуальное состояние", response_text(response))
        self.assertEqual(self.fixture.modes.get(self.fixture.key).capability_id, "other")
        self.assertIn("/exit", response_text(self.send("/help", handler=fail)))

    def test_pre_read_preserves_expired_input_response(self):
        """Предварительный снимок меню не перехватывает первое сообщение после TTL."""
        fixture = self.fixture
        fixture.store.begin(_event("x").reply_address, "core", "link_code",
            app_user_id=23, retry_command="/link")
        fixture.now += timedelta(minutes=10)
        response = self.send("поздний код")
        self.assertIn("Срок ожидания ввода истёк", response_text(response))
        self.assertIsNone(fixture.store.get(_event("x").reply_address))
        self.assertFalse(fixture.other.calls)
        self.assertEqual(len(self.updates), 1)
        self.assertNotIn("/cancel", [item.command for item in self.updates[-1].menu.items])

    def test_pre_read_preserves_invalid_context_response(self):
        """Смена режима и доступа остаётся видна владельцу предметного ввода."""
        fixture = self.fixture
        fixture.modes.save(ChatMode(fixture.key, "obsidian"))
        fixture.store.begin(_event("x").reply_address, "obsidian", "article",
            app_user_id=23, required_mode="obsidian")
        fixture.modes.clear(fixture.key)
        response = self.send("значение")
        self.assertIn("Контекст ввода изменился", response_text(response))
        self.assertIsNone(fixture.store.get(_event("x").reply_address))
        self.assertEqual(len(self.updates), 1)
        self.assertNotIn("/cancel", [item.command for item in self.updates[-1].menu.items])

        fixture.modes.save(ChatMode(fixture.key, "obsidian"))
        fixture.store.begin(_event("x").reply_address, "obsidian", "article",
            app_user_id=23, required_mode="obsidian")
        fixture.preferences.rows[(23, "obsidian")] = False
        response = self.send("значение")
        self.assertIn("лично выключена", response_text(response))
        self.assertIsNone(fixture.store.get(_event("x").reply_address))
        self.assertEqual(len(self.updates), 2)
        self.assertNotIn("/cancel", [item.command for item in self.updates[-1].menu.items])


class ChannelMenuDeliveryTest(unittest.TestCase):
    """Проверяет внешнюю доставку меню после короткого SQLite scope."""

    def test_processor_delivers_after_scope_and_preserves_reply_on_failure(self):
        """Ожидание регистрации передаётся после закрытия scope, ошибка не прячет текст."""
        with TemporaryDirectory(prefix="menu-delivery-") as directory:
            database_path = Path(directory) / "app.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
            processor = ChannelMessageProcessor(database_path=database_path, capabilities=())
            scope_active = [False]

            @contextmanager
            def tracked_connection(path):
                with connect_database(path) as connection:
                    scope_active[0] = True
                    try:
                        yield connection
                    finally:
                        scope_active[0] = False

            updates = []
            def receive(update):
                self.assertFalse(scope_active[0])
                updates.append(update)

            with patch("compadre_assistant.bootstrap.connect_database", tracked_connection):
                response = processor(_event("/register"), menu_update_handler=receive)
                self.assertIn("имя", response_text(response))
                self.assertEqual(len(updates), 1)
                self.assertEqual(updates[0].address, _event("x").reply_address)
                self.assertEqual([item.command for item in updates[0].menu.items], ["/cancel"])

                def fail(update):
                    self.assertFalse(scope_active[0])
                    self.assertNotIn("/cancel", [item.command for item in update.menu.items])
                    raise RuntimeError("menu transport unavailable")

                with self.assertLogs("compadre_assistant.bootstrap", level="ERROR"):
                    response = processor(_event("/cancel"), menu_update_handler=fail)
                self.assertIn("Ввод отменён", response_text(response))


if __name__ == "__main__":
    unittest.main()
