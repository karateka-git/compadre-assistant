"""Выбирает исторический тариф и оценивает расходы по наблюдению модели."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from compadre_assistant.capabilities.ask.domain.usage import (
    MAX_USAGE_INTEGER,
    TokenUsage,
    UnknownUsageCost,
    UsageObservation,
    _require_usage_integer,
    _require_utc,
)


class UsageDetail(StrEnum):
    """Выделяет категории, для которых тариф может задавать отдельные правила."""

    IMAGE = "image"  # Изображение во входном счётчике.
    CACHED = "cached"  # Кэш во входном счётчике.
    REASONING = "reasoning"  # Reasoning в выходном счётчике.


@dataclass(frozen=True, slots=True)
class UsageTariff:
    """Содержит снимок тарифных ставок в нанорублях за токен.

    Args:
        provider: Идентификатор профиля провайдера.
        model: Фактическая модель, к которой применим тариф.
        source: Ссылка или описание источника ставок.
        effective_from: Начало действия в UTC включительно.
        effective_until: Конец действия в UTC исключительно, если задан.
        prompt_rate: Ставка общего входного счётчика без исключений.
        completion_rate: Ставка общего выходного счётчика без исключений.
        image_rate: Ставка для токенов изображения внутри входного счётчика.
        cached_rate: Ставка для токенов кэша внутри входного счётчика.
        reasoning_rate: Ставка для reasoning-токенов внутри выходного счётчика.
        unpriced_details: Категории, для которых ставка не подтверждена.
        image_cache_disjoint: Провайдер гарантирует, что отдельно
            тарифицируемые токены изображения и кэша не пересекаются.

    Для каждой категории общая ставка применяется, если отдельная ставка
    отсутствует и категория не отмечена в unpriced_details. Отдельная ставка
    требует свой счётчик. Для неподтверждённой категории только явный ноль
    позволяет применить остальные ставки.

    Raises:
        ValueError: Пустые идентификаторы, неверный диапазон времени или ставки.
    """

    provider: str
    model: str
    source: str
    effective_from: datetime
    effective_until: datetime | None
    prompt_rate: int
    completion_rate: int
    image_rate: int | None = None
    cached_rate: int | None = None
    reasoning_rate: int | None = None
    unpriced_details: frozenset[UsageDetail] = frozenset()
    image_cache_disjoint: bool = False

    def __post_init__(self) -> None:
        """Проверяет идентификаторы, интервал действия и ставки тарифа."""
        if not all(value.strip() for value in (self.provider, self.model, self.source)):
            raise ValueError("Профиль, модель и источник тарифа должны быть заданы.")
        _require_utc(self.effective_from)
        if self.effective_until is not None:
            _require_utc(self.effective_until)
            if self.effective_until <= self.effective_from:
                raise ValueError("Конец действия тарифа должен быть позже начала.")
        _require_usage_integer(self.prompt_rate)
        _require_usage_integer(self.completion_rate)
        if any(not isinstance(detail, UsageDetail) for detail in self.unpriced_details):
            raise ValueError("Тариф содержит неизвестную категорию токенов.")
        for detail, rate in (
            (UsageDetail.IMAGE, self.image_rate),
            (UsageDetail.CACHED, self.cached_rate),
            (UsageDetail.REASONING, self.reasoning_rate),
        ):
            if rate is not None:
                if detail in self.unpriced_details:
                    raise ValueError("Категория не может одновременно иметь ставку и быть неподтверждённой.")
                _require_usage_integer(rate)


@dataclass(frozen=True, slots=True)
class UsageEstimate:
    """Связывает наблюдение со снимком тарифа и результатом оценки.

    Сумма хранится в нанорублях. При неизвестной стоимости сумма отсутствует,
    а причина присутствует. Снимок тарифа доступен, если тариф был найден.

    Raises:
        ValueError: Поля суммы, тарифа и причины не образуют корректную оценку.
    """

    observation: UsageObservation
    tariff: UsageTariff | None
    cost_nanorubles: int | None
    unknown_reason: UnknownUsageCost | None = None

    def __post_init__(self) -> None:
        """Проверяет, что известную стоимость можно однозначно отличить от неизвестной."""
        if self.cost_nanorubles is None:
            if self.unknown_reason is None:
                raise ValueError("Для неизвестной стоимости требуется причина.")
        elif self.tariff is None or self.unknown_reason is not None:
            raise ValueError("Для рассчитанной стоимости требуется тариф без причины отказа.")
        else:
            _require_usage_integer(self.cost_nanorubles)


class UsageEstimateWriter(Protocol):
    """Сохраняет рассчитанное обращение с защитой от повторной записи."""

    def store(self, estimate: UsageEstimate) -> bool:
        """Атомарно сохраняет первый результат обращения.

        Args:
            estimate: Наблюдение и результат расчёта с исходным ключом вызова.

        Returns:
            True для новой записи или False, если этот ключ уже сохранён.
        """


class UsagePricing:
    """Оценивает стоимость по проверенному набору тарифов на разные периоды.

    Args:
        tariffs: Снимки тарифов. Пустой каталог допускает запись без оценок.

    Raises:
        ValueError: Пересекаются интервалы одной модели и профиля провайдера.
    """

    def __init__(self, tariffs: tuple[UsageTariff, ...] = ()) -> None:
        """Группирует тарифы по профилю и модели и проверяет их интервалы."""
        self._by_model: dict[tuple[str, str], list[UsageTariff]] = {}
        for tariff in sorted(tariffs, key=lambda item: item.effective_from):
            history = self._by_model.setdefault((tariff.provider, tariff.model), [])
            if history and (history[-1].effective_until is None
                            or tariff.effective_from < history[-1].effective_until):
                raise ValueError("Интервалы действия тарифов для одной модели пересекаются.")
            history.append(tariff)

    def estimate(self, observation: UsageObservation) -> UsageEstimate:
        """Рассчитывает стоимость по фактической модели и моменту вызова.

        Args:
            observation: Наблюдение с доступными счётчиками вызова.

        Returns:
            Точная сумма со снимком тарифа либо наблюдение с причиной, почему
            сумму определить нельзя.
        """
        model = observation.actual_model
        if model is None:
            return UsageEstimate(observation, None, None, UnknownUsageCost.MODEL_UNKNOWN)
        tariff = self._find_tariff(observation.provider, model, observation.started_at)
        if tariff is None:
            return UsageEstimate(observation, None, None, UnknownUsageCost.TARIFF_MISSING)
        amount = _calculate_amount(tariff, observation.tokens)
        if isinstance(amount, UnknownUsageCost):
            return UsageEstimate(observation, tariff, None, amount)
        return UsageEstimate(observation, tariff, amount)

    def _find_tariff(self, provider: str, model: str, started_at: datetime) -> UsageTariff | None:
        return next((
            tariff
            for tariff in self._by_model.get((provider, model), [])
            if tariff.effective_from <= started_at
            and (tariff.effective_until is None or started_at < tariff.effective_until)
        ), None)


def _calculate_amount(tariff: UsageTariff, usage: TokenUsage | None) -> int | UnknownUsageCost:
    if usage is None or usage.prompt_tokens is None or usage.completion_tokens is None:
        return UnknownUsageCost.COUNTERS_MISSING
    prompt, completion = usage.prompt_tokens, usage.completion_tokens
    if (
        (usage.total_tokens is not None and usage.total_tokens != prompt + completion)
        or (usage.prompt_text_tokens or 0) + (usage.image_tokens or 0) > prompt
        or (usage.cached_tokens or 0) > prompt
        or (usage.completion_text_tokens or 0) > completion
        or (usage.reasoning_tokens or 0) > completion
    ):
        return UnknownUsageCost.COUNTERS_INCONSISTENT

    if any(detail in tariff.unpriced_details and count != 0 for detail, count in (
        (UsageDetail.IMAGE, usage.image_tokens),
        (UsageDetail.CACHED, usage.cached_tokens),
        (UsageDetail.REASONING, usage.reasoning_tokens),
    )):
        return UnknownUsageCost.RULES_UNCONFIRMED
    if (tariff.image_rate is not None and tariff.cached_rate is not None
            and usage.image_tokens and usage.cached_tokens and not tariff.image_cache_disjoint):
        return UnknownUsageCost.RULES_UNCONFIRMED

    amount = 0
    for total, base_rate, exceptions in (
        (prompt, tariff.prompt_rate, ((usage.image_tokens, tariff.image_rate), (usage.cached_tokens, tariff.cached_rate))),
        (completion, tariff.completion_rate, ((usage.reasoning_tokens, tariff.reasoning_rate),)),
    ):
        remaining = total
        for count, rate in exceptions:
            if rate is None:
                continue
            if count is None:
                return UnknownUsageCost.COUNTERS_MISSING
            # Исключение замещает часть общего счётчика по правилам тарифа.
            remaining -= count
            amount += count * rate
        if remaining < 0:
            return UnknownUsageCost.COUNTERS_INCONSISTENT
        amount += remaining * base_rate
    if amount > MAX_USAGE_INTEGER:
        return UnknownUsageCost.AMOUNT_OUT_OF_RANGE
    return amount
