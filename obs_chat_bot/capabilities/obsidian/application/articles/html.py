from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleHtml:
    """Представляет HTML страницы статьи, загруженный из внешнего источника.

    Эта модель принадлежит application-слою: use cases работают с ней как с
    результатом загрузки, не зная деталей HTTP-клиента.
    """

    source_url: str
    final_url: str
    content: str
    content_type: str | None = None
