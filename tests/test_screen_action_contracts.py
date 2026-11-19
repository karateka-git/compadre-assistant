"""Проверяет нейтральное описание действий до привязки и проекции каналом."""

import unittest

from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionLayoutLimits, ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.ports import (
    IncomingResponse, ReplyAction, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.message_content import text_content


class ScreenActionContractsTest(unittest.TestCase):
    """Проверяет три формы ответа и границы интерпретации действий."""

    def test_main_menu_and_separate_query_share_the_same_action_description(self):
        """Запрос меню читает те же цели, не вызывая обработчик сообщения."""
        actions = (ReplyAction("Открыть", "/open", scope=ActionScope.SECTION),)

        class Owner(SectionActionsQueryPort):
            """Тестовый владелец возвращает одно описание доступного раздела."""

            def section_actions(self, app_user, address, mode_revision, pending):
                """Читает готовый набор без изменения состояния."""
                return actions

        response = IncomingResponse(text_content("Выбери раздел. /open"), suggested_actions=actions)
        self.assertIs(Owner().section_actions(None, None, 1, None), response.suggested_actions)
        self.assertEqual(response.suggested_actions[0].scope, ActionScope.SECTION)

    def test_addressed_choice_and_back_keep_the_shown_target_and_page(self):
        """Номер для человека не подменяет устойчивую цель выбора."""
        response = IncomingResponse(text_content("11. Позиция. Выбери номер."), suggested_actions=(
            ReplyAction("11. Позиция", "/choose stable-id", purpose=ActionPurpose.SELECTION),
            ReplyAction("Назад", "/list page 2", purpose=ActionPurpose.NAVIGATION,
                        destination=ScreenDestination("list", 2)),
        ), command_menu_delivery=SectionMenuDelivery.PRESERVE)
        self.assertEqual(response.suggested_actions[0].command, "/choose stable-id")
        self.assertEqual(response.suggested_actions[1].destination, ScreenDestination("list", 2))
        self.assertTrue(all(action.scope == ActionScope.SCREEN for action in response.suggested_actions))

    def test_text_without_actions_does_not_request_a_keyboard(self):
        """Обычный текст не создаёт действия и допускает сохранение меню."""
        response = IncomingResponse(text_content("Готово."), command_menu_delivery=SectionMenuDelivery.PRESERVE)
        self.assertEqual(response.suggested_actions, ())
        self.assertEqual(response.actions, ())
        self.assertEqual(response.bound_actions, ())

    def test_empty_section_snapshot_and_preserve_are_distinct(self):
        """Сверка пустого раздела снимает меню, сохранение не читает снимок."""
        self.assertNotEqual(SectionMenuDelivery.RECONCILE, SectionMenuDelivery.PRESERVE)
        response = IncomingResponse(text_content("Раздел закрыт."),
                                    command_menu_delivery=SectionMenuDelivery.RECONCILE)
        self.assertEqual(response.suggested_actions, ())
        self.assertIs(response.command_menu_delivery, SectionMenuDelivery.RECONCILE)

    def test_link_target_is_not_an_executable_command(self):
        """Ссылка открывает диалог, не конкурируя с командной целью."""
        action = ReplyAction("Открыть бота", url="https://example.com/bot")
        self.assertEqual(action.command, "")
        self.assertEqual(action.url, "https://example.com/bot")
        for url in ("javascript:alert(1)", "/relative", "https://"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ReplyAction("Открыть", url=url)
        with self.assertRaises(ValueError):
            ReplyAction("Открыть", "/execute", url="https://example.com")
        with self.assertRaises(ValueError):
            ReplyAction("Открыть")

    def test_selection_cannot_lose_its_screen_and_navigation_requires_destination(self):
        """Ошибочное описание не переносит адресный выбор в меню раздела."""
        with self.assertRaises(ValueError):
            ReplyAction("Выбрать", "/choose id", scope=ActionScope.SECTION, purpose=ActionPurpose.SELECTION)
        with self.assertRaises(ValueError):
            ReplyAction("Назад", "/back", purpose=ActionPurpose.NAVIGATION)
        with self.assertRaises(ValueError):
            ReplyAction("Выбрать", "/choose id", destination=ScreenDestination("list"))
        for page in (0, -1, True, 1.5):
            with self.subTest(page=page), self.assertRaises(ValueError):
                ScreenDestination("list", page)

    def test_page_capacity_reserves_navigation_without_changing_item_numbers(self):
        """Владелец заранее оставляет два ряда и три кнопки переходов."""
        limits = ActionLayoutLimits(max_buttons=30, max_rows=6, max_columns=5, max_label_length=40)
        capacity = limits.selection_capacity(reserved_rows=2, reserved_buttons=3)
        numbered_targets = tuple((number, f"target-{number}") for number in range(11, 21))
        self.assertEqual(capacity, 4)
        self.assertEqual(numbered_targets[:capacity],
                         ((11, "target-11"), (12, "target-12"), (13, "target-13"), (14, "target-14")))
        self.assertEqual(limits.selection_capacity(reserved_rows=7), 0)
        self.assertEqual(limits.selection_capacity(reserved_buttons=31), 0)
        self.assertEqual(ActionLayoutLimits(3, 10, 5, 40).selection_capacity(reserved_buttons=2), 1)

    def test_unavailable_buttons_leave_page_size_to_the_manual_scenario(self):
        """Нулевая вместимость не удаляет подготовленный текстовый путь."""
        limits = ActionLayoutLimits()
        response = IncomingResponse(text_content("Введи номер позиции или /cancel."))
        self.assertEqual(limits.selection_capacity(), 0)
        self.assertEqual(response.suggested_actions, ())
        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ActionLayoutLimits(max_rows=invalid)
            with self.subTest(reserve=invalid), self.assertRaises(ValueError):
                limits.selection_capacity(reserved_rows=invalid)


if __name__ == "__main__":
    unittest.main()
