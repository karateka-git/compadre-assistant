"""Сборка процессного сценария Ask вне domain."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from compadre_assistant.capabilities.ask.data.background_usage_recorder import BackgroundUsageRecorder
from compadre_assistant.capabilities.ask.data.configuration import AskConfig, load_ask_config
from compadre_assistant.capabilities.ask.data.llm.openai_ask_model import OpenAIAskModel
from compadre_assistant.capabilities.ask.data.sqlite.usage_repository import SQLiteUsageRepository
from compadre_assistant.capabilities.ask.domain.usage import UsageTotalsReader
from compadre_assistant.capabilities.ask.domain.usage_pricing import UsagePricing
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import AskQuestion


@contextmanager
def create_ask_runtime(
    *, enabled: bool, database_path: Path, config: AskConfig | None = None,
) -> Iterator[AskQuestion | None]:
    """Собирает сценарий Ask, клиент и фоновую запись на время работы канала.

    Args:
        enabled: Явный выбор composition root; при False окружение не читается.
        database_path: Общий путь SQLite с применёнными миграциями.
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
    with ExitStack() as resources:
        recorder = BackgroundUsageRecorder(
            pricing=UsagePricing(profile.usage_tariffs), writer=SQLiteUsageRepository(database_path),
        )
        resources.callback(recorder.close)
        model = OpenAIAskModel(profile, usage_recorder=recorder)
        resources.callback(model.close)
        yield AskQuestion(model=model, max_question_chars=profile.max_question_chars)


def create_usage_totals_reader(database_path: Path) -> UsageTotalsReader:
    """Открывает доступ к агрегатам без чтения профиля и запуска клиента Ask.

    Args:
        database_path: Общий путь SQLite с применёнными миграциями.

    Returns:
        Публичный интерфейс чтения агрегатов по интервалам.
    """
    return SQLiteUsageRepository(database_path)
