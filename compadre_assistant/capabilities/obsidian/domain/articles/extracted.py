from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExtractedArticle:
    """Представляет текст статьи, извлечённый из HTML.

    Модель принадлежит application-слою и не зависит от конкретной библиотеки,
    которая очищает HTML.
    """

    source_url: str
    final_url: str
    cleaned_text: str
    title: str | None = None
