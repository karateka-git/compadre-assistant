"""Проверки нейтрального содержания ответа и текстового представления действий."""

import unittest

from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction
from compadre_assistant.core.presentation.message_content import (
    TextSpan,
    TextStyle,
    confirmation_action_paragraph,
)


class MessageContentTest(unittest.TestCase):
    """Проверяет буквальные данные и полные адресные команды действий."""

    def test_confirmation_preserves_full_addressed_command(self) -> None:
        """Каждый вариант содержит исходную адресную команду без сокращений и тегов."""
        for choice in ("yes", "no"):
            with self.subTest(choice=choice):
                command = f"/confirm obsidian disconnect 42 3 {choice}"
                action = ConfirmationAction(choice, "Выбрать", command)

                paragraph = confirmation_action_paragraph(action)

                self.assertEqual("".join(span.text for span in paragraph.spans), f"Выбрать: {command}")
                self.assertEqual(paragraph.spans[-1], TextSpan(command, TextStyle.CODE))

    def test_action_does_not_parse_or_normalize_label_and_command(self) -> None:
        """Даже похожие на разметку подпись и параметры действия остаются данными."""
        label = '<b>Выбрать</b> & `да`'
        command = '/confirm example "<id>&" `version` yes'

        paragraph = confirmation_action_paragraph(ConfirmationAction("id", label, command))

        self.assertEqual("".join(span.text for span in paragraph.spans), f"{label}: {command}")
        self.assertEqual(paragraph.spans[0].text, label)
        self.assertEqual(paragraph.spans[-1], TextSpan(command, TextStyle.CODE))


if __name__ == "__main__":
    unittest.main()
