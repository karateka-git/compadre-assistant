"""Общие helpers безопасной отправки ответов во внешние каналы."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable


DEFAULT_SEND_ATTEMPTS = 3
RetryDelayResolver = Callable[[Exception, int], float | None]


def safe_send(
    send: Callable[[], None],
    *,
    logger: logging.Logger,
    channel: str,
    target_id: str,
    retry_delay_resolver: RetryDelayResolver | None = None,
    max_attempts: int = DEFAULT_SEND_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Выполняет синхронную отправку с ограниченными безопасными retries.

    Args:
        send: Функция отправки сообщения во внешний канал.
        logger: Logger adapter'а.
        channel: Название внешнего канала для диагностики.
        target_id: ID получателя или чата внешнего канала.
        retry_delay_resolver: Возвращает задержку для временной ошибки или `None`,
            если повторять её нельзя.
        max_attempts: Максимальное число попыток, включая первую.
        sleeper: Функция ожидания между попытками.

    Returns:
        `True` после успешной отправки, иначе `False` после записи ошибки в лог.
    """
    _validate_max_attempts(max_attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            send()
            return True
        except Exception as error:
            delay = _resolve_retry_delay(
                error,
                attempt=attempt,
                max_attempts=max_attempts,
                resolver=retry_delay_resolver,
            )
            if delay is None:
                _log_final_failure(
                    logger,
                    channel=channel,
                    target_id=target_id,
                    attempt=attempt,
                    error=error,
                )
                return False
            _log_retry(
                logger,
                channel=channel,
                target_id=target_id,
                attempt=attempt,
                max_attempts=max_attempts,
                delay=delay,
                error=error,
            )
            sleeper(delay)
    return False


async def safe_send_async(
    send: Callable[[], Awaitable[None]],
    *,
    logger: logging.Logger,
    channel: str,
    target_id: str,
    retry_delay_resolver: RetryDelayResolver | None = None,
    max_attempts: int = DEFAULT_SEND_ATTEMPTS,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Выполняет асинхронную отправку с ограниченными безопасными retries.

    Args:
        send: Coroutine factory отправки сообщения во внешний канал.
        logger: Logger adapter'а.
        channel: Название внешнего канала для диагностики.
        target_id: ID получателя или чата внешнего канала.
        retry_delay_resolver: Возвращает задержку для временной ошибки или `None`,
            если повторять её нельзя.
        max_attempts: Максимальное число попыток, включая первую.
        sleeper: Асинхронная функция ожидания между попытками.

    Returns:
        `True` после успешной отправки, иначе `False` после записи ошибки в лог.
    """
    _validate_max_attempts(max_attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            await send()
            return True
        except Exception as error:
            delay = _resolve_retry_delay(
                error,
                attempt=attempt,
                max_attempts=max_attempts,
                resolver=retry_delay_resolver,
            )
            if delay is None:
                _log_final_failure(
                    logger,
                    channel=channel,
                    target_id=target_id,
                    attempt=attempt,
                    error=error,
                )
                return False
            _log_retry(
                logger,
                channel=channel,
                target_id=target_id,
                attempt=attempt,
                max_attempts=max_attempts,
                delay=delay,
                error=error,
            )
            await sleeper(delay)
    return False


def exponential_retry_delay(failed_attempt: int) -> float:
    """Возвращает короткую экспоненциальную задержку для неудачной попытки."""
    if failed_attempt <= 0:
        raise ValueError("failed_attempt must be positive")
    return 0.5 * (2 ** (failed_attempt - 1))


def _resolve_retry_delay(
    error: Exception,
    *,
    attempt: int,
    max_attempts: int,
    resolver: RetryDelayResolver | None,
) -> float | None:
    if resolver is None or attempt >= max_attempts:
        return None
    delay = resolver(error, attempt)
    if delay is None:
        return None
    return max(0.0, float(delay))


def _validate_max_attempts(max_attempts: int) -> None:
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")


def _log_retry(
    logger: logging.Logger,
    *,
    channel: str,
    target_id: str,
    attempt: int,
    max_attempts: int,
    delay: float,
    error: Exception,
) -> None:
    logger.warning(
        "%s message send retry: target_id=%s failed_attempt=%s/%s "
        "delay_seconds=%.2f error_type=%s",
        channel,
        target_id,
        attempt,
        max_attempts,
        delay,
        type(error).__name__,
    )


def _log_final_failure(
    logger: logging.Logger,
    *,
    channel: str,
    target_id: str,
    attempt: int,
    error: Exception,
) -> None:
    logger.error(
        "%s message send failed: target_id=%s attempts=%s error_type=%s error=%s",
        channel,
        target_id,
        attempt,
        type(error).__name__,
        error,
    )
