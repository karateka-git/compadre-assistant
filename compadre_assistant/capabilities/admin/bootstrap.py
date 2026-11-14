"""Собирает Admin и управляет его клиентом статистики Timeweb."""

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from compadre_assistant.capabilities.admin.data.configuration import load_admin_timeweb_config
from compadre_assistant.capabilities.admin.data.timeweb_statistics import TimewebCostStatisticsReader
from compadre_assistant.capabilities.admin.domain.cost_report import GetGatewayCostReport
from compadre_assistant.capabilities.admin.presentation.handler import AdminCapability


@contextmanager
def create_admin_runtime(*, enabled: bool) -> Iterator[AdminCapability | None]:
    """Создаёт обработчик Admin, владея HTTP-клиентом до выхода из контекста.

    Args:
        enabled: Входит ли Admin в список возможностей процесса.

    Yields:
        Обработчик Admin или None для отключённой возможности.
    """
    if not enabled:
        yield None
        return
    config = load_admin_timeweb_config()
    if config is None:
        yield AdminCapability(GetGatewayCostReport(None, currency=None))
        return
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        reader = TimewebCostStatisticsReader(config, client)
        yield AdminCapability(GetGatewayCostReport(reader, currency=config.currency, count_reader=reader))
