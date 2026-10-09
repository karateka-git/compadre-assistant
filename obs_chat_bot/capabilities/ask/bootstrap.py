"""Сборка процессного сценария Ask вне domain."""

from collections.abc import Iterator
from contextlib import contextmanager

from obs_chat_bot.capabilities.ask.data.configuration import AskConfig, load_ask_config
from obs_chat_bot.capabilities.ask.data.llm.openai_ask_model import OpenAIAskModel
from obs_chat_bot.capabilities.ask.domain.use_cases.ask_question import AskQuestion


@contextmanager
def create_ask_runtime(*, enabled: bool, config: AskConfig | None = None) -> Iterator[AskQuestion | None]:
    """Собирает сценарий включённого Ask и закрывает клиент при выходе.

    Args:
        enabled: Явный выбор composition root; при False окружение не читается.
        config: Готовый профиль либо None для загрузки ASK_* из окружения.

    Yields:
        Сценарий одного вопроса либо None, если Ask отключён.

    Raises:
        AskConfigError: Настройки включённого Ask некорректны.
    """
    if not enabled:
        yield None
        return
    profile = config if config is not None else load_ask_config()
    model = OpenAIAskModel(profile)
    try:
        yield AskQuestion(model=model, max_question_chars=profile.max_question_chars)
    finally:
        model.close()
