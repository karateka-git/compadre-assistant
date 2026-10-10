from __future__ import annotations

from enum import StrEnum


class ArticleStatus(StrEnum):
    """Описывает этап обработки сохранённой статьи."""

    # Запись создана, обработка ещё не началась.
    NEW = "new"
    # Страница загружается по исходному URL.
    FETCHING = "fetching"
    # Чистый текст извлечён из загруженной страницы.
    EXTRACTED = "extracted"
    # Извлечённый текст обрабатывается LLM.
    ANALYZING = "analyzing"
    # Результат LLM-анализа сохранён.
    ANALYZED = "analyzed"
    # Статья ожидает сопоставления с заметками Obsidian.
    NEEDS_OBSIDIAN_REVIEW = "needs_obsidian_review"
    # Проверка и сопоставление с Obsidian завершены.
    REVIEWED = "reviewed"
    # Обработка остановлена из-за ошибки на одном из этапов.
    FAILED = "failed"
