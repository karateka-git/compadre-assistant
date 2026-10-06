"""Тесты извлечения metadata из Obsidian Markdown."""

import unittest

from obs_chat_bot.capabilities.obsidian.domain.vaults.markdown import (
    ensure_frontmatter_tag,
    normalize_updated_markdown,
    parse_markdown,
)


class MarkdownMetadataTest(unittest.TestCase):
    """Проверяет frontmatter, tags, заголовок и wikilinks."""

    def test_extracts_frontmatter_and_obsidian_metadata(self) -> None:
        """Парсер объединяет YAML tags, inline tags и нормализует wikilinks."""
        markdown = """---
title: "Моя заметка"
tags:
  - python
  - inbox
---
# Другой заголовок

Текст #idea с [[Folder/Note|алиасом]] и [[Target#Раздел]].
"""
        result = parse_markdown("Folder/File.md", markdown)

        self.assertEqual(result.title, "Моя заметка")
        self.assertIn("title:", result.frontmatter)
        self.assertEqual(result.tags, ("python", "inbox", "idea"))
        self.assertEqual(result.wikilinks, ("Folder/Note", "Target"))

    def test_falls_back_to_heading_and_file_name(self) -> None:
        """Без frontmatter title берётся из H1, затем из имени файла."""
        self.assertEqual(parse_markdown("A.md", "# Заголовок").title, "Заголовок")
        self.assertEqual(parse_markdown("Folder/A.md", "Текст").title, "A")

    def test_created_note_tag_creates_frontmatter_and_is_idempotent(self) -> None:
        """Маркер создаёт frontmatter и не дублируется при повторном вызове."""
        original = "# Заголовок\n\nТело заметки"

        marked = ensure_frontmatter_tag(original, "knowledge-catcher")

        self.assertEqual(
            marked,
            "---\ntags:\n  - knowledge-catcher\n---\n# Заголовок\n\nТело заметки",
        )
        self.assertEqual(
            ensure_frontmatter_tag(marked, "knowledge-catcher"),
            marked,
        )

    def test_created_note_tag_does_not_duplicate_different_case(self) -> None:
        """Obsidian считает регистр тегов незначимым для уникальности."""
        original = "---\ntags: [Knowledge-Catcher]\n---\nТело"

        self.assertEqual(
            ensure_frontmatter_tag(original, "knowledge-catcher"),
            original,
        )

    def test_created_note_tag_preserves_frontmatter_properties_and_body(self) -> None:
        """Существующий block tags дополняется без потери свойств и тела."""
        original = (
            "---\n"
            'title: "Заметка"\n'
            "tags:\n"
            "  - python\n"
            "aliases: [Example]\n"
            "---\n"
            "# Заголовок\n\nТело без изменений\n"
        )

        marked = ensure_frontmatter_tag(original, "topic/ai")

        self.assertIn('title: "Заметка"\n', marked)
        self.assertIn("  - python\n  - topic/ai\n", marked)
        self.assertIn("aliases: [Example]\n", marked)
        self.assertTrue(marked.endswith("# Заголовок\n\nТело без изменений\n"))
        self.assertEqual(parse_markdown("Note.md", marked).tags, ("python", "topic/ai"))

    def test_created_note_tag_adds_tags_property_to_existing_frontmatter(self) -> None:
        """Frontmatter без tags сохраняет свойства и получает block sequence."""
        original = "---\ntitle: Note\naliases: [Example]\n---\nТело"

        marked = ensure_frontmatter_tag(original, "knowledge-catcher")

        self.assertEqual(
            marked,
            "---\ntitle: Note\naliases: [Example]\ntags:\n"
            "  - knowledge-catcher\n---\nТело",
        )

    def test_created_note_tag_extends_flow_tags(self) -> None:
        """Flow sequence сохраняет прежние теги и получает служебный тег."""
        original = "---\ntags: [python, inbox]\n---\nТело"

        marked = ensure_frontmatter_tag(original, "knowledge-catcher")

        self.assertIn("tags: [python, inbox, knowledge-catcher]", marked)
        self.assertEqual(
            parse_markdown("Note.md", marked).tags,
            ("python", "inbox", "knowledge-catcher"),
        )

    def test_created_note_tag_rejects_inline_marker(self) -> None:
        """Inline-маркер от LLM не сохраняется как допустимый результат."""
        with self.assertRaisesRegex(ValueError, "must not appear inline"):
            ensure_frontmatter_tag("# Заголовок\n#knowledge-catcher", "knowledge-catcher")

    def test_created_note_tag_rejects_inline_marker_different_case(self) -> None:
        """Inline-конфликт распознаётся без учёта регистра."""
        with self.assertRaisesRegex(ValueError, "must not appear inline"):
            ensure_frontmatter_tag("Тело #Knowledge-Catcher", "knowledge-catcher")

    def test_created_note_tag_rejects_duplicate_tags_keys(self) -> None:
        """Несколько tags-ключей нельзя безопасно нормализовать выборочно."""
        markdown = (
            "---\n"
            "tags: [python]\n"
            "Tags:\n"
            "  - inbox\n"
            "---\n"
            "Тело"
        )

        with self.assertRaisesRegex(ValueError, "duplicate tags keys"):
            ensure_frontmatter_tag(markdown, "knowledge-catcher")

    def test_created_note_tag_rejects_unquoted_hash_yaml_tags(self) -> None:
        """Некавыченный `#tag` является YAML-комментарием, а не значением."""
        markdown_variants = (
            "---\ntags: #python\n---\nТело",
            "---\ntags: [#python]\n---\nТело",
            "---\ntags:\n  - #python\n---\nТело",
        )

        for markdown in markdown_variants:
            with self.subTest(markdown=markdown):
                with self.assertRaisesRegex(ValueError, "Unsupported frontmatter"):
                    ensure_frontmatter_tag(markdown, "knowledge-catcher")

    def test_created_note_tag_rejects_unsupported_tags_yaml(self) -> None:
        """Сложный YAML tags отклоняется вместо потенциально опасной перезаписи."""
        markdown = "---\ntags: {python: true}\ntitle: Note\n---\nТело"

        with self.assertRaisesRegex(ValueError, "Unsupported frontmatter"):
            ensure_frontmatter_tag(markdown, "knowledge-catcher")

    def test_created_note_tag_rejects_unclosed_frontmatter(self) -> None:
        """Незакрытый frontmatter не маскируется вторым YAML-блоком."""
        with self.assertRaisesRegex(ValueError, "Unsupported frontmatter"):
            ensure_frontmatter_tag(
                "---\ntitle: Broken\n# Заголовок",
                "knowledge-catcher",
            )

    def test_disabled_created_note_tag_keeps_markdown_unchanged(self) -> None:
        """Явное отключение не создаёт frontmatter и не меняет Markdown."""
        original = "# Заголовок\nТело"

        self.assertEqual(ensure_frontmatter_tag(original, None), original)

    def test_update_preserves_source_frontmatter_tags(self) -> None:
        """Update восстанавливает все исходные tags, пропущенные генератором."""
        source = "---\ntags: [Knowledge-Catcher, python]\n---\n# Было"
        proposed = "# Стало\nНовый текст"

        result = normalize_updated_markdown(source, proposed, None)

        self.assertEqual(
            parse_markdown("Note.md", result).tags,
            ("Knowledge-Catcher", "python"),
        )

    def test_update_rejects_marker_on_user_created_note(self) -> None:
        """LLM не может впервые пометить пользовательскую заметку при update."""
        source = "---\ntags: [python]\n---\n# Было"
        proposed = "---\ntags: [python, knowledge-catcher]\n---\n# Стало"

        with self.assertRaisesRegex(ValueError, "user-created"):
            normalize_updated_markdown(source, proposed, "knowledge-catcher")

    def test_update_rejects_new_inline_tag_when_marker_is_disabled(self) -> None:
        """При `off` новый inline tag также не может скрыть служебный marker."""
        with self.assertRaisesRegex(ValueError, "inline tags"):
            normalize_updated_markdown(
                "# Было",
                "# Стало\n#knowledge-catcher",
                None,
            )

    def test_update_allows_new_regular_inline_tag_with_active_marker(self) -> None:
        """Активная настройка не запрещает новые тематические inline-теги."""
        result = normalize_updated_markdown(
            "# Было",
            "# Стало\nТема #python",
            "knowledge-catcher",
        )

        self.assertEqual(result, "# Стало\nТема #python")

    def test_update_rejects_configured_marker_inline_with_active_setting(self) -> None:
        """Configured marker остаётся запрещённым inline при обычном режиме."""
        with self.assertRaisesRegex(ValueError, "must not appear inline"):
            normalize_updated_markdown(
                "# Было",
                "# Стало\n#Knowledge-Catcher",
                "knowledge-catcher",
            )

    def test_update_rejects_new_frontmatter_tag_when_marker_is_disabled(self) -> None:
        """При `off` LLM не может впервые добавить неизвестный старый marker."""
        proposed = "---\ntags: [knowledge-catcher]\n---\n# Стало"

        with self.assertRaisesRegex(ValueError, "frontmatter tags"):
            normalize_updated_markdown("# Было", proposed, None)

    def test_update_preserves_tag_also_present_inline(self) -> None:
        """Обычный исходный tag сохраняется при совпадении с inline-тегом тела."""
        source = "---\ntags: [python]\n---\n# Было\nТема #python"
        proposed = "# Стало\nТема #python"

        result = normalize_updated_markdown(
            source,
            proposed,
            "knowledge-catcher",
        )

        self.assertEqual(parse_markdown("Note.md", result).tags, ("python",))
        self.assertIn("Тема #python", result)

    def test_update_rejects_unsupported_source_frontmatter_tag(self) -> None:
        """Необычный исходный tag не переносится в новый YAML без проверки."""
        source = '---\ntags: ["python data"]\n---\n# Было'

        with self.assertRaisesRegex(ValueError, "Unsupported frontmatter"):
            normalize_updated_markdown(
                source,
                "# Стало",
                "knowledge-catcher",
            )

    def test_update_rejects_duplicate_source_tags_keys(self) -> None:
        """Два исходных набора tags отклоняются до частичного восстановления."""
        source = "---\ntags: [python]\nTags: [docker]\n---\n# Было"

        with self.assertRaisesRegex(ValueError, "Source frontmatter.*duplicate"):
            normalize_updated_markdown(
                source,
                "# Стало",
                "knowledge-catcher",
            )


if __name__ == "__main__":
    unittest.main()
