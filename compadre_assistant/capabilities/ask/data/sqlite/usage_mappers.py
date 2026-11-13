"""Преобразует модели учёта Ask в форму SQLite и проекции обратно."""

from datetime import UTC, datetime, timedelta
import json

from compadre_assistant.capabilities.ask.data.sqlite.usage_dtos import UsageCostDto, UsageEstimateDto
from compadre_assistant.capabilities.ask.domain.usage import UnknownUsageCost, UsageTotals
from compadre_assistant.capabilities.ask.domain.usage_pricing import UsageEstimate


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def to_epoch_microseconds(value: datetime) -> int:
    """Кодирует момент в целых микросекундах UTC от эпохи Unix.

    Args:
        value: Проверенный момент в UTC.

    Returns:
        Целое значение без округления через timestamp с плавающей точкой.
    """
    return (value - _EPOCH) // _MICROSECOND


def _snapshot_json(fields: dict[str, object]) -> str:
    # Явная версия сохраняет смысл снимков при расширении модели учёта.
    return json.dumps({"version": 1, **fields}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def usage_estimate_to_dto(estimate: UsageEstimate) -> UsageEstimateDto:
    """Кодирует результат расчёта для неизменяемой записи обращения.

    Args:
        estimate: Доменный результат с наблюдением и снимком тарифа.

    Returns:
        Данные строки, в которых отсутствующий счётчик отличается от нуля.
    """
    observation = estimate.observation
    tokens = observation.tokens
    tariff = estimate.tariff
    tariff_until = tariff.effective_until if tariff is not None else None
    return UsageEstimateDto(
        call_id=str(observation.call_id),
        started_at_us=to_epoch_microseconds(observation.started_at),
        provider=observation.provider,
        actual_model=observation.actual_model,
        tokens_json=None if tokens is None else _snapshot_json({
            "prompt_tokens": tokens.prompt_tokens,
            "completion_tokens": tokens.completion_tokens,
            "total_tokens": tokens.total_tokens,
            "prompt_text_tokens": tokens.prompt_text_tokens,
            "image_tokens": tokens.image_tokens,
            "cached_tokens": tokens.cached_tokens,
            "completion_text_tokens": tokens.completion_text_tokens,
            "reasoning_tokens": tokens.reasoning_tokens,
        }),
        tariff_json=None if tariff is None else _snapshot_json({
            "provider": tariff.provider,
            "model": tariff.model,
            "source": tariff.source,
            "effective_from_us": to_epoch_microseconds(tariff.effective_from),
            "effective_until_us": to_epoch_microseconds(tariff_until) if tariff_until is not None else None,
            "prompt_rate": tariff.prompt_rate,
            "completion_rate": tariff.completion_rate,
            "image_rate": tariff.image_rate,
            "cached_rate": tariff.cached_rate,
            "reasoning_rate": tariff.reasoning_rate,
            "unpriced_details": sorted(detail.value for detail in tariff.unpriced_details),
            "image_cache_disjoint": tariff.image_cache_disjoint,
        }),
        cost_nanorubles=estimate.cost_nanorubles,
        unknown_reason=estimate.unknown_reason.value if estimate.unknown_reason is not None else None,
    )


def usage_cost_to_totals(dto: UsageCostDto) -> UsageTotals:
    """Восстанавливает вклад строки в отчёт, учитывая будущие причины неизвестности."""
    reason = None
    if dto.unknown_reason is not None:
        try:
            reason = UnknownUsageCost(dto.unknown_reason)
        except ValueError:
            reason = UnknownUsageCost.UNKNOWN
    if reason is not None:
        return UsageTotals(cost_nanorubles=0, unknown_count=1)
    assert dto.cost_nanorubles is not None  # Ограничение таблицы гарантирует сумму без причины.
    return UsageTotals(cost_nanorubles=dto.cost_nanorubles, unknown_count=0)
