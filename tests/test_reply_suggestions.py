"""Проверяет небольшие следующие действия и их ручной текстовый путь."""

import unittest
from dataclasses import replace
from unittest.mock import Mock

from compadre_assistant.capabilities.ai.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ai.presentation.handler import AiCapability
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.action_contracts import ActionScope
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import HandlerContext, ReplyAction
from compadre_assistant.presentation.telegram.rendering import render_telegram_content
from compadre_assistant.presentation.vk.rendering import render_vk_content
from tests.response_assertions import content_text
from tests.test_contextual_help import ContextualHelpTest
from tests.test_incoming_boundary import _private_event as _event
from tests.test_process_incoming_message import FakeVaultSelectionManager, _test_vault


class ReplySuggestionsTest(unittest.TestCase):
    """Проверяет сценарии ядра, Ask и Obsidian без копирования меню."""

    def test_registration_and_mode_exit_offer_accessible_entries_and_help(self) -> None:
        """Оба перехода показывают те же доступные входы и справку."""
        fixture = ContextualHelpTest()
        fixture.setUp()
        user = AppUser(23, "Имя")
        fixture.identity.resolve.side_effect = [None, None, user, user, user]
        fixture.identity.register_with_name.return_value = user

        event = replace(_event("/register", "123"), external_user_id="123")
        fixture.dispatcher.execute(event)
        registered = fixture.dispatcher.execute(replace(event, text="Имя"))
        self.assertEqual({action.command for action in registered.suggested_actions},
                         {"/obsidian", "/ai", "/other", "/settings", "/help"})
        self.assertNotIn("/help", content_text(registered.content))

        fixture.identity.resolve.side_effect = None
        fixture.identity.resolve.return_value = user
        exited = fixture.dispatcher.execute(replace(event, text="/exit"))
        self.assertEqual(exited.suggested_actions, registered.suggested_actions)
        self.assertNotIn("/help", content_text(exited.content))
        outside = fixture.dispatcher.execute(_event("/status"))
        self.assertEqual({action.command for action in outside.suggested_actions}, {"/settings", "/help"})

    def test_ask_long_question_offers_valid_retry(self) -> None:
        """Слишком длинный вопрос можно ввести заново без аргумента-заполнителя."""
        model = Mock()
        question = AskQuestion(model=model, max_question_chars=5)
        ask = AiCapability(question, Mock())
        response = ask.handle(HandlerContext(AppUser(23, "Имя"), pending_inputs=Mock()),
                              _event("/ask вопрос")).response
        self.assertEqual(response.suggested_actions,
                         (ReplyAction("Задать вопрос", "/ask", scope=ActionScope.SECTION), HELP_SECTION_ACTION))
        self.assertIn("начни ввод заново", content_text(response.content))
        self.assertNotIn("/ask", "".join(part.text for part in render_telegram_content(response.content)))
        self.assertNotIn("/ask", "".join(part.text for part in render_vk_content(response.content)))
        model.answer.assert_not_called()

        model.answer.return_value = "Ответ модели"
        accepted = ask.handle(HandlerContext(AppUser(23, "Имя"), pending_inputs=Mock()), _event("/ask тест"))
        self.assertIn("Вопрос принят", content_text(accepted.response.content))
        model.answer.assert_not_called()
        answered = accepted.cancellable_action(lambda: False)
        self.assertEqual(content_text(answered.content), "Ответ модели")
        self.assertEqual({action.command for action in answered.suggested_actions},
                         {"/ask", "/get_text_from_image", "/exit", "/help"})

    def test_ask_without_pending_input_keeps_only_direct_retry(self) -> None:
        """Без хранилища ожиданий голая команда не предлагается."""
        model = Mock()
        question = AskQuestion(model=model, max_question_chars=5)
        response = AiCapability(question, Mock()).handle(
            HandlerContext(AppUser(23, "Имя")), _event("/ask вопрос"),
        ).response
        self.assertEqual(response.suggested_actions, (HELP_SECTION_ACTION,))
        self.assertNotIn("/ask", content_text(response.content))
        self.assertNotIn("вызови /ask", content_text(response.content))
        model.answer.assert_not_called()

    def test_obsidian_vault_offers_only_available_section_actions(self) -> None:
        """Экран входа и отдельный запрос возвращают действия выбранного хранилища."""
        user = AppUser(23, "Имя")
        selection = FakeVaultSelectionManager(selected=_test_vault(23))
        event = _event("/obsidian")
        context = HandlerContext(user)
        for sync, selected, commands in (
            (Mock(), selection, ("/vault_status", "/vault_sync", "/vault_disconnect", "/exit")),
            (None, selection, ("/vault_disconnect", "/exit")),
            (Mock(), FakeVaultSelectionManager(), ("/exit",)),
            (None, None, ("/exit",)),
        ):
            with self.subTest(commands=commands):
                handler = ObsidianMessageHandler(article_url_use_case=Mock(), vault_selection_manager=selected,
                                                 vault_sync_manager=sync)
                response = handler.enter(context, event, "").response
                self.assertEqual(tuple(action.command for action in response.suggested_actions), commands)
                self.assertEqual(response.suggested_actions, handler.section_actions(user, event.reply_address, 1, None))
                self.assertTrue(all(action.scope == ActionScope.SECTION for action in response.suggested_actions))
                if "/vault_status" in commands:
                    self._assert_command_visible_in_channels(response, "/vault_status")
                self._assert_command_visible_in_channels(response, "/help")
                self.assertEqual(response.actions, ())

    def test_confirmation_actions_stay_separate_from_suggestions(self) -> None:
        """Адресные решения не превращаются в общие следующие шаги."""
        fixture = ContextualHelpTest()
        fixture.setUp()
        fixture.rebind()
        response = fixture.dispatcher.execute(_event("/confirm"))
        self.assertEqual(tuple(action.command for action in response.suggested_actions), ("/help",))
        self.assertEqual(len(response.actions), 2)
        self.assertTrue(all(action.command.startswith("/confirm core identity ")
                            for action in response.actions))
        help_response = fixture.dispatcher.execute(_event("/help"))
        for action in response.actions:
            self.assertNotIn(action.command, content_text(response.content))
            self._assert_command_visible_in_channels(help_response, action.command)

    def _assert_command_visible_in_channels(self, response, command: str) -> None:
        """Проверяет текстовый путь при отсутствии кнопок ответа в каналах."""
        self.assertIn(command, content_text(response.content))
        self.assertIn(command, "".join(part.text for part in render_vk_content(response.content)))
        self.assertIn(command, "".join(part.text for part in render_telegram_content(response.content)))


if __name__ == "__main__":
    unittest.main()
