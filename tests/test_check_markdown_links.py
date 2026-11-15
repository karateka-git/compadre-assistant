"""Регрессии локальной проверки Markdown в отдельном Git-репозитории."""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import quote


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_markdown_links.py"


class MarkdownLinksTest(unittest.TestCase):
    """Проверяет поведение checker через командную строку и Git-индекс."""

    def setUp(self) -> None:
        """Создаёт временное дерево без истории и сетевых подключений."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self._git("init", "--quiet")

    def _run_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(_SCRIPT)],
            cwd=self.root,
            capture_output=True,
            encoding="utf-8",
        )

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def _write(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_resolves_links_and_skips_code(self) -> None:
        """Учитывает русские якоря, ссылки на файлы и исключает примеры кода."""
        self._write("README.md", "# Вводная\n")
        self._write("assets/Знак (1).svg", "<svg />\n")
        self._write("docs/Два слова.md", """# Старт

## Подтверждать начало `/vault_sync` сразу
## Старт

**Описание**
-------------
""")
        heading = quote("подтверждать-начало-vault_sync-сразу")
        self._write("docs/main.md", f"""# Документ

| [Описание](<Два слова.md#описание> "Справка") | [Команда]({quote("Два слова.md")}#{heading}) |
| - | - |

[Старт](Два%20слова.md#старт-1) · [Документ](#документ) · [Вводная](../README.md#вводная)
[Картинка][значок] · ![Картинка](../assets/Знак%20(1).svg) · [Каталог](/assets/)
[Почта](mailto:bot@example.org) · [Сайт](https://example.org/no-network#fragment)

[значок]: ../assets/Знак%20(1).svg?raw=true#view

`[inline](ignored.md)`
<!-- [комментарий](ignored.md) -->

~~~~md
# Скрытый заголовок
~~~
[пример](ignored.md)
~~~~
""")
        self._write("untracked.md", "[ошибка вне индекса](ignored.md)\n")
        self._git("add", "README.md", "assets", "docs")

        result = self._run_check()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Markdown-файлов проверено: 3, ошибок: 0", result.stdout)

    def test_detects_break_in_unchanged_source(self) -> None:
        """Показывает строку исходного файла после переименования заголовка цели."""
        self._write("README.md", "# Обзор\n\n[Описание](docs/target.md#заголовок)\n")
        self._write("docs/target.md", "# Заголовок\n")
        self._git("add", ".")
        self.assertEqual(self._run_check().returncode, 0)

        self._write("docs/target.md", "# Новый заголовок\n\n```markdown\n# Заголовок\n```\n")
        self._git("add", "docs/target.md")

        result = self._run_check()

        self.assertEqual(result.returncode, 1)
        self.assertIn("README.md:3: якорь отсутствует в docs/target.md: docs/target.md#заголовок", result.stderr)

    def test_detects_missing_paths_and_wrong_case(self) -> None:
        """Требует tracked-путь с точным регистром, включая файлы другого формата."""
        self._write("docs/source.md", "[Не найден](../absent.txt) · [Регистр](../asset.TXT)\n")
        self._write("asset.txt", "путь есть в нижнем регистре")
        self._git("add", ".")

        result = self._run_check()

        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/source.md:1: путь отсутствует в репозитории: ../absent.txt", result.stderr)
        self.assertIn("docs/source.md:1: путь отсутствует в репозитории: ../asset.TXT", result.stderr)
