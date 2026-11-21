"""Публичные контракты маршрутизации исходящих уведомлений между возможностями и каналами."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse


@dataclass(frozen=True, slots=True)
class NotificationTarget:
    """Описывает допуск профиля и актуальный личный адрес, если выбран канал."""

    allowed: bool
    address: ReplyAddress | None = None


class NotificationDeclined(Exception):
    """Канал окончательно отклонил сообщение, например бот заблокирован."""


class NotificationRetry(Exception):
    """Канал подтвердил временный отказ до принятия сообщения."""

    def __init__(self, seconds: float) -> None:
        """Сохраняет минимальное ожидание до следующей попытки."""
        super().__init__(seconds)
        self.seconds = seconds


NotificationRoute = Callable[[int], NotificationTarget]
NotificationSend = Callable[[ReplyAddress, IncomingResponse], None]


NotificationDelivery = Callable[[int, ReplyAddress, IncomingResponse], None]


class NotificationSource(Protocol):
    """Предоставляет готовые адресные сообщения подключённой возможности."""

    def dispatch(self, channel: str, now: datetime, route: NotificationRoute, send: NotificationDelivery) -> None:
        """Отправляет созревшие уведомления через выбранный адаптер канала."""
        ...
