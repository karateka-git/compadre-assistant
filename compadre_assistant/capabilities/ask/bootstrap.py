"""Сборка процессного сценария Ask вне domain."""

from collections.abc import Iterator
from contextlib import contextmanager

from compadre_assistant.capabilities.ask.data.configuration import AskConfig, load_ask_config
from compadre_assistant.capabilities.ask.data.llm.openai_ask_model import OpenAIAskModel
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ask.domain.use_cases.extract_image_text import ExtractImageText
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability


@contextmanager
def create_ask_runtime(
    *, enabled: bool, config: AskConfig | None = None,
) -> Iterator[AskCapability | None]:
    """Собирает режим ИИ с одним клиентом на время работы канала.

    Args:
        enabled: Явный выбор composition root; при False окружение не читается.
        config: Готовый профиль либо None для загрузки ASK_* из окружения.

    Yields:
        Обработчик режима ИИ либо None, если Ask отключён.

    Raises:
        AskConfigError: Настройки включённого Ask некорректны.
    """
    if not enabled:
        yield None
        return
    profile = config if config is not None else load_ask_config()
    model = OpenAIAskModel(profile)
    try:
        yield AskCapability(
            question=AskQuestion(model=model, max_question_chars=profile.max_question_chars),
            extract_image_text=ExtractImageText(model=model),
        )
    finally:
        model.close()
