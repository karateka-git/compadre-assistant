"""Публичные модели и контракты общего учёта расходов ИИ."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID


# Единица ставки за токен и суммы позволяет хранить дробные рубли точно.
NANORUBLES_PER_RUBLE = 1_000_000_000
# Максимум для отдельного счётчика, ставки или суммы обращения.
MAX_USAGE_INTEGER = 2**63 - 1


def _require_utc(value: datetime) -> None:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Время учёта должно быть задано в UTC.")


def _require_usage_integer(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_USAGE_INTEGER:
        raise ValueError("Значение учёта выходит за диапазон неотрицательного 64-битного целого.")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Хранит доступные счётчики одного ответа модели.

    None означает отсутствие счётчика, 0 — явное нулевое значение. Детали
    не прибавляются к общим счётчикам. Несовместимые сочетания сохраняются
    как наблюдение без оценки.

    Raises:
        ValueError: Счётчик не является неотрицательным 64-битным целым.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    prompt_text_tokens: int | None = None
    image_tokens: int | None = None
    cached_tokens: int | None = None
    completion_text_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        """Проверяет диапазон каждого предоставленного счётчика."""
        for count in (
            self.prompt_tokens, self.completion_tokens, self.total_tokens,
            self.prompt_text_tokens, self.image_tokens, self.cached_tokens,
            self.completion_text_tokens, self.reasoning_tokens,
        ):
            if count is not None:
                _require_usage_integer(count)


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """Хранит метаданные попытки вызова модели для записи расходов.

    Args:
        call_id: Внутренний ключ, созданный до сетевого вызова. При повторе
            записи одного результата ключ сохраняется.
        started_at: Время начала обращения в UTC.
        provider: Идентификатор профиля провайдера в каталоге тарифов.
        actual_model: Модель из ответа провайдера либо None, если она неизвестна.
        tokens: Счётчики ответа либо None, если расход не был сообщён.

    Raises:
        ValueError: Время задано без UTC, провайдер или указанная модель пусты.
    """

    call_id: UUID
    started_at: datetime
    provider: str
    actual_model: str | None
    tokens: TokenUsage | None

    def __post_init__(self) -> None:
        """Проверяет время и идентификаторы, по которым выбирается тариф."""
        _require_utc(self.started_at)
        if not self.provider.strip() or (self.actual_model is not None and not self.actual_model.strip()):
            raise ValueError("Идентификаторы провайдера и указанной модели не могут быть пустыми.")


@dataclass(frozen=True, slots=True)
class UsagePeriod:
    """Задаёт полуоткрытый интервал отчёта [start, end) в UTC.

    Raises:
        ValueError: Границы заданы без UTC либо end раньше start.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """Проверяет границы интервала."""
        _require_utc(self.start)
        _require_utc(self.end)
        if self.end < self.start:
            raise ValueError("Конец интервала раньше начала.")


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Содержит точную сумму в нанорублях и число обращений без оценки.

    Сумма учитывает только рассчитанные обращения и может превышать диапазон
    отдельной записи.
    """

    cost_nanorubles: int
    unknown_count: int


class UsageRecorder(Protocol):
    """Публичный приём наблюдений от сценариев, которые вызывают модель."""

    def submit(self, observation: UsageObservation) -> None:
        """Передаёт наблюдение на фоновый расчёт и запись.

        Метод не ожидает записи в хранилище. Сбои учёта передаются в диагностику.

        Args:
            observation: Метаданные одного вызова с ключом, общим для повторов.
        """


class UsageTotalsReader(Protocol):
    """Публичное чтение сумм для режима администрирования."""

    def read_totals(self, periods: tuple[UsagePeriod, ...]) -> tuple[UsageTotals, ...]:
        """Читает агрегаты по всем интервалам из одного снимка данных.

        Args:
            periods: Интервалы UTC, границы которых вычислил вызывающий сценарий.

        Returns:
            Агрегаты в том же порядке. Пустой интервал даёт нулевые значения.
        """


class UnknownUsageCost(StrEnum):
    """Объясняет, почему стоимость обращения не рассчитана."""

    MODEL_UNKNOWN = "model_unknown"  # Провайдер не сообщил фактическую модель.
    TARIFF_MISSING = "tariff_missing"  # Для модели и времени нет подтверждённого тарифа.
    COUNTERS_MISSING = "counters_missing"  # Тариф требует счётчик, которого нет в ответе.
    COUNTERS_INCONSISTENT = "counters_inconsistent"  # Счётчики противоречат друг другу или тарифу.
    RULES_UNCONFIRMED = "rules_unconfirmed"  # Нельзя применить ставки к этой детализации.
    AMOUNT_OUT_OF_RANGE = "amount_out_of_range"  # Точная сумма превышает диапазон записи.
    UNKNOWN = "unknown"  # Сохранённая причина не распознана этой версией.
