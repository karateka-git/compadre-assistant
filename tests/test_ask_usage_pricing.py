"""Проверяет точный расчёт расходов и правила выбора тарифа для Ask."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

from compadre_assistant.capabilities.ask.domain.usage import (
    MAX_USAGE_INTEGER, TokenUsage, UnknownUsageCost, UsageObservation, UsagePeriod,
)
from compadre_assistant.capabilities.ask.domain.usage_pricing import (
    UsageDetail, UsagePricing, UsageTariff,
)


_START = datetime(2026, 11, 13, 15, 37, 17, tzinfo=UTC)
_MODEL = "gemini/gemini-3.1-flash-lite"


class UsagePricingTests(unittest.TestCase):
    """Сверяет оценку по наблюдениям Timeweb и искусственным границам."""

    def setUp(self) -> None:
        """Создаёт тестовый тариф с условными общими ставками из пробы."""
        self.tariff = UsageTariff(
            "timeweb", _MODEL, "Условный пример из GATEWAY_OBSERVATIONS",
            _START, None, prompt_rate=34_000, completion_rate=203_000,
        )

    def _observation(self, tokens: TokenUsage | None) -> UsageObservation:
        return UsageObservation(uuid4(), _START, "timeweb", _MODEL, tokens)

    def test_exact_cost_from_observed_usage(self) -> None:
        """Входные токены изображения уже входят в общий счётчик пробы."""
        pricing = UsagePricing((self.tariff,))
        # Счётчики из gateway-text-response.json и gateway-image-response.json.
        for tokens, expected in (
            (TokenUsage(prompt_tokens=19, completion_tokens=2, total_tokens=21,
                        prompt_text_tokens=19, completion_text_tokens=2), 1_052_000),
            (TokenUsage(prompt_tokens=1088, completion_tokens=13, total_tokens=1101,
                        prompt_text_tokens=24, image_tokens=1064, completion_text_tokens=13), 39_631_000),
        ):
            with self.subTest(tokens=tokens):
                estimate = pricing.estimate(self._observation(tokens))
                self.assertEqual(estimate.cost_nanorubles, expected)
                self.assertEqual(estimate.tariff, self.tariff)
                self.assertIsNone(estimate.unknown_reason)

    def test_zero_and_missing_counters_are_distinct(self) -> None:
        """Нулевые общие счётчики дают ноль, отсутствие обязательного даёт причину."""
        pricing = UsagePricing((self.tariff,))
        zero = pricing.estimate(self._observation(TokenUsage(prompt_tokens=0, completion_tokens=0)))
        self.assertEqual(zero.cost_nanorubles, 0)
        self.assertIsNone(zero.unknown_reason)
        for tokens in (None, TokenUsage(prompt_tokens=0), TokenUsage(completion_tokens=0)):
            with self.subTest(tokens=tokens):
                missing = pricing.estimate(self._observation(tokens))
                self.assertIsNone(missing.cost_nanorubles)
                self.assertEqual(missing.unknown_reason, UnknownUsageCost.COUNTERS_MISSING)
                self.assertEqual(missing.tariff, self.tariff)

    def test_separate_rates_replace_part_of_totals(self) -> None:
        """Искусственный тариф требует детали и учитывает каждую категорию один раз."""
        tariff = replace(self.tariff, prompt_rate=10, completion_rate=20,
                         image_rate=5, cached_rate=2, reasoning_rate=1, image_cache_disjoint=True)
        pricing = UsagePricing((tariff,))
        tokens = TokenUsage(prompt_tokens=10, completion_tokens=5, image_tokens=3,
                            cached_tokens=2, reasoning_tokens=4)
        estimate = pricing.estimate(self._observation(tokens))
        self.assertEqual(estimate.cost_nanorubles, 5 * 10 + 3 * 5 + 2 * 2 + 1 * 20 + 4)
        missing = pricing.estimate(self._observation(replace(tokens, cached_tokens=None)))
        self.assertEqual(missing.unknown_reason, UnknownUsageCost.COUNTERS_MISSING)
        explicit_zero = pricing.estimate(self._observation(replace(tokens, cached_tokens=0)))
        self.assertEqual(explicit_zero.cost_nanorubles, 7 * 10 + 3 * 5 + 1 * 20 + 4)

    def test_unconfirmed_rules_require_explicit_absence_or_disjointness(self) -> None:
        """Без правил для детализации расчёт возможен при явном нуле категории."""
        tokens = TokenUsage(prompt_tokens=10, completion_tokens=0, image_tokens=2, cached_tokens=3)
        tariff = replace(self.tariff, unpriced_details=frozenset({UsageDetail.IMAGE}))
        pricing = UsagePricing((tariff,))
        for count in (None, 2):
            with self.subTest(image_tokens=count):
                estimate = pricing.estimate(self._observation(replace(tokens, image_tokens=count)))
                self.assertEqual(estimate.unknown_reason, UnknownUsageCost.RULES_UNCONFIRMED)
        zero = pricing.estimate(self._observation(replace(tokens, image_tokens=0)))
        self.assertEqual(zero.cost_nanorubles, 10 * tariff.prompt_rate)

        potential_overlap = replace(self.tariff, image_rate=5, cached_rate=2)
        estimate = UsagePricing((potential_overlap,)).estimate(self._observation(tokens))
        self.assertEqual(estimate.unknown_reason, UnknownUsageCost.RULES_UNCONFIRMED)

    def test_inconsistent_details_prevent_an_estimate(self) -> None:
        """Противоречивые общие и детальные данные остаются в наблюдении."""
        tariff = replace(self.tariff, image_rate=1, cached_rate=1, image_cache_disjoint=True)
        pricing = UsagePricing((tariff,))
        tokens = TokenUsage(prompt_tokens=10, completion_tokens=3, image_tokens=7, cached_tokens=2)
        for invalid in (
            replace(tokens, total_tokens=12), replace(tokens, prompt_text_tokens=4),
            replace(tokens, cached_tokens=5), replace(tokens, reasoning_tokens=4),
        ):
            with self.subTest(tokens=invalid):
                observation = self._observation(invalid)
                estimate = pricing.estimate(observation)
                self.assertEqual(estimate.unknown_reason, UnknownUsageCost.COUNTERS_INCONSISTENT)
                self.assertIsNone(estimate.cost_nanorubles)
                self.assertEqual(estimate.observation, observation)

    def test_tariff_selection_uses_actual_model_provider_and_time(self) -> None:
        """На границе действует новый тариф фактической модели, включая резервную."""
        later = _START + timedelta(hours=1)
        old = replace(self.tariff, effective_until=later)
        new = replace(self.tariff, effective_from=later, prompt_rate=old.prompt_rate + 1)
        fallback = replace(self.tariff, model="fallback", prompt_rate=42)
        another_provider = replace(self.tariff, provider="another", prompt_rate=9)
        pricing = UsagePricing((new, fallback, old, another_provider))
        base = self._observation(TokenUsage(prompt_tokens=1, completion_tokens=0))
        for observation, expected in (
            (base, old), (replace(base, started_at=later), new),
            (replace(base, actual_model="fallback"), fallback),
            (replace(base, provider="another"), another_provider),
        ):
            with self.subTest(observation=observation):
                estimate = pricing.estimate(observation)
                self.assertEqual(estimate.tariff, expected)
                self.assertEqual(estimate.cost_nanorubles, expected.prompt_rate)
        self.assertEqual(pricing.estimate(replace(base, actual_model=None)).unknown_reason,
                         UnknownUsageCost.MODEL_UNKNOWN)
        self.assertEqual(pricing.estimate(replace(base, actual_model="unlisted")).unknown_reason,
                         UnknownUsageCost.TARIFF_MISSING)
        self.assertEqual(pricing.estimate(replace(base, started_at=_START - timedelta(microseconds=1))).unknown_reason,
                         UnknownUsageCost.TARIFF_MISSING)

    def test_bounds_prevent_invalid_records_and_overflow(self) -> None:
        """Учитывает максимальную сумму записи и отвергает повреждённые входы."""
        tariff = replace(self.tariff, prompt_rate=1, completion_rate=1)
        pricing = UsagePricing((tariff,))
        tokens = TokenUsage(prompt_tokens=MAX_USAGE_INTEGER, completion_tokens=0)
        self.assertEqual(pricing.estimate(self._observation(tokens)).cost_nanorubles, MAX_USAGE_INTEGER)
        overflow = pricing.estimate(self._observation(replace(tokens, completion_tokens=1)))
        self.assertEqual(overflow.unknown_reason, UnknownUsageCost.AMOUNT_OUT_OF_RANGE)
        for value in (-1, True, MAX_USAGE_INTEGER + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TokenUsage(prompt_tokens=value)
        with self.assertRaises(ValueError):
            replace(tariff, cached_rate=-1)
        with self.assertRaises(ValueError):
            UsagePricing((tariff, replace(tariff, effective_from=_START + timedelta(days=1))))

    def test_report_period_allows_empty_and_requires_ordered_utc_bounds(self) -> None:
        """Контракт чтения допускает пустой интервал и отвергает локальное время."""
        self.assertEqual(UsagePeriod(_START, _START).end, _START)
        with self.assertRaises(ValueError):
            UsagePeriod(_START, _START.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            UsagePeriod(_START, _START - timedelta(microseconds=1))
