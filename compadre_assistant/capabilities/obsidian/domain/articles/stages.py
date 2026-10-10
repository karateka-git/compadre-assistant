from __future__ import annotations

from enum import StrEnum


class ProcessingStage(StrEnum):
    """Описывает этап pipeline, на котором может возникнуть ошибка."""

    # Подготовка и нормализация входного URL.
    NORMALIZATION = "normalization"
    # Создание или поиск записи статьи в хранилище.
    STORAGE = "storage"
    # Загрузка HTML страницы статьи.
    FETCHING = "fetching"
    # Извлечение чистого текста из HTML.
    EXTRACTION = "extraction"
    # LLM-анализ очищенного текста статьи.
    ANALYSIS = "analysis"
    # Поиск по vault и подготовка предлагаемого Obsidian-изменения.
    OBSIDIAN_REVIEW = "obsidian_review"
    # Проверка SHA и подтверждённая запись Markdown в GitHub vault.
    OBSIDIAN_WRITE = "obsidian_write"
    # Обновление локальных chunks и embeddings после успешной записи в vault.
    OBSIDIAN_INDEX = "obsidian_index"
