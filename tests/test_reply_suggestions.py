"""Проверяет небольшие следующие действия и их ручной текстовый путь."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from compadre_assistant.capabilities.ask.domain.errors import QuestionTooLongError
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import VaultConfirmationAction
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.ports import HandlerContext, ReplyAction
from compadre_assistant.presentation.telegram.rendering import render_telegram_content
from compadre_assistant.presentation.vk.rendering import render_vk_content
from tests.response_assertions import content_text
from tests.test_contextual_help import ContextualHelpTest
from tests.test_incoming_boundary import _event
from tests.test_process_incoming_message import _test_vault


class ReplySuggestionsTest(unittest.TestCase):
    """Проверяет сценарии ядра, Ask и Obsidian без копирования меню."""

    def test_registration_and_mode_exit_offer_only_help(self) -> None:
        """Оба перехода дают исполнимую справку с видимой командой."""
        fixture = ContextualHelpTest()
        fixture.setUp()
        user = AppUser(23, "Имя")
        fixture.identity.resolve.side_effect = [None, None, user, user, user]
        fixture.identity.register_with_name.return_value = user

        fixture.dispatcher.execute(_event("/register"))
        registered = fixture.dispatcher.execute(_event("Имя"))
        self.assertEqual(registered.suggested_actions, (ReplyAction("Посмотреть команды", "/help"),))
        self._assert_command_visible_in_channels(registered, "/help")

        fixture.identity.resolve.side_effect = None
        fixture.identity.resolve.return_value = user
        exited = fixture.dispatcher.execute(_event("/exit"))
        self.assertEqual(exited.suggested_actions, (ReplyAction("Посмотреть команды", "/help"),))
        self._assert_command_visible_in_channels(exited, "/help")
        self.assertEqual(fixture.dispatcher.execute(_event("/status")).suggested_actions, ())

    def test_ask_long_question_offers_valid_retry(self) -> None:
        """Слишком длинный вопрос можно ввести заново без аргумента-заполнителя."""
        question = Mock()
        question.execute.side_effect = QuestionTooLongError("Слишком длинный вопрос")
        ask = AskCapability(question)
        response = ask.enter(HandlerContext(AppUser(23, "Имя"), pending_inputs=Mock()),
                             _event("/ask вопрос"), "вопрос").response
        self.assertEqual(response.suggested_actions, (ReplyAction("Ввести вопрос заново", "/ask"),))
        self.assertIn("вызови /ask", content_text(response.content))
        self._assert_command_visible_in_channels(response, "/ask")

        question.execute.side_effect = None
        question.execute.return_value = "Ответ модели"
        answered = ask.enter(HandlerContext(AppUser(23, "Имя"), pending_inputs=Mock()),
                             _event("/ask вопрос"), "вопрос").response
        self.assertEqual(answered.suggested_actions, ())

    def test_ask_without_pending_input_keeps_only_direct_retry(self) -> None:
        """Без хранилища ожиданий голая команда не предлагается."""
        question = Mock()
        question.execute.side_effect = QuestionTooLongError("Слишком длинный вопрос")
        response = AskCapability(question).enter(
            HandlerContext(AppUser(23, "Имя")), _event("/ask вопрос"), "вопрос",
        ).response
        self.assertEqual(response.suggested_actions, ())
        self.assertIn("/ask <вопрос>", content_text(response.content))
        self.assertNotIn("вызови /ask", content_text(response.content))

    def test_obsidian_vault_offers_status_only_when_available(self) -> None:
        """Подключённый vault предлагает одну работающую команду состояния."""
        selection = Mock()
        selection.get_selected.return_value = _test_vault(23)
        selection.get_pending.return_value = None
        handler = ObsidianMessageHandler(article_url_use_case=Mock(),
            vault_selection_manager=selection, vault_sync_manager=Mock())
        context = HandlerContext(AppUser(23, "Имя"))
        response = handler.enter(context, _event("/obsidian"), "").response
        self.assertEqual(response.suggested_actions,
                         (ReplyAction("Проверить состояние vault", "/github_status"),))
        self._assert_command_visible_in_channels(response, "/github_status")
        self.assertEqual(response.actions, ())

        handler = ObsidianMessageHandler(article_url_use_case=Mock(),
            vault_selection_manager=selection)
        self.assertEqual(handler.enter(context, _event("/obsidian"), "").response.suggested_actions, ())
        selection.get_selected.return_value = None
        handler = ObsidianMessageHandler(article_url_use_case=Mock(),
            vault_selection_manager=selection, vault_sync_manager=Mock())
        self.assertEqual(handler.enter(context, _event("/obsidian"), "").response.suggested_actions, ())

        selection.get_selected.return_value = _test_vault(23)
        selection.get_pending.return_value = SimpleNamespace(
            action=VaultConfirmationAction.DISCONNECT, id=7, version=2)
        pending = handler.enter(context, _event("/obsidian"), "").response
        self.assertEqual(pending.suggested_actions, ())
        self.assertEqual(tuple(action.command for action in pending.actions),
                         ("/confirm obsidian disconnect 7 2 yes",
                          "/confirm obsidian disconnect 7 2 no"))

    def test_confirmation_actions_stay_separate_from_suggestions(self) -> None:
        """Адресные решения не превращаются в общие следующие шаги."""
        fixture = ContextualHelpTest()
        fixture.setUp()
        fixture.rebind()
        response = fixture.dispatcher.execute(_event("/confirm"))
        self.assertEqual(response.suggested_actions, ())
        self.assertEqual(len(response.actions), 2)
        self.assertTrue(all(action.command.startswith("/confirm core identity ")
                            for action in response.actions))
        self.assertEqual(fixture.dispatcher.execute(_event("/help")).suggested_actions, ())

    def _assert_command_visible_in_channels(self, response, command: str) -> None:
        """Проверяет текстовый путь при отсутствии кнопок ответа в каналах."""
        self.assertIn(command, content_text(response.content))
        self.assertIn(command, "".join(part.text for part in render_vk_content(response.content, response.actions)))
        self.assertIn(command, "".join(part.text for part in render_telegram_content(response.content, response.actions)))


if __name__ == "__main__":
    unittest.main()
