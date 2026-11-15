"""Собирает возможность ИИ для процесса канала."""

from collections.abc import Iterator
from contextlib import contextmanager

from compadre_assistant.capabilities.ai.data.configuration import AiConfig, load_ai_config
from compadre_assistant.capabilities.ai.data.llm.openai_model import OpenAIModel
from compadre_assistant.capabilities.ai.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ai.domain.use_cases.extract_image_text import ExtractImageText
from compadre_assistant.capabilities.ai.presentation.handler import AiCapability


@contextmanager
def create_ai_runtime(
    *, enabled: bool, config: AiConfig | None = None,
) -> Iterator[AiCapability | None]:
    """Собирает режим ИИ с одним клиентом на время работы канала.

    Args:
        enabled: Явный выбор composition root; при False окружение не читается.
        config: Готовый профиль либо None для загрузки ASK_* из окружения.

    Yields:
        Обработчик режима ИИ либо None, если ИИ отключён.

    Raises:
        AiConfigError: Настройки включённого ИИ некорректны.
    """
    if not enabled:
        yield None
        return
    profile = config if config is not None else load_ai_config()
    model = OpenAIModel(profile)
    try:
        yield AiCapability(
            question=AskQuestion(model=model, max_question_chars=profile.max_question_chars),
            extract_image_text=ExtractImageText(model=model),
        )
    finally:
        model.close()
