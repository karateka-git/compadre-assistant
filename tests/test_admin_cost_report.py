"""Проверяет календарный отчёт Timeweb на фиксированном времени."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfoNotFoundError

from compadre_assistant.capabilities.admin.domain.cost_report import (
    CostReportRow, CostReportStatus, CostSourceError, CostStatistics, CostStatisticsReader,
    GatewayCostReport, GetGatewayCostReport, IncompleteCostPeriod,
    InvalidReportTimezone, ReportPeriod,
)


_MICROSECOND = timedelta(microseconds=1)
_NOW = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
_OMSK_STARTS = (
    datetime(2026, 1, 1, 18, tzinfo=UTC),
    datetime(2025, 12, 31, 18, tzinfo=UTC),
    datetime(2025, 12, 31, 18, tzinfo=UTC),
)
_MOSCOW_START = datetime(2025, 12, 31, 21, tzinfo=UTC)


class GatewayCostReportTest(unittest.TestCase):
    """Проверяет календарный отчёт шлюза без сети и базы SQLite."""

    def setUp(self) -> None:
        """Подготавливает источник с явно подтверждённой нулевой суммой."""
        self.reader = Mock(spec=CostStatisticsReader)
        self.reader.read_period.return_value = CostStatistics(Decimal(0))

    def test_summary_uses_one_clock_value_for_three_omsk_periods(self) -> None:
        """В сводке границы местного календаря разделяют одну конечную дату."""
        clock = Mock(return_value=_NOW)
        report = GetGatewayCostReport(self.reader, currency="RUB", clock=clock).execute()
        self.assertEqual(report, GatewayCostReport("Asia/Omsk", "RUB", tuple(
            CostReportRow(period, CostReportStatus.COMPLETE, Decimal(0)) for period in ReportPeriod
        )))
        self.assertEqual(self.reader.read_period.call_args_list, [call(start, _NOW) for start in _OMSK_STARTS])
        clock.assert_called_once_with()

    def test_explicit_period_reads_only_requested_window(self) -> None:
        """Выбранный период использует начало месяца в указанном часовом поясе."""
        report = GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: _NOW).execute(
            "Europe/Moscow", period=ReportPeriod.MONTH,
        )
        self.assertEqual(report.rows, (CostReportRow(ReportPeriod.MONTH, CostReportStatus.COMPLETE, Decimal(0)),))
        self.reader.read_period.assert_called_once_with(_MOSCOW_START, _NOW)

    def test_dst_preserves_the_actual_local_day_start(self) -> None:
        """Скачок на летнее время не сдвигает уже прошедшую местную полночь."""
        end = datetime(2026, 3, 29, 10, tzinfo=UTC)
        local_clock = end.astimezone(timezone(timedelta(hours=9)))
        GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: local_clock).execute(
            "Europe/Berlin", period=ReportPeriod.DAY,
        )
        self.reader.read_period.assert_called_once_with(datetime(2026, 3, 28, 23, tzinfo=UTC), end)

    def test_missing_and_repeated_midnight(self) -> None:
        """В отчёт входит первый реальный момент местных суток."""
        for month, day, hour in ((3, 8, 5), (11, 1, 4)):
            end = datetime(2026, month, day, 12, tzinfo=UTC)
            with self.subTest(month=month):
                self.reader.reset_mock()
                GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: end).execute(
                    "America/Havana", period=ReportPeriod.DAY,
                )
                self.reader.read_period.assert_called_once_with(datetime(2026, month, day, hour, tzinfo=UTC), end)

    def test_new_year_starts_at_midnight_of_selected_zone(self) -> None:
        """Один момент относится к разным годам для Москвы и Омска."""
        end = datetime(2025, 12, 31, 20, tzinfo=UTC)
        use_case = GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: end)
        omsk = use_case.execute(period=ReportPeriod.YEAR)
        self.reader.read_period.assert_called_once_with(datetime(2025, 12, 31, 18, tzinfo=UTC), end)
        self.reader.reset_mock()
        moscow = use_case.execute("Europe/Moscow", period=ReportPeriod.YEAR)
        self.reader.read_period.assert_not_called()
        self.assertEqual(omsk.rows[0].status, CostReportStatus.COMPLETE)
        self.assertEqual(moscow.rows[0].status, CostReportStatus.PERIOD_UNAVAILABLE)

    def test_empty_window_at_midnight_does_not_need_provider(self) -> None:
        """При совпадении границ календарный интервал содержит ноль обращений."""
        report = GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: _OMSK_STARTS[0]).execute(
            period=ReportPeriod.DAY,
        )
        self.assertEqual(report.rows, (CostReportRow(ReportPeriod.DAY, CostReportStatus.COMPLETE, Decimal(0)),))
        self.reader.read_period.assert_not_called()

    def test_year_requires_full_coverage_at_the_ninety_day_boundary(self) -> None:
        """Ровно 90 суток доступны, за границей полный год нельзя подменить частью."""
        end = _OMSK_STARTS[2] + timedelta(days=90)
        selected = GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: end).execute(period=ReportPeriod.YEAR)
        self.reader.read_period.assert_called_once_with(_OMSK_STARTS[2], end)
        self.assertEqual(selected.rows[0].status, CostReportStatus.COMPLETE)

        self.reader.reset_mock()
        outside = end + _MICROSECOND
        summary = GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: outside).execute()
        self.assertEqual(self.reader.read_period.call_count, 2)
        self.assertEqual(summary.rows[-1], CostReportRow(ReportPeriod.YEAR, CostReportStatus.PERIOD_UNAVAILABLE))
        self.assertEqual([row.status for row in summary.rows[:2]], [CostReportStatus.COMPLETE] * 2)

    def test_partial_price_and_read_errors_keep_other_summary_rows(self) -> None:
        """Неполная стоимость, ошибка чтения и недостаток покрытия различаются."""
        amount = Decimal("0.0159")
        self.reader.read_period.side_effect = [CostSourceError(), CostStatistics(amount, 2), IncompleteCostPeriod()]
        report = GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: _NOW).execute()
        self.assertEqual(report.rows, (
            CostReportRow(ReportPeriod.DAY, CostReportStatus.SOURCE_FAILED),
            CostReportRow(ReportPeriod.MONTH, CostReportStatus.PARTIAL, amount, 2),
            CostReportRow(ReportPeriod.YEAR, CostReportStatus.PERIOD_UNAVAILABLE),
        ))

    def test_all_costs_unknown_does_not_become_zero(self) -> None:
        """При полном покрытии без единой цены нет известной суммы."""
        self.reader.read_period.return_value = CostStatistics(None, 2)
        report = GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: _NOW).execute(period=ReportPeriod.DAY)
        self.assertEqual(report.rows, (CostReportRow(ReportPeriod.DAY, CostReportStatus.PARTIAL, None, 2),))
        with self.assertRaises(ValueError):
            CostStatistics(None)

    def test_unconfigured_source_has_its_own_status(self) -> None:
        """Полностью пустая настройка явно обозначается в сводке."""
        report = GetGatewayCostReport(None, currency=None, clock=lambda: _NOW).execute()
        self.assertEqual(report.rows, tuple(
            CostReportRow(period, CostReportStatus.SOURCE_UNCONFIGURED) for period in ReportPeriod
        ))

    def test_invalid_timezone_does_not_read_provider(self) -> None:
        """Проверка значения IANA выполняется до чтения статистики и часов."""
        clock = Mock(return_value=_NOW)
        with self.assertRaises(InvalidReportTimezone):
            GetGatewayCostReport(self.reader, currency="RUB", clock=clock).execute("Europe/../Asia/Omsk")
        clock.assert_not_called()
        self.reader.read_period.assert_not_called()

    def test_clock_and_default_timezone_errors_are_runtime_errors(self) -> None:
        """Ошибки окружения не считаются ошибками пользовательского аргумента."""
        with self.assertRaises(ValueError):
            GetGatewayCostReport(self.reader, currency="RUB", clock=lambda: _NOW.replace(tzinfo=None)).execute()
        with patch("compadre_assistant.capabilities.admin.domain.cost_report.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            with self.assertRaises(ZoneInfoNotFoundError):
                GetGatewayCostReport(self.reader, currency="RUB").execute()
        self.reader.read_period.assert_not_called()
