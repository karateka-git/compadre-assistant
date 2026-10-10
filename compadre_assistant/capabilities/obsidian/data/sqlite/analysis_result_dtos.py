from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleAnalysisResultDto:
    """Представляет результат анализа в форме data-слоя SQLite.

    DTO отражает таблицу `analysis_results`: хранит связь со статьёй,
    использованную LLM-модель, версию prompt и готовый Markdown-результат.
    """

    article_id: int
    app_user_id: int
    llm_model: str
    prompt_version: str
    result_text: str
    id: int | None = None
    created_at: str | None = None
