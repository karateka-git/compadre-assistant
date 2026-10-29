"""Проверки содержимого, границ и оформления чистых renderer каналов."""

from html.parser import HTMLParser
import json
import unittest

from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction
from compadre_assistant.core.presentation.message_content import (
    BulletList, ListItem, MessageContent, Paragraph, TextSpan, TextStyle,
)
from compadre_assistant.presentation.telegram.rendering import render_telegram_content
from compadre_assistant.presentation.vk.rendering import render_vk_content


class _HtmlReader(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.parts: list[str] = []
        self.tags: list[str] = []
        self.feed(text)
        self.close()
        assert not self.stack

    def handle_starttag(self, tag: str, attrs: list) -> None:
        assert tag in {"b", "code"} and not attrs
        self.stack.append(tag)
        self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _content(*spans: TextSpan) -> MessageContent:
    return MessageContent((Paragraph(spans),))


class MessageRenderingTest(unittest.TestCase):
    """Проверяет независимые от API гарантии обеих сериализаций."""

    def test_blocks_and_actions_keep_order_and_exact_commands(self) -> None:
        """Списки, подписи и полные адресные команды не теряются и не меняются."""
        command = "/confirm obsidian disconnect 42 3 yes"
        url = "https://example.org/путь?q=<tag>&id=42"
        content = MessageContent((
            Paragraph((TextSpan("Заголовок", TextStyle.EMPHASIS),)),
            BulletList((ListItem((TextSpan("Первый"),)), ListItem((TextSpan(url),)))),
            Paragraph((TextSpan("Финал"),)),
        ))
        actions = (ConfirmationAction("yes", "Принять", command),
                   ConfirmationAction("no", "Отклонить", command[:-3] + "no"))
        expected = (f"Заголовок\n\n• Первый\n• {url}\n\nФинал\n\nПринять: {command}"
                    f"\n\nОтклонить: {command[:-3]}no")
        telegram = render_telegram_content(content, actions, limit=90)
        vk = render_vk_content(content, actions, limit=90)
        decoded = ["".join(_HtmlReader(chunk.text).parts) for chunk in telegram]
        self.assertEqual("".join(decoded), expected)
        self.assertEqual("".join(chunk.text for chunk in vk), expected)
        for parts in (decoded, [chunk.text for chunk in vk]):
            self.assertTrue(any(command in part for part in parts))
            self.assertTrue(any(url in part for part in parts))

    def test_telegram_escapes_every_style_and_action(self) -> None:
        """HTML/Markdown, кавычки, diff и entities остаются буквальными данными."""
        raw = '<b>"&amp;"</b> `md` \\path\n+diff\n-старое'
        content = _content(*(TextSpan(raw, style) for style in TextStyle))
        action = ConfirmationAction("a", '<img src="x">', "/confirm <x>& yes")
        chunk, = render_telegram_content(content, (action,))
        parsed = _HtmlReader(chunk.text)
        self.assertEqual("".join(parsed.parts), raw * 3 + '\n\n<img src="x">: /confirm <x>& yes')
        self.assertEqual(parsed.tags, ["b", "code", "code"])
        self.assertEqual(chunk.parse_mode, "HTML")
        self.assertIn("&amp;amp;", chunk.text)
        self.assertIn("&quot;", chunk.text)

    def test_long_unicode_text_has_no_loss_or_duplicate_and_closed_tags(self) -> None:
        """Разбиение сохраняет все символы и пробелы при строгом лимите частей."""
        raw = ("Кириллица😀👩‍💻е\u0301<&>  \n\n" * 40) + "X" * 301
        for style in TextStyle:
            with self.subTest(style=style):
                content = _content(TextSpan(raw, style))
                telegram = render_telegram_content(content, limit=61)
                vk = render_vk_content(content, limit=61)
                self.assertGreater(len(telegram), 1)
                self.assertEqual("".join("".join(_HtmlReader(c.text).parts) for c in telegram), raw)
                self.assertEqual("".join(c.text for c in vk), raw)
                for chunk in (*telegram, *vk):
                    self.assertTrue(chunk.text)
                    self.assertLessEqual(len(chunk.text.encode("utf-16-le")) // 2, 61)

    def test_vk_ranges_are_local_and_target_exact_cyrillic(self) -> None:
        """Диапазоны bold и italic указывают на нужные фрагменты каждой части."""
        content = MessageContent(tuple(
            Paragraph((TextSpan("До: "), TextSpan("Жирный", TextStyle.EMPHASIS),
                       TextSpan(" /ask", TextStyle.CODE)))
            for _ in range(4)
        ))
        chunks = render_vk_content(content, limit=19)
        styled_texts = []
        for chunk in chunks:
            if chunk.format_data:
                data = json.loads(chunk.format_data)
                self.assertEqual(data["version"], "1")
                for item in data["items"]:
                    styled_texts.append((item["type"], chunk.text[item["offset"]:item["offset"] + item["length"]]))
        self.assertEqual(styled_texts, [("bold", "Жирный"), ("italic", " /ask")] * 4)

    def test_vk_ambiguous_unicode_falls_back_only_in_affected_chunk(self) -> None:
        """Непроверенные offsets emoji не выдаются за подтверждённую API-разметку."""
        for raw in ("😀", "е\u0301", "👩‍💻", "❤\ufe0f"):
            content = MessageContent((
                Paragraph((TextSpan(raw + " Текст", TextStyle.EMPHASIS),)),
                Paragraph((TextSpan("Жирный", TextStyle.EMPHASIS),)),
            ))
            chunks = render_vk_content(content, limit=12)
            self.assertIsNone(chunks[0].format_data)
            last = chunks[-1]
            data = json.loads(last.format_data)
            item, = data["items"]
            self.assertEqual(last.text[item["offset"]:item["offset"] + item["length"]], "Жирный")

    def test_vk_external_markup_is_literal_and_code_is_italic(self) -> None:
        """Не анализирует внешнюю разметку; CODE оформляет точный текст курсивом."""
        raw = '<b>Привет</b> **мир** `код` &amp;'
        chunk, = render_vk_content(_content(TextSpan(raw), TextSpan("/ask", TextStyle.CODE)))
        self.assertEqual(chunk.text, raw + "/ask")
        self.assertEqual(json.loads(chunk.format_data)["items"], [
            {"type": "italic", "offset": len(raw), "length": 4},
        ])

    def test_empty_and_invalid_limits(self) -> None:
        """Пустой ответ не отправляется; невозможный лимит приводит к явной ошибке."""
        for render in (render_telegram_content, render_vk_content):
            self.assertEqual(render(MessageContent(())), ())
            for limit in (0, -1):
                with self.assertRaises(ValueError):
                    render(_content(TextSpan("x")), limit=limit)
        with self.assertRaises(ValueError):
            render_telegram_content(_content(TextSpan("&", TextStyle.CODE)), limit=17)
        with self.assertRaises(ValueError):
            render_vk_content(_content(TextSpan("😀")), limit=1)

    def test_composed_characters_stay_together_at_small_limits(self) -> None:
        """Combining, VS и ZWJ не отрываются от основы при тесном лимите."""
        for cluster in ("е\u0301", "❤\ufe0f", "👩‍💻", "👍🏽", "🇷🇺"):
            content = _content(TextSpan(cluster * 3, TextStyle.EMPHASIS))
            width = len(cluster.encode("utf-16-le")) // 2
            vk = render_vk_content(content, limit=width)
            telegram = render_telegram_content(content, limit=width + len("<b></b>"))
            self.assertEqual([chunk.text for chunk in vk], [cluster] * 3)
            self.assertEqual(["".join(_HtmlReader(c.text).parts) for c in telegram], [cluster] * 3)
            with self.assertRaisesRegex(ValueError, "grapheme cluster"):
                render_vk_content(content, limit=width - 1)
            with self.assertRaisesRegex(ValueError, "grapheme cluster"):
                render_telegram_content(content, limit=width + len("<b></b>") - 1)

    def test_cluster_crossing_span_boundary_is_repacked(self) -> None:
        """Даже разные стили основы и combining не разделяют символ сообщениями."""
        content = _content(TextSpan("xxе"), TextSpan("\u0301", TextStyle.EMPHASIS))
        vk = render_vk_content(content, limit=3)
        self.assertEqual([chunk.text for chunk in vk], ["xx", "е\u0301"])
        with self.assertRaisesRegex(ValueError, "grapheme cluster"):
            render_vk_content(content, limit=1)
