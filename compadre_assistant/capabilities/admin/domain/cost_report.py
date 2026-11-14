"""Собирает точные агрегаты расходов по локальным календарным периодам."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from compadre_assistant.capabilities.ask.domain.usage import UsagePeriod, UsageTotals, UsageTotalsReader


DEFAULT_REPORT_TIMEZONE = "Asia/Omsk"
_CLOCK_RESOLUTION = timedelta(microseconds=1)  # Точность datetime и границ учёта.


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
        zone_key = DEFAULT_REPORT_TIMEZONE if timezone is None else timezone
        try:
            zone = ZoneInfo(zone_key)
        except (ZoneInfoNotFoundError, ValueError):
            if zone_key == DEFAULT_REPORT_TIMEZONE:
                raise
            raise InvalidReportTimezone("Часовой пояс отчёта неизвестен.") from None

        current = self._clock()
        if current.utcoffset() is None:
            raise ValueError("Для отчёта требуется время с часовым поясом.")
        as_of = current.astimezone(UTC)
        today = as_of.astimezone(zone).date()
        starts = (today, today.replace(day=1), today.replace(month=1, day=1))
        periods = tuple(UsagePeriod(_start_of_local_date(day, zone), as_of) for day in starts)
        daily, monthly, yearly = self._reader.read_totals(periods)
        return LlmCostReport(zone.key, daily, monthly, yearly)


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
