"""Собирает точные агрегаты расходов по локальным календарным периодам."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from compadre_assistant.capabilities.ask.domain.usage import UsagePeriod, UsageTotals, UsageTotalsReader


DEFAULT_REPORT_TIMEZONE = "Asia/Omsk"  # Пояс отчёта без аргумента.
_CLOCK_RESOLUTION = timedelta(microseconds=1)  # Точность datetime и границ учёта.
_STATISTICS_HISTORY = timedelta(days=90)  # Подтверждённая глубина статистики Timeweb.


class InvalidReportTimezone(ValueError):
    """Часовой пояс отчёта не является допустимым идентификатором IANA."""


@dataclass(frozen=True, slots=True)
class LlmCostReport:
    """Хранит выбранный пояс и точные агрегаты до округления при выводе.

    Args:
        timezone: Использованный идентификатор IANA.
        today: Итог с начала текущих местных суток.
        month: Итог с начала текущего местного календарного месяца.
        year: Итог с начала текущего местного календарного года.
    """

    timezone: str
    today: UsageTotals
    month: UsageTotals
    year: UsageTotals


class GetLlmCostReport:
    """Читает итоги текущих суток, месяца и года одним снимком учёта.

    Args:
        reader: Публичный контракт чтения агрегатов по UTC-интервалам.
        clock: Источник одного момента с часовым поясом на отчёт. По умолчанию
            используется текущее время UTC.
    """

    def __init__(self, reader: UsageTotalsReader, *, clock: Callable[[], datetime] | None = None) -> None:
        self._reader = reader
        self._clock = clock if clock is not None else _utc_now

    def execute(self, timezone: str | None = None) -> LlmCostReport:
        """Возвращает точные суммы и количество обращений без оценки.

        Args:
            timezone: Идентификатор IANA для вызова либо None для `Asia/Omsk`.

        Returns:
            Итоги трёх полуоткрытых интервалов до единого момента отчёта.

        Raises:
            InvalidReportTimezone: Передан неизвестный или недопустимый пояс.
            ValueError: Источник времени вернул момент без часового пояса.
            ZoneInfoNotFoundError: В окружении отсутствуют данные пояса по умолчанию.
        """
        zone_key, periods = _calendar_periods(timezone, self._clock, tuple(ReportPeriod))
        daily, monthly, yearly = self._reader.read_totals(tuple(UsagePeriod(start, end) for _, start, end in periods))
        return LlmCostReport(zone_key, daily, monthly, yearly)


class ReportPeriod(StrEnum):
    """Определяет начало календарного периода отчёта."""

    DAY = "DAY"  # Начало сегодняшних суток.
    MONTH = "MONTH"  # Первый день текущего месяца.
    YEAR = "YEAR"  # Первый день текущего года.


class CostReportStatus(StrEnum):
    """Различает доступность периода и полноту стоимости."""

    COMPLETE = "complete"  # Источник подтвердил стоимость всего периода.
    PARTIAL = "partial"  # Период покрыт, но часть интервалов без цены.
    PERIOD_UNAVAILABLE = "period_unavailable"  # Полное покрытие периода недоступно.
    SOURCE_UNCONFIGURED = "source_unconfigured"  # Оператор ещё не настроил источник.
    SOURCE_FAILED = "source_failed"  # Ошибка чтения этого периода.


@dataclass(frozen=True, slots=True)
class CostStatistics:
    """Хранит статистику по интервалам с подтверждённым покрытием периода.

    Args:
        amount: Сумма известных стоимостей либо None, если все интервалы без цены.
        unknown_intervals: Число интервалов, для которых стоимость неизвестна.
    """

    amount: Decimal | None
    unknown_intervals: int = 0

    def __post_init__(self) -> None:
        """Проверяет, что итог без цены сопровождается признаком неполноты."""
        if self.unknown_intervals < 0 or (self.amount is None and self.unknown_intervals == 0):
            raise ValueError("Некорректный итог интервалов без стоимости.")


class CostSourceError(RuntimeError):
    """Источник не смог вернуть пригодный для обработки ответ."""


class IncompleteCostPeriod(CostSourceError):
    """Источник не подтвердил покрытие всего запрошенного периода."""


class CostStatisticsReader(Protocol):
    """Получает затраты за один интервал из внешней статистики."""

    def read_period(self, start: datetime, end: datetime) -> CostStatistics:
        """Читает покрытый статистикой непустой UTC-интервал.

        Args:
            start: Нижняя граница периода включительно.
            end: Верхняя граница периода исключительно.

        Returns:
            Статистика с известной суммой и числом интервалов без цены.

        Raises:
            IncompleteCostPeriod: Покрытие периода не подтверждено.
            CostSourceError: Чтение или проверка ответа завершились ошибкой.
        """
        ...


@dataclass(frozen=True, slots=True)
class CostReportRow:
    """Хранит сумму, если она известна, и состояние одного периода отчёта."""

    period: ReportPeriod
    status: CostReportStatus
    amount: Decimal | None = None
    unknown_intervals: int = 0


@dataclass(frozen=True, slots=True)
class GatewayCostReport:
    """Содержит выбранный пояс, валюту аккаунта и результаты по периодам."""

    timezone: str
    currency: str
    rows: tuple[CostReportRow, ...]


class GetGatewayCostReport:
    """Строит календарный отчёт из статистики шлюза с отдельным итогом каждого периода.

    Args:
        reader: Источник статистики, если настроен оператором.
        currency: ISO-код валюты аккаунта источника.
        clock: Источник одного момента с часовым поясом на отчёт.
    """

    def __init__(self, reader: CostStatisticsReader | None, *, currency: str,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._reader = reader
        self._currency = currency
        self._clock = clock if clock is not None else _utc_now

    def execute(self, timezone: str | None = None, *, period: ReportPeriod | None = None) -> GatewayCostReport:
        """Возвращает отчёт за указанный период или сводку трёх периодов.

        Args:
            timezone: Идентификатор IANA либо None для пояса по умолчанию.
            period: Выбранный период либо None для сводки.

        Returns:
            Итоги за периоды с общей конечной границей.

        Raises:
            InvalidReportTimezone: Передан неизвестный или недопустимый пояс.
            ValueError: Источник времени вернул момент без часового пояса.
            ZoneInfoNotFoundError: В окружении отсутствуют данные пояса по умолчанию.
        """
        periods = tuple(ReportPeriod) if period is None else (period,)
        zone_key, bounds = _calendar_periods(timezone, self._clock, periods)
        rows = tuple(self._read_row(item, start, end) for item, start, end in bounds)
        return GatewayCostReport(zone_key, self._currency, rows)

    def _read_row(self, period: ReportPeriod, start: datetime, end: datetime) -> CostReportRow:
        if self._reader is None:
            return CostReportRow(period, CostReportStatus.SOURCE_UNCONFIGURED)
        if end - start > _STATISTICS_HISTORY:
            return CostReportRow(period, CostReportStatus.PERIOD_UNAVAILABLE)
        if start == end:
            return CostReportRow(period, CostReportStatus.COMPLETE, Decimal(0))
        try:
            stats = self._reader.read_period(start, end)
        except IncompleteCostPeriod:
            return CostReportRow(period, CostReportStatus.PERIOD_UNAVAILABLE)
        except CostSourceError:
            return CostReportRow(period, CostReportStatus.SOURCE_FAILED)
        status = CostReportStatus.PARTIAL if stats.unknown_intervals else CostReportStatus.COMPLETE
        return CostReportRow(period, status, stats.amount, stats.unknown_intervals)


def _calendar_periods(timezone: str | None, clock: Callable[[], datetime],
                      periods: tuple[ReportPeriod, ...]) -> tuple[str, tuple[tuple[ReportPeriod, datetime, datetime], ...]]:
    zone_key = DEFAULT_REPORT_TIMEZONE if timezone is None else timezone
    try:
        zone = ZoneInfo(zone_key)
    except (ZoneInfoNotFoundError, ValueError):
        if zone_key == DEFAULT_REPORT_TIMEZONE:
            raise
        raise InvalidReportTimezone("Часовой пояс отчёта неизвестен.") from None

    current = clock()
    if current.utcoffset() is None:
        raise ValueError("Для отчёта требуется время с часовым поясом.")
    as_of = current.astimezone(UTC)
    today = as_of.astimezone(zone).date()
    starts = {
        ReportPeriod.DAY: today,
        ReportPeriod.MONTH: today.replace(day=1),
        ReportPeriod.YEAR: today.replace(month=1, day=1),
    }
    return zone.key, tuple((period, _start_of_local_date(starts[period], zone), as_of) for period in periods)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _start_of_local_date(day: date, zone: ZoneInfo) -> datetime:
    midnight = datetime.combine(day, time.min, tzinfo=zone)
    local_offset = midnight.utcoffset()
    before = midnight.astimezone(UTC)
    after = midnight.replace(fold=1).astimezone(UTC)
    if before <= after:
        # В обычную или повторившуюся полночь выбираем самый ранний момент.
        return before

    # Пропущенная полночь лежит внутри скачка вперёд. Ищем его реальную
    # границу, в том числе если скачок начался ещё накануне полуночи.
    earlier, later = after, before
    while later - earlier > _CLOCK_RESOLUTION:
        middle = earlier + (later - earlier) // 2
        if middle.astimezone(zone).utcoffset() == local_offset:
            earlier = middle
        else:
            later = middle
    return later
