"""Сборка процессного сценария Ask вне domain."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from compadre_assistant.capabilities.ask.data.background_usage_recorder import BackgroundUsageRecorder
from compadre_assistant.capabilities.ask.data.configuration import AskConfig, load_ask_config
from compadre_assistant.capabilities.ask.data.llm.openai_ask_model import OpenAIAskModel
from compadre_assistant.capabilities.ask.data.sqlite.usage_repository import SQLiteUsageRepository
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion
from compadre_assistant.capabilities.ask.domain.use_cases.extract_image_text import ExtractImageText
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability


@contextmanager
def create_ask_runtime(
    *, enabled: bool, database_path: Path, config: AskConfig | None = None,
) -> Iterator[AskCapability | None]:
    """Собирает режим ИИ с одним клиентом и фоновой очередью на время работы канала.

    Args:
        enabled: Явный выбор composition root; при False окружение не читается.
        database_path: Общий путь SQLite с применёнными миграциями.
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
    with ExitStack() as resources:
        recorder = BackgroundUsageRecorder(writer=SQLiteUsageRepository(database_path))
        resources.callback(recorder.close)
        model = OpenAIAskModel(profile, usage_recorder=recorder)
        resources.callback(model.close)
        yield AskCapability(
            question=AskQuestion(model=model, max_question_chars=profile.max_question_chars),
            extract_image_text=ExtractImageText(model=model),
        )
