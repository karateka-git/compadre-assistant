from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ArticleAnalysisResult:
    """Представляет сохранённый результат LLM-анализа статьи.

    На первом MVP результат анализа хранится как готовый Markdown-текст:
    краткая сводка, основные идеи, практическая польза и темы статьи.
    Более детальную структуру можно добавить позже, когда станет понятно,
    какие блоки действительно нужны в ежедневном использовании.
    """

    article_id: int
    llm_model: str
    prompt_version: str
    result_text: str
    id: int | None = None
    created_at: datetime | None = None
    app_user_id: int = 1

    def __post_init__(self) -> None:
        if self.article_id <= 0:
            raise ValueError("article_id must be positive")
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if not self.llm_model.strip():
            raise ValueError("llm_model must not be empty")
        if not self.prompt_version.strip():
            raise ValueError("prompt_version must not be empty")
        if not self.result_text.strip():
            raise ValueError("result_text must not be empty")
        if self.id is not None and self.id <= 0:
            raise ValueError("id must be positive")
