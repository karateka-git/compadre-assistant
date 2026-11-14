"""Описывает форму наблюдения Ask в SQLite."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UsageObservationDto:
    """Хранит поля строки обращения в формате SQLite."""

    call_id: str
    started_at_us: int
    provider: str
    actual_model: str | None
    tokens_json: str | None
