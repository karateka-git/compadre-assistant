"""Получает агрегаты расходов и запросов выделенного ключа Timeweb AI Gateway."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
import json
import logging

import httpx

from compadre_assistant.capabilities.admin.data.configuration import AdminTimewebConfig
from compadre_assistant.capabilities.admin.domain.cost_report import (
    CostSourceError, CostStatistics, CostStatisticsReader, IncompleteCostPeriod, RequestCountReader,
)


_LOGGER = logging.getLogger(__name__)
_STATISTICS_URL = "https://api.timeweb.cloud/api/v1/cloud-ai/ai-keys/statistic/token-usage"
_CALLS_URL = "https://api.timeweb.cloud/api/v1/cloud-ai/ai-keys/statistic/calls"
_INTERVAL_MINUTES = 60  # Документированный шаг API.
_INTERVAL = timedelta(minutes=_INTERVAL_MINUTES)
_TIMEOUT_SECONDS = 3.0  # Ограничивает каждую сетевую фазу запроса.
_MAX_CALLS_PER_POINT = 2_147_483_647  # Верхняя граница int32 из модели Timeweb.


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


@dataclass(frozen=True, slots=True)
class _TimewebCallPoint:
    """Отражает число запросов и временную метку точки Timeweb."""

    time: datetime
    call_count: int


class TimewebCostStatisticsReader(CostStatisticsReader, RequestCountReader):
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

        Returns:
            Стоимость и число часовых интервалов без цены.

        Raises:
            IncompleteCostPeriod: Ответ не подтверждает покрытие периода.
            CostSourceError: Сетевая ошибка или непригодный ответ.
            ValueError: Границы не образуют непустой интервал UTC.
        """
        inclusive_end = _inclusive_end(start, end)
        content = self._read_response(_STATISTICS_URL, start, inclusive_end)
        try:
            parsed = _parse_statistics(content)
            return _map_statistics(parsed, start, inclusive_end)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError, DecimalException):
            _LOGGER.warning("API статистики Timeweb вернул некорректный ответ.")
            raise CostSourceError("Некорректный ответ API статистики Timeweb.") from None

    def read_request_count(self, start: datetime, end: datetime) -> int:
        """Возвращает число запросов для непустого полуоткрытого UTC-интервала.

        Args:
            start: Нижняя граница периода включительно.
            end: Верхняя граница периода исключительно.

        Returns:
            Сумма числа запросов по всем интервалам и моделям ключа.

        Raises:
            IncompleteCostPeriod: Ответ не подтверждает покрытие периода.
            CostSourceError: Сетевая ошибка или непригодный ответ.
            ValueError: Границы не образуют непустой интервал UTC.
        """
        inclusive_end = _inclusive_end(start, end)
        content = self._read_response(_CALLS_URL, start, inclusive_end)
        try:
            payload, total = _parse_statistics_payload(content, "ai_key_calls_statistics")
            points = tuple(_parse_call_point(point) for point in payload)
            _validate_coverage(tuple(point.time for point in points), total, start, inclusive_end)
            return sum(point.call_count for point in points)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError, DecimalException):
            _LOGGER.warning("API числа запросов Timeweb вернул некорректный ответ.")
            raise CostSourceError("Некорректный ответ API числа запросов Timeweb.") from None

    def _read_response(self, url: str, start: datetime, inclusive_end: datetime) -> bytes:
        try:
            response = self._client.get(
                url,
                headers={"Authorization": f"Bearer {self._config.api_token}", "Accept": "application/json"},
                params={
                    "entity_id": str(self._config.key_id),
                    "entity_type": "aikey",
                    "start_time": _utc_isoformat(start),
                    "end_time": _utc_isoformat(inclusive_end),
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
        return response.content


def _parse_statistics(content: bytes) -> _TimewebStatisticsResponse:
    points, total = _parse_statistics_payload(content, "ai_key_token_statistics")
    return _TimewebStatisticsResponse(tuple(_parse_cost_point(point) for point in points), total)


def _parse_statistics_payload(content: bytes, points_key: str) -> tuple[list[dict[str, object]], Decimal]:
    payload = json.loads(content, parse_float=Decimal, parse_constant=_reject_json_constant)
    points = payload[points_key]
    if not isinstance(points, list):
        raise ValueError("Ожидается список точек.")
    count = _nonnegative_decimal(payload["meta"]["total"])
    if count != count.to_integral_value():
        raise ValueError("Число точек должно быть целым.")
    return points, count


def _parse_cost_point(point: dict[str, object]) -> _TimewebCostPoint:
    raw_cost = point["total_cost"]
    cost = None if raw_cost is None else _nonnegative_decimal(raw_cost)
    return _TimewebCostPoint(_parse_point_time(point["time"]), cost)


def _parse_call_point(point: dict[str, object]) -> _TimewebCallPoint:
    count = point["call_count"]
    if type(count) is not int or not 0 <= count <= _MAX_CALLS_PER_POINT:
        raise ValueError("Число запросов должно быть неотрицательным int32.")
    return _TimewebCallPoint(_parse_point_time(point["time"]), count)


def _parse_point_time(raw_time: object) -> datetime:
    if not isinstance(raw_time, str):
        raise ValueError("Ожидается время в ISO 8601.")
    time = datetime.fromisoformat(raw_time)
    if time.utcoffset() is None:
        raise ValueError("Временная метка без смещения.")
    return time.astimezone(UTC)


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


def _map_statistics(response: _TimewebStatisticsResponse, start: datetime, inclusive_end: datetime) -> CostStatistics:
    _validate_coverage(tuple(point.time for point in response.points), response.total, start, inclusive_end)
    known = [point.total_cost for point in response.points if point.total_cost is not None]
    # Допустимый диапазон от 10**-100 до 10**39 и число точек укладываются
    # в эту точность. Сумма остаётся точной до округления интерфейсом.
    with localcontext(prec=150):
        amount = sum(known, Decimal(0)) if known else None
    return CostStatistics(amount, len(response.points) - len(known))


def _validate_coverage(hours: tuple[datetime, ...], total: Decimal, start: datetime, inclusive_end: datetime) -> None:
    if total != len(hours):
        raise IncompleteCostPeriod("Ответ Timeweb содержит неполное число точек.")
    seen = set(hours)
    if len(seen) != len(hours):
        raise ValueError("В ответе есть повторные интервалы.")

    # Метка точки обозначает час UTC. Начальный и конечный часы могут
    # содержать только часть запрошенного окна, включая часы без запросов.
    first_hour = start.replace(minute=0, second=0, microsecond=0)
    last_hour = inclusive_end.replace(minute=0, second=0, microsecond=0)
    expected_count = (last_hour - first_hour) // _INTERVAL + 1
    expected_hours = {first_hour + index * _INTERVAL for index in range(expected_count)}
    if seen != expected_hours:
        raise IncompleteCostPeriod("Ответ Timeweb не покрывает запрошенный интервал.")

def _inclusive_end(start: datetime, end: datetime) -> datetime:
    if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or start >= end:
        raise ValueError("Timeweb требуется непустой интервал UTC.")
    # Timeweb включает час переданного конца. Переводим полуоткрытый
    # интервал в закрытый с точностью datetime, сохраняя точное начало.
    return end - datetime.resolution


def _utc_isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"


def _reject_json_constant(_: str) -> None:
    raise ValueError("Ожидается конечное JSON-число.")
