"""Получает агрегаты расходов выделенного ключа Timeweb AI Gateway."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
import json
import logging

import httpx

from compadre_assistant.capabilities.admin.data.configuration import AdminTimewebConfig
from compadre_assistant.capabilities.admin.domain.cost_report import (
    CostSourceError, CostStatistics, CostStatisticsReader, IncompleteCostPeriod,
)


_LOGGER = logging.getLogger(__name__)
_STATISTICS_URL = "https://api.timeweb.cloud/api/v1/cloud-ai/ai-keys/statistic/token-usage"
_INTERVAL_MINUTES = 60  # Документированный шаг API.
_INTERVAL = timedelta(minutes=_INTERVAL_MINUTES)
_TIMEOUT_SECONDS = 3.0  # Ограничивает каждую сетевую фазу запроса.


@dataclass(frozen=True, slots=True)
class _TimewebCostPoint:
    """Отражает нужные поля точки в ответе Timeweb."""

    time: datetime
    total_cost: Decimal | None


@dataclass(frozen=True, slots=True)
class _TimewebStatisticsResponse:
    """Хранит точки ответа и объявленное в meta число точек."""

    points: tuple[_TimewebCostPoint, ...]
    total: Decimal


class TimewebCostStatisticsReader(CostStatisticsReader):
    """Читает статистику фиксированного ключа через HTTP-клиент процесса."""

    def __init__(self, config: AdminTimewebConfig, client: httpx.Client) -> None:
        """Сохраняет конфигурацию ключа и HTTP-клиент, которым владеет вызывающий код."""
        self._config = config
        self._client = client

    def read_period(self, start: datetime, end: datetime) -> CostStatistics:
        """Возвращает агрегат для непустого полуоткрытого интервала UTC.

        Args:
            start: Нижняя граница периода включительно.
            end: Верхняя граница периода исключительно.

        Raises:
            IncompleteCostPeriod: Ответ не подтверждает покрытие периода.
            CostSourceError: Сетевая ошибка или непригодный ответ.
            ValueError: Границы не образуют непустой интервал UTC.
        """
        if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or start >= end:
            raise ValueError("Timeweb требуется непустой интервал UTC.")
        try:
            response = self._client.get(
                _STATISTICS_URL,
                headers={"Authorization": f"Bearer {self._config.api_token}", "Accept": "application/json"},
                params={
                    "entity_id": str(self._config.key_id),
                    "entity_type": "aikey",
                    "start_time": _utc_isoformat(start),
                    "end_time": _utc_isoformat(end),
                    "interval": _INTERVAL_MINUTES,
                },
                follow_redirects=False,
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.RequestError:
            _LOGGER.warning("Не удалось подключиться к API статистики Timeweb.")
            raise CostSourceError("Не удалось прочитать статистику Timeweb.") from None
        if response.status_code != 200:
            _LOGGER.warning("API статистики Timeweb вернул HTTP %s.", response.status_code)
            raise CostSourceError("Не удалось прочитать статистику Timeweb.")
        try:
            parsed = _parse_statistics(response.content)
            return _map_statistics(parsed, start, end)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError, DecimalException):
            _LOGGER.warning("API статистики Timeweb вернул некорректный ответ.")
            raise CostSourceError("Некорректный ответ API статистики Timeweb.") from None


def _parse_statistics(content: bytes) -> _TimewebStatisticsResponse:
    payload = json.loads(content, parse_float=Decimal, parse_constant=_reject_json_constant)
    points = payload["ai_key_token_statistics"]
    if not isinstance(points, list):
        raise ValueError("Ожидается список точек.")
    count = _nonnegative_decimal(payload["meta"]["total"])
    if count != count.to_integral_value():
        raise ValueError("Число точек должно быть целым.")
    return _TimewebStatisticsResponse(tuple(_parse_point(point) for point in points), count)


def _parse_point(point: dict[str, object]) -> _TimewebCostPoint:
    raw_time = point["time"]
    if not isinstance(raw_time, str):
        raise ValueError("Ожидается время в ISO 8601.")
    time = datetime.fromisoformat(raw_time)
    if time.utcoffset() is None:
        raise ValueError("Временная метка без смещения.")
    raw_cost = point["total_cost"]
    cost = None if raw_cost is None else _nonnegative_decimal(raw_cost)
    return _TimewebCostPoint(time.astimezone(UTC), cost)


def _nonnegative_decimal(value: object) -> Decimal:
    if type(value) is not int and not isinstance(value, Decimal):
        raise ValueError("Ожидается JSON-число.")
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0:
        raise ValueError("Ожидается конечное неотрицательное число.")
    if not amount:
        return Decimal(0)
    # Поля SDK описаны как float32. Допуск сохраняет десятичное представление
    # этих чисел и ограничивает точность, которую может навязать ответ.
    if amount.adjusted() > 38 or amount.as_tuple().exponent < -100:
        raise ValueError("Число выходит за допустимый диапазон.")
    return amount


def _map_statistics(response: _TimewebStatisticsResponse, start: datetime, end: datetime) -> CostStatistics:
    if response.total != len(response.points):
        raise IncompleteCostPeriod("Ответ Timeweb содержит неполное число точек.")
    seen = {point.time for point in response.points}
    if len(seen) != len(response.points):
        raise ValueError("В ответе есть повторные интервалы.")

    # До живой сверки считаем, что API строит часовые интервалы от start_time,
    # включая последний неполный интервал и интервалы без запросов.
    expected_count = -((start - end) // _INTERVAL)
    if len(seen) != expected_count or any(
        not start <= timestamp < end or (timestamp - start) % _INTERVAL for timestamp in seen
    ):
        raise IncompleteCostPeriod("Ответ Timeweb не покрывает запрошенный интервал.")

    known = [point.total_cost for point in response.points if point.total_cost is not None]
    # Допустимый диапазон от 10**-100 до 10**39 и число точек укладываются
    # в эту точность. Сумма остаётся точной до округления интерфейсом.
    with localcontext(prec=150):
        amount = sum(known, Decimal(0)) if known else None
    return CostStatistics(amount, len(response.points) - len(known))


def _utc_isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"


def _reject_json_constant(_: str) -> None:
    raise ValueError("Ожидается конечное JSON-число.")
