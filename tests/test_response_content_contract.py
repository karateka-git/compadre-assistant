"""Проверки перехода producers на нейтральное содержимое ответа."""

import unittest

from compadre_assistant.core.presentation.incoming.ports import (
    ConfirmationAction, IncomingResponse,
)
from compadre_assistant.core.presentation.message_content import (
    BulletList, ListItem, MessageContent, Paragraph, TextSpan, TextStyle,
    code_span, compose_content, join_content, text_content,
)
from compadre_assistant.capabilities.obsidian.presentation.processing import (
    ObsidianMessageResult, _present_result,
)
from compadre_assistant.capabilities.obsidian.presentation.results import ObsidianResultType
from tests.response_assertions import content_text
from compadre_assistant.presentation.telegram.rendering import render_telegram_content
from compadre_assistant.presentation.vk.rendering import render_vk_content
from tests.test_message_rendering import _HtmlReader


class ResponseContentContractTest(unittest.TestCase):
    """Защищает буквальные данные, структуру и канальную доставку действий."""

    def test_nested_content_preserves_styles_and_block_boundaries(self) -> None:
        """Объединение оставляет списки, команды и данные в исходном порядке."""
        command = '/confirm obsidian proposal 42 7 yes'
        body = MessageContent((
            Paragraph((TextSpan('<b>Данные</b> & `текст`'),)),
            BulletList((ListItem((code_span(command),)),)),
            Paragraph((TextSpan('Итог', TextStyle.EMPHASIS),)),
        ))
        result = join_content('\n', (body, text_content('Следующий шаг')))
        self.assertEqual(result.blocks[0], body.blocks[0])
        self.assertEqual(result.blocks[1], body.blocks[1])
        self.assertEqual(result.blocks[2].spans[0], body.blocks[2].spans[0])
        self.assertEqual(
            content_text(result),
            '<b>Данные</b> & `текст`\n\n• ' + command + '\n\nИтог\nСледующий шаг',
        )

    def test_channel_renderers_preserve_content_and_addressed_actions(self) -> None:
        """Runtime сохраняет полные команды yes/no после всех блоков."""
        actions = tuple(
            ConfirmationAction(choice, label, f'/confirm obsidian replace 17 3 {choice}')
            for choice, label in (('yes', 'Подтвердить'), ('no', 'Отменить'))
        )
        content = MessageContent((
            Paragraph((TextSpan('Заголовок', TextStyle.EMPHASIS),)),
            BulletList((ListItem((TextSpan('пункт'),)),)),
        ))
        response = IncomingResponse(content, actions=actions)
        expected = content_text(content) + '\n\n' + '\n\n'.join(
            f'{action.label}: {action.command}' for action in actions
        )
        telegram = render_telegram_content(response.content, response.actions)
        vk = render_vk_content(response.content, response.actions)
        outputs = (
            "".join("".join(_HtmlReader(part.text).parts) for part in telegram),
            "".join(part.text for part in vk),
        )
        for output in outputs:
            self.assertEqual(output, expected)
            for action in actions:
                self.assertEqual(output.count(action.command), 1)

    def test_override_and_error_survive_presentation(self) -> None:
        """Override передаётся буквально, включая пустое значение, и сохраняет error."""
        error = ValueError('technical detail')
        for override in ('', '<b>данные</b>\n```diff\n+ `x` & y\n```'):
            with self.subTest(override=override):
                response = _present_result(ObsidianMessageResult(
                    type=ObsidianResultType.CONFIRMATION_MISSING,
                    text_override=override, error=error,
                ))
                self.assertIs(response.error, error)
                self.assertEqual(content_text(response.content), override)
                self.assertEqual(response.content, text_content(override))

    def test_dynamic_markdown_remains_plain_when_composed_with_code(self) -> None:
        """Разметка пользователя не превращается в стиль или команды бота."""
        data = '<code>/exit</code> & **жирный**\n```diff\n+ текст\n```'
        result = compose_content('Ответ: ', text_content(data), '\n', code_span('/help'))
        self.assertEqual(content_text(result), 'Ответ: ' + data + '\n/help')
        self.assertEqual(result.blocks[0].spans[1], TextSpan(data))
        self.assertEqual(result.blocks[0].spans[-1], TextSpan('/help', TextStyle.CODE))
        telegram = render_telegram_content(result, ())
        vk = render_vk_content(result, ())
        self.assertEqual(
            ''.join(''.join(_HtmlReader(part.text).parts) for part in telegram),
            'Ответ: ' + data + '\n/help',
        )
        self.assertEqual(''.join(part.text for part in vk), 'Ответ: ' + data + '\n/help')


if __name__ == '__main__':
    unittest.main()
