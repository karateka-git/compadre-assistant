"""Проверяет календарный отчёт на фиксированном времени и временной SQLite."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

from compadre_assistant.capabilities.admin.domain.cost_report import GetLlmCostReport, InvalidReportTimezone, LlmCostReport
from compadre_assistant.capabilities.ask.bootstrap import create_usage_totals_reader
from compadre_assistant.capabilities.ask.data.sqlite.usage_repository import SQLiteUsageRepository
from compadre_assistant.capabilities.ask.domain.usage import TokenUsage, UsageObservation, UsagePeriod, UsageTotals, UsageTotalsReader
from compadre_assistant.capabilities.ask.domain.usage_pricing import UsagePricing, UsageTariff
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations


_MICROSECOND = timedelta(microseconds=1)
_NOW = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
_OMSK_STARTS = (
    datetime(2026, 1, 1, 18, tzinfo=UTC),
    datetime(2025, 12, 31, 18, tzinfo=UTC),
    datetime(2025, 12, 31, 18, tzinfo=UTC),
)
_MOSCOW_START = datetime(2025, 12, 31, 21, tzinfo=UTC)


class CalendarCostReportTest(unittest.TestCase):
    """Проверяет границы отчёта и контракт чтения одного снимка."""

    def setUp(self) -> None:
        """Возвращает разные точные итоги по порядку интервалов."""
        self.reader = Mock(spec=UsageTotalsReader)
        self.totals = (UsageTotals(1, 2), UsageTotals(3, 4), UsageTotals(5, 6))
        self.reader.read_totals.return_value = self.totals

    def assert_starts(self, expected: tuple[datetime, datetime, datetime], end: datetime) -> None:
        """Проверяет порядок UTC-границ и единственное обращение к reader."""
        self.reader.read_totals.assert_called_once_with(tuple(UsagePeriod(start, end) for start in expected))

    def test_omsk_default_and_moscow_on_different_days(self) -> None:
        """В один момент местные даты отличаются, а верхняя граница общая."""
        clock = Mock(return_value=_NOW)
        use_case = GetLlmCostReport(self.reader, clock=clock)
        self.assertEqual(use_case.execute(), LlmCostReport("Asia/Omsk", *self.totals))
        self.assert_starts(_OMSK_STARTS, _NOW)
        clock.assert_called_once_with()

        self.reader.reset_mock()
        self.assertEqual(use_case.execute("Europe/Moscow"), LlmCostReport("Europe/Moscow", *self.totals))
        self.assert_starts((_MOSCOW_START,) * 3, _NOW)

    def test_month_and_year_cross_in_omsk_before_moscow(self) -> None:
        """Часовой пояс определяет год и месяц рядом с новогодней границей."""
        end = datetime(2025, 12, 31, 19, tzinfo=UTC)
        use_case = GetLlmCostReport(self.reader, clock=lambda: end)
        use_case.execute("Asia/Omsk")
        self.assert_starts((_OMSK_STARTS[1],) * 3, end)
        self.reader.reset_mock()
        use_case.execute("Europe/Moscow")
        self.assert_starts((
            datetime(2025, 12, 30, 21, tzinfo=UTC),
            datetime(2025, 11, 30, 21, tzinfo=UTC),
            datetime(2024, 12, 31, 21, tzinfo=UTC),
        ), end)

    def test_dst_day_uses_offset_at_each_calendar_start(self) -> None:
        """Сезонная смена смещения не переносит полночь и начало года."""
        end = datetime(2026, 3, 29, 10, tzinfo=UTC)
        local_clock = end.astimezone(timezone(timedelta(hours=9)))
        GetLlmCostReport(self.reader, clock=lambda: local_clock).execute("Europe/Berlin")
        self.assert_starts((
            datetime(2026, 3, 28, 23, tzinfo=UTC),
            datetime(2026, 2, 28, 23, tzinfo=UTC),
            datetime(2025, 12, 31, 23, tzinfo=UTC),
        ), end)

    def test_skipped_and_repeated_midnights(self) -> None:
        """Первый реальный момент суток используется и при пропуске полуночи, и при её повторе."""
        for month, day, hour in ((3, 8, 5), (11, 1, 4)):
            with self.subTest(month=month):
                self.reader.reset_mock()
                end = datetime(2026, month, day, 12, tzinfo=UTC)
                GetLlmCostReport(self.reader, clock=lambda: end).execute("America/Havana")
                daily = self.reader.read_totals.call_args.args[0][0]
                self.assertEqual(daily, UsagePeriod(datetime(2026, month, day, hour, tzinfo=UTC), end))

    def test_invalid_timezone_and_clock_do_not_read_usage(self) -> None:
        """Неизвестные, недопустимые ключи и время без пояса останавливают отчёт."""
        clock = Mock(return_value=_NOW)
        for invalid in ("Mars/Olympus", "", "/Asia/Omsk", "Europe/../Asia/Omsk", "Europe/\0Moscow"):
            with self.subTest(timezone=invalid), self.assertRaises(InvalidReportTimezone):
                GetLlmCostReport(self.reader, clock=clock).execute(invalid)
        clock.assert_not_called()
        with self.assertRaises(ValueError):
            GetLlmCostReport(self.reader, clock=lambda: _NOW.replace(tzinfo=None)).execute()
        self.reader.read_totals.assert_not_called()

    def test_missing_default_zone_data_is_runtime_error(self) -> None:
        """Отсутствие обязательной зоны не выдаётся за ошибку пользовательского аргумента."""
        with patch("compadre_assistant.capabilities.admin.domain.cost_report.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            with self.assertRaises(ZoneInfoNotFoundError):
                GetLlmCostReport(self.reader).execute()
        self.reader.read_totals.assert_not_called()


class SQLiteCostReportTest(unittest.TestCase):
    """Проверяет какой набор обращений возвращает публичный port учёта."""

    def test_records_on_calendar_boundaries_are_included_once_per_interval(self) -> None:
        """Полуоткрытые UTC-интервалы включают начало и исключают время отчёта."""
        directory = self.enterContext(TemporaryDirectory(prefix="admin-cost-report-"))
        database_path = Path(directory) / "usage.db"
        with connect_database(database_path) as connection:
            apply_migrations(connection)
        writer = SQLiteUsageRepository(database_path)
        end = datetime(2026, 2, 2, 22, tzinfo=UTC)
        first_year = datetime(2025, 12, 31, 18, tzinfo=UTC)
        first_month = datetime(2026, 1, 31, 18, tzinfo=UTC)
        first_day = datetime(2026, 2, 2, 18, tzinfo=UTC)
        tariff = UsageTariff("test", "sample", "Проверка", first_year - _MICROSECOND, None, 1, 1)
        pricing = UsagePricing((tariff,))
        report = GetLlmCostReport(create_usage_totals_reader(database_path), clock=lambda: end)
        self.assertEqual(report.execute(), LlmCostReport("Asia/Omsk", UsageTotals(0, 0), UsageTotals(0, 0), UsageTotals(0, 0)))

        fixtures = (
            (first_year - _MICROSECOND, 1), (first_year, 2),
            (first_month - _MICROSECOND, 4), (first_month, 8),
            (first_day - _MICROSECOND, 16), (first_day, 32),
            (end - _MICROSECOND, 64), (end, 128), (first_day, None),
        )
        for started, amount in fixtures:
            tokens = None if amount is None else TokenUsage(prompt_tokens=amount, completion_tokens=0)
            observation = UsageObservation(uuid4(), started, "test", "sample", tokens)
            self.assertTrue(writer.store(pricing.estimate(observation)))

        self.assertEqual(report.execute(), LlmCostReport("Asia/Omsk", UsageTotals(96, 1), UsageTotals(120, 1), UsageTotals(126, 1)))
        self.assertEqual(report.execute("Europe/Moscow"), LlmCostReport("Europe/Moscow", UsageTotals(64, 0), UsageTotals(112, 1), UsageTotals(124, 1)))
