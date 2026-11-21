"""Запускает обработку уведомлений независимо от поступления входящих событий."""

from logging import Logger
from threading import Event, Thread

from compadre_assistant.core.presentation.notifications import NotificationSend


def start_notification_worker(
    processor: object, channel: str, send: NotificationSend, stop: Event, logger: Logger,
) -> Thread | None:
    """Проверяет срок доставки в отдельном потоке до остановки polling."""
    dispatch = getattr(processor, "dispatch_notifications", None)
    if not callable(dispatch):
        return None

    def run() -> None:
        while not stop.is_set():
            try:
                dispatch(channel, send)
            except Exception:
                logger.exception("Notification processing failed: channel=%s", channel)
            stop.wait(1)

    thread = Thread(target=run, name=f"{channel}-notifications", daemon=True)
    thread.start()
    return thread
