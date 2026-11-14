"""Проверяет адаптер статистики Timeweb через подменный HTTP transport."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
import unittest
from uuid import UUID

import httpx

from compadre_assistant.capabilities.admin.data.configuration import AdminTimewebConfig
from compadre_assistant.capabilities.admin.data.timeweb_statistics import TimewebCostStatisticsReader
from compadre_assistant.capabilities.admin.domain.cost_report import (
    CostSourceError, CostStatistics, IncompleteCostPeriod,
)


_START = datetime(2026, 11, 14, 18, 17, tzinfo=UTC)
_END = _START + timedelta(hours=2, minutes=31, microseconds=42)
_TOKEN = "account-test-token"
_KEY = UUID("550e8400-e29b-41d4-a716-446655440000")
_URL = "https://api.timeweb.cloud/api/v1/cloud-ai/ai-keys/statistic/token-usage"
_LOGGER = "compadre_assistant.capabilities.admin.data.timeweb_statistics"


def _point(hour_offset: int, cost: object) -> dict[str, object]:
    point = _START + timedelta(hours=hour_offset)
    return {"time": point.isoformat(), "total_cost": cost}


def _response(points: list[dict[str, object]], total: int | float | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "ai_key_token_statistics": points, "meta": {"total": len(points) if total is None else total},
    })


class TimewebCostStatisticsReaderTest(unittest.TestCase):
    """Проверяет запрос, календарное покрытие и безопасные отказы."""

    def setUp(self) -> None:
        """Подменяет transport и специально включает перенаправления у клиента."""
        self.requests = []
        self.reply = _response([_point(0, 0), _point(1, 0), _point(2, 0)])
        self.failure = None

        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure
            return self.reply

        client = self.enterContext(httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True))
        self.reader = TimewebCostStatisticsReader(AdminTimewebConfig(_TOKEN, _KEY, "RUB"), client)

    def test_get_uses_exact_utc_bounds_and_explicit_key(self) -> None:
        """Запрос отправляет токен аккаунта, а в query идут только параметры ключа и периода."""
        self.assertEqual(self.reader.read_period(_START, _END), CostStatistics(Decimal(0)))
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url.copy_with(query=None)), _URL)
        self.assertEqual(request.headers["Authorization"], f"Bearer {_TOKEN}")
        self.assertEqual(dict(request.url.params), {
            "entity_id": str(_KEY), "entity_type": "aikey", "start_time": "2026-11-14T18:17:00Z",
            "end_time": "2026-11-14T20:48:00.000042Z", "interval": "60",
        })
        self.assertEqual(request.extensions["timeout"]["read"], 3.0)

    def test_sums_raw_decimal_numbers_and_keeps_null_separate(self) -> None:
        """Дроби из JSON не проходят через float, порядок точек не влияет на итог."""
        self.reply = httpx.Response(200, content=(
            b'{"meta":{"total":3.0},"ai_key_token_statistics":['
            b'{"time":"2026-11-14T20:17:00Z","total_cost":null},'
            b'{"time":"2026-11-14T18:17:00Z","total_cost":0.100000000000000001},'
            b'{"time":"2026-11-14T22:17:00+03:00","total_cost":0.2}]}'
        ))
        with localcontext(prec=2):
            self.assertEqual(self.reader.read_period(_START, _END), CostStatistics(Decimal("0.300000000000000001"), 1))

        self.reply = _response([_point(0, None), _point(1, None), _point(2, None)])
        self.assertEqual(self.reader.read_period(_START, _END), CostStatistics(None, 3))
        self.reply = _response([_point(0, None), _point(1, 0), _point(2, 0)])
        self.assertEqual(self.reader.read_period(_START, _END), CostStatistics(Decimal(0), 1))

    def test_missing_or_shifted_points_do_not_become_zero(self) -> None:
        """Пустой, обрезанный, сдвинутый или найденный за границей ответ неполон."""
        for points, total in (
            ([], 0),
            ([_point(0, 0), _point(2, 0)], 2),
            ([_point(0, 0), _point(1, 0), _point(2, 0)], 4),
            ([_point(1, 0), _point(2, 0), _point(3, 0)], 3),
            ([{"time": _START.replace(minute=0).isoformat(), "total_cost": 0}, _point(1, 0), _point(2, 0)], 3),
        ):
            self.reply = _response(points, total)
            with self.subTest(points=points, total=total), self.assertRaises(IncompleteCostPeriod):
                self.reader.read_period(_START, _END)

    def test_malformed_response_has_safe_failure(self) -> None:
        """Повтор, неверная метка, отсутствующая цена и плохой JSON отклоняются."""
        for reply in (
            _response([_point(0, 0), _point(0, 0), _point(2, 0)]),
            _response([_point(0, 0), _point(1, 0), _point(2, 0)], 3.5),
            _response([{"time": _START.isoformat()}, _point(1, 0), _point(2, 0)]),
            _response([{"time": _START.replace(tzinfo=None).isoformat(), "total_cost": 0}, _point(1, 0), _point(2, 0)]),
            _response([_point(0, "0.1"), _point(1, 0), _point(2, 0)]),
            _response([_point(0, True), _point(1, 0), _point(2, 0)]),
            _response([_point(0, -1), _point(1, 0), _point(2, 0)]),
            httpx.Response(200, json={"meta": {"total": 0}, "ai_key_token_statistics": None}),
            httpx.Response(200, text=_TOKEN),
        ):
            self.reply = reply
            with self.assertLogs(_LOGGER, level="WARNING") as logs, self.assertRaises(CostSourceError) as raised:
                self.reader.read_period(_START, _END)
            self.assertIs(type(raised.exception), CostSourceError)
            self.assert_safe(logs.output, raised.exception)

    def test_status_errors_and_redirects_never_disclose_request(self) -> None:
        """Протокол не следует redirect и оставляет в журнале только статус."""
        for status in (302, 400, 401, 403, 404, 429, 500, 503):
            self.reply = httpx.Response(status, headers={"Location": "https://other.invalid"}, text=_TOKEN)
            self.requests.clear()
            with self.subTest(status=status), self.assertLogs(_LOGGER, level="WARNING") as logs, self.assertRaises(CostSourceError) as raised:
                self.reader.read_period(_START, _END)
            self.assertEqual(len(self.requests), 1)
            self.assertIn(f"HTTP {status}", logs.output[0])
            self.assert_safe(logs.output, raised.exception)

    def test_timeout_and_network_error_only_raise_public_failure(self) -> None:
        """Сетевые исключения преобразуются без исходного сообщения и URL."""
        for failure in (httpx.ReadTimeout(_TOKEN), httpx.ConnectError(_URL)):
            self.failure = failure
            with self.assertLogs(_LOGGER, level="WARNING") as logs, self.assertRaises(CostSourceError) as raised:
                self.reader.read_period(_START, _END)
            self.assert_safe(logs.output, raised.exception)

    def test_invalid_bounds_do_not_make_http_request(self) -> None:
        """Пустые интервалы и метки без UTC отклоняются на границе порта."""
        for start, end in ((_START, _START), (_END, _START), (_START.replace(tzinfo=None), _END)):
            with self.assertRaises(ValueError):
                self.reader.read_period(start, end)
        self.assertFalse(self.requests)

    def assert_safe(self, logs: list[str], error: Exception) -> None:
        """Проверяет отсутствие деталей запроса в диагностике и исключении."""
        content = "\n".join([*logs, str(error)])
        for private_value in (_TOKEN, str(_KEY), _URL):
            self.assertNotIn(private_value, content)
