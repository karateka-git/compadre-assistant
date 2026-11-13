"""Хранит обращения Ask и читает точные агрегаты для отчёта."""

from pathlib import Path
import sqlite3

from compadre_assistant.capabilities.ask.data.sqlite.usage_dtos import UsageCostDto
from compadre_assistant.capabilities.ask.data.sqlite.usage_mappers import (
    to_epoch_microseconds, usage_cost_to_totals, usage_estimate_to_dto,
)
from compadre_assistant.capabilities.ask.domain.usage import UsagePeriod, UsageTotals, UsageTotalsReader
from compadre_assistant.capabilities.ask.domain.usage_pricing import UsageEstimate, UsageEstimateWriter
from compadre_assistant.core.data.sqlite.connection import connect_database


class SQLiteUsageRepository(UsageEstimateWriter, UsageTotalsReader):
    """Записывает обращения и читает агрегаты на отдельных соединениях.

    Args:
        database_path: Путь к общей базе с применёнными миграциями.
    """

    def __init__(self, database_path: Path) -> None:
        """Сохраняет путь для открытия коротких соединений в потоке вызова."""
        self._database_path = database_path

    def store(self, estimate: UsageEstimate) -> bool:
        """Атомарно записывает первый результат по ключу обращения.

        Args:
            estimate: Доменный результат расчёта стоимости.

        Returns:
            True для новой строки или False, если ключ уже использован.

        Raises:
            sqlite3.Error: Не удалось выполнить или зафиксировать запись.
        """
        dto = usage_estimate_to_dto(estimate)
        with connect_database(self._database_path) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO ask_usage "
                "(call_id, started_at_us, provider, actual_model, tokens_json, tariff_json, cost_nanorubles, unknown_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(call_id) DO NOTHING",
                (dto.call_id, dto.started_at_us, dto.provider, dto.actual_model,
                 dto.tokens_json, dto.tariff_json, dto.cost_nanorubles, dto.unknown_reason),
            )
            return cursor.rowcount == 1

    def read_totals(self, periods: tuple[UsagePeriod, ...]) -> tuple[UsageTotals, ...]:
        """Читает суммы и число неизвестных стоимостей из одной транзакции.

        Args:
            periods: Полуоткрытые интервалы UTC в порядке вывода.

        Returns:
            Агрегаты в порядке переданных интервалов, по одному на каждый.

        Raises:
            sqlite3.Error: База недоступна для чтения.
        """
        if not periods:
            return ()
        with connect_database(self._database_path) as connection, connection:
            connection.execute("BEGIN")
            return tuple(self._read_period(connection, period) for period in periods)

    @staticmethod
    def _read_period(connection: sqlite3.Connection, period: UsagePeriod) -> UsageTotals:
        rows = connection.execute(
            "SELECT cost_nanorubles, unknown_reason FROM ask_usage "
            "WHERE started_at_us >= ? AND started_at_us < ?",
            (to_epoch_microseconds(period.start), to_epoch_microseconds(period.end)),
        )
        cost = unknown = 0
        for row in rows:
            contribution = usage_cost_to_totals(UsageCostDto(*row))
            # SQLite SUM(INTEGER) переполняется, даже если каждая запись допустима.
            cost += contribution.cost_nanorubles
            unknown += contribution.unknown_count
        return UsageTotals(cost_nanorubles=cost, unknown_count=unknown)
