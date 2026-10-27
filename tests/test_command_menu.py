"""Матрица меню и независимость действий ответа от полного каталога."""

from dataclasses import replace
from datetime import timedelta
import unittest

from compadre_assistant.core.domain.chat_modes import ChatMode
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CommandParameter, ConfirmationQueryResult, IncomingResponse,
    ReplyAction,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.message_content import text_content
from tests import test_contextual_help as help_tests
from tests.test_incoming_boundary import _event


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


if __name__ == "__main__":
    unittest.main()
