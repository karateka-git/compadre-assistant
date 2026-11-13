"""Описывает форму SQLite-данных для учёта расходов Ask."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UsageEstimateDto:
    """Хранит поля строки обращения в формате SQLite."""

    call_id: str
    started_at_us: int
    provider: str
    actual_model: str | None
    tokens_json: str | None
    tariff_json: str | None
    cost_nanorubles: int | None
    unknown_reason: str | None


@dataclass(frozen=True, slots=True)
class UsageCostDto:
    """Хранит проекцию строки SQLite для отчёта о расходах."""

    cost_nanorubles: int | None
    unknown_reason: str | None
