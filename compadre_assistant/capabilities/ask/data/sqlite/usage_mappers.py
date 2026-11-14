"""Преобразует наблюдение Ask в строку SQLite."""

from datetime import UTC, datetime, timedelta
import json

from compadre_assistant.capabilities.ask.data.sqlite.usage_dtos import UsageObservationDto
from compadre_assistant.capabilities.ask.domain.usage import UsageObservation


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def usage_observation_to_dto(observation: UsageObservation) -> UsageObservationDto:
    """Кодирует метаданные обращения для неизменяемой записи.

    Args:
        observation: Доменное наблюдение с исходным ключом вызова.

    Returns:
        Данные SQLite с точным временем в микросекундах UTC и версией формы
        счётчиков. Отсутствующий счётчик отличается от нулевого.
    """
    tokens = observation.tokens
    tokens_json = None if tokens is None else json.dumps({
        "version": 1,
        "prompt_tokens": tokens.prompt_tokens,
        "completion_tokens": tokens.completion_tokens,
        "total_tokens": tokens.total_tokens,
        "prompt_text_tokens": tokens.prompt_text_tokens,
        "image_tokens": tokens.image_tokens,
        "cached_tokens": tokens.cached_tokens,
        "completion_text_tokens": tokens.completion_text_tokens,
        "reasoning_tokens": tokens.reasoning_tokens,
    }, sort_keys=True, separators=(",", ":"))
    return UsageObservationDto(
        call_id=str(observation.call_id),
        started_at_us=(observation.started_at - _EPOCH) // _MICROSECOND,
        provider=observation.provider,
        actual_model=observation.actual_model,
        tokens_json=tokens_json,
    )
