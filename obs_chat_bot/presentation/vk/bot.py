from __future__ import annotations

import json
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from random import randint
from threading import Condition, Thread
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import BackgroundReply, ChannelCompletionHandler, IncomingResponse
from obs_chat_bot.presentation.shared.safe_send import (
    exponential_retry_delay,
    safe_send,
)


VK_API_VERSION = "5.199"
VK_API_BASE_URL = "https://api.vk.com/method"
VK_SAFE_MESSAGE_LIMIT = 3500
VK_LONG_POLL_WAIT_SECONDS = 25
VK_TRANSIENT_API_ERROR_CODES = frozenset({1, 6, 9, 10, 29})
VK_LONG_POLL_RETRY_BASE_SECONDS = 0.5
VK_LONG_POLL_RETRY_MAX_SECONDS = 30.0
VK_LONG_POLL_RETRY_JITTER_RATIO = 0.2
VK_UPDATE_WORKER_COUNT = 4
VK_UPDATE_QUEUE_SIZE = 128
# При большей степени базовая задержка уже превышает ограничение в 30 секунд.
VK_LONG_POLL_RETRY_MAX_EXPONENT = 6
RetryResult = TypeVar("RetryResult")
IncomingMessageProcessor = Callable[
    [IncomingEvent, ChannelCompletionHandler | None],
    IncomingResponse,
]


class VkBotError(RuntimeError):
    """Ошибка запуска или работы VK adapter."""


class VkTransientError(VkBotError):
    """Временная сетевая или серверная ошибка VK, допускающая повтор запроса."""


def run_vk_bot(
    *,
    token: str,
    group_id: int,
    incoming_message_processor: IncomingMessageProcessor,
    logger: logging.Logger,
    empty_message_reply: str = "Пришли текстовое сообщение или выбери возможность через `/help`.",
    client: VkApiClient | None = None,
    retry_sleeper: Callable[[float], None] = time.sleep,
    retry_jitter: Callable[[float, float], float] = random.uniform,
    worker_count: int = VK_UPDATE_WORKER_COUNT,
    max_queue_size: int = VK_UPDATE_QUEUE_SIZE,
) -> None:
    """Запускает VK Bots Long Poll adapter.

    Args:
        token: Токен доступа VK-группы.
        group_id: ID группы VK.
        incoming_message_processor: Общий обработчик входящих сообщений.
        logger: Логгер runtime-событий.
        empty_message_reply: Ответ на сообщение без текста.
        client: Необязательный VK API-клиент для тестов.
        retry_sleeper: Функция ожидания между повторами.
        retry_jitter: Генератор случайного смещения задержки.
        worker_count: Максимальное число одновременно обрабатываемых обновлений.
        max_queue_size: Максимальное число ожидающих обработки обновлений.
    """
    api_client = client or VkApiClient(token=token)
    long_poll = _get_long_poll_server_with_retry(
        api_client,
        group_id=group_id,
        logger=logger,
        stage="initial_server",
        sleeper=retry_sleeper,
        jitter=retry_jitter,
    )
    workers = _VkUpdateWorkers(
        handler=lambda update: _handle_update(
            update,
            incoming_message_processor=incoming_message_processor,
            client=api_client,
            logger=logger,
            empty_message_reply=empty_message_reply,
        ),
        logger=logger,
        worker_count=worker_count,
        max_queue_size=max_queue_size,
    )
    logger.info("VK bot long polling started for group_id=%s", group_id)
    try:
        while True:
            payload = _retry_long_poll_request(
                lambda: api_client.wait_long_poll(long_poll),
                logger=logger,
                stage="wait",
                sleeper=retry_sleeper,
                jitter=retry_jitter,
            )

            if "failed" in payload:
                long_poll = _recover_long_poll(
                    payload,
                    long_poll,
                    group_id,
                    api_client,
                    logger=logger,
                    sleeper=retry_sleeper,
                    jitter=retry_jitter,
                )
                continue

            long_poll = LongPollServer(
                server=long_poll.server,
                key=long_poll.key,
                ts=str(payload.get("ts", long_poll.ts)),
            )
            for update in payload.get("updates", []):
                if not isinstance(update, dict):
                    logger.error("VK update handling failed: unexpected update format")
                    continue
                if update.get("type") != "message_new":
                    continue
                object_data = update.get("object")
                message = object_data.get("message") if isinstance(object_data, dict) else None
                chat_id = str(message.get("peer_id")) if isinstance(message, dict) else ""
                workers.submit(chat_id, update)
    finally:
        workers.close()


class _VkUpdateWorkers:
    """Выполняет VK updates параллельно с последовательностью внутри чата.

    Ожидающие обновления ограничены; submit блокирует Long Poll при заполнении
    очереди. Во время close принятые обновления исполняются до выхода workers.
    """

    def __init__(
        self,
        *,
        handler: Callable[[dict[str, Any]], None],
        logger: logging.Logger,
        worker_count: int,
        max_queue_size: int,
    ) -> None:
        if worker_count < 1 or max_queue_size < 1:
            raise ValueError("worker_count and max_queue_size must be positive")
        self._handler = handler
        self._logger = logger
        self._condition = Condition()
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        self._ready: deque[str] = deque()
        self._scheduled: set[str] = set()
        self._queued_count = 0
        self._closed = False
        self._max_queue_size = max_queue_size
        self._threads = [
            Thread(target=self._run, name=f"vk-update-{index + 1}")
            for index in range(worker_count)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, chat_id: str, update: dict[str, Any]) -> None:
        """Добавляет update в очередь, ожидая свободного места при переполнении.

        Args:
            chat_id: Идентификатор VK-чата для последовательной обработки.
            update: Исходное обновление Long Poll.

        Raises:
            RuntimeError: Если пул уже закрыт.
        """
        with self._condition:
            while self._queued_count >= self._max_queue_size and not self._closed:
                self._condition.wait()
            if self._closed:
                raise RuntimeError("VK update workers are closed")
            self._queues.setdefault(chat_id, deque()).append(update)
            self._queued_count += 1
            if chat_id not in self._scheduled:
                self._scheduled.add(chat_id)
                self._ready.append(chat_id)
            self._condition.notify_all()

    def close(self) -> None:
        """Останавливает приём и дожидается обработки всех принятых updates."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        for thread in self._threads:
            thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._ready:
                    if self._closed:
                        return
                    self._condition.wait()
                chat_id = self._ready.popleft()
                update = self._queues[chat_id].popleft()
                self._queued_count -= 1
                self._condition.notify_all()
            try:
                self._handler(update)
            except Exception as error:
                self._logger.error("VK update handling failed: %s", error)
            finally:
                with self._condition:
                    if self._queues[chat_id]:
                        self._ready.append(chat_id)
                    else:
                        del self._queues[chat_id]
                        self._scheduled.remove(chat_id)
                    self._condition.notify_all()


class LongPollServer:
    """Параметры VK Bots Long Poll server."""

    def __init__(self, *, server: str, key: str, ts: str) -> None:
        self.server = server
        self.key = key
        self.ts = ts


class VkApiClient:
    """Минимальный VK API client для Bots Long Poll и отправки сообщений."""

    def __init__(self, *, token: str, api_version: str = VK_API_VERSION) -> None:
        if not token.strip():
            raise ValueError("token must not be empty")
        self._token = token
        self._api_version = api_version

    def get_long_poll_server(self, *, group_id: int) -> LongPollServer:
        """Получает VK Bots Long Poll server для группы."""
        response = self._api_call(
            "groups.getLongPollServer",
            {"group_id": str(group_id)},
        )
        data = response.get("response")
        if not isinstance(data, dict):
            raise VkBotError("VK long poll server response has unexpected format")
        return LongPollServer(
            server=str(data["server"]),
            key=str(data["key"]),
            ts=str(data["ts"]),
        )

    def wait_long_poll(self, long_poll: LongPollServer) -> dict[str, Any]:
        """Ожидает события VK Long Poll."""
        params = urlencode(
            {
                "act": "a_check",
                "key": long_poll.key,
                "ts": long_poll.ts,
                "wait": str(VK_LONG_POLL_WAIT_SECONDS),
            }
        )
        return self._request_json(f"{long_poll.server}?{params}")

    def send_message(
        self,
        *,
        peer_id: int,
        text: str,
        random_id: int | None = None,
    ) -> None:
        """Отправляет сообщение VK с опциональным idempotency `random_id`.

        Args:
            peer_id: ID диалога-получателя.
            text: Текст сообщения.
            random_id: Стабильный ID одной retry-серии или `None` для нового ID.
        """
        chunks = split_vk_message(text)
        for chunk in chunks:
            chunk_random_id = (
                random_id
                if random_id is not None and len(chunks) == 1
                else randint(1, 2_147_483_647)
            )
            self._api_call(
                "messages.send",
                {
                    "peer_id": str(peer_id),
                    "message": chunk,
                    "random_id": str(chunk_random_id),
                },
            )

    def _api_call(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        api_params = dict(params)
        api_params["access_token"] = self._token
        api_params["v"] = self._api_version
        encoded_params = urlencode(api_params).encode("utf-8")
        request = Request(
            f"{VK_API_BASE_URL}/{method}",
            data=encoded_params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._request_json(request)

    def _request_json(self, request: str | Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=VK_LONG_POLL_WAIT_SECONDS + 5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            error_type = (
                VkTransientError
                if error.code == 429 or error.code >= 500
                else VkBotError
            )
            raise error_type(f"VK request failed with HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise VkTransientError(
                f"VK request temporarily failed: {type(error).__name__}"
            ) from error

        if not isinstance(payload, dict):
            raise VkBotError("VK response is not an object")
        if "error" in payload:
            error_payload = payload["error"]
            error_code = (
                error_payload.get("error_code")
                if isinstance(error_payload, dict)
                else None
            )
            error_type = (
                VkTransientError
                if error_code in VK_TRANSIENT_API_ERROR_CODES
                else VkBotError
            )
            raise error_type(f"VK API error {error_code}")
        return payload


def split_vk_message(text: str, *, limit: int = VK_SAFE_MESSAGE_LIMIT) -> list[str]:
    """Делит длинный VK-ответ на безопасные части."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = _find_split_position(remaining, limit)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def _handle_update(
    update: dict[str, Any],
    *,
    incoming_message_processor: IncomingMessageProcessor,
    client: VkApiClient,
    logger: logging.Logger,
    empty_message_reply: str = "Пришли текстовое сообщение или выбери возможность через `/help`.",
) -> None:
    if update.get("type") != "message_new":
        return
    object_data = update.get("object")
    if not isinstance(object_data, dict):
        return
    message = object_data.get("message")
    if not isinstance(message, dict):
        return
    text = str(message.get("text") or "")
    peer_id = _require_int(message.get("peer_id"), "peer_id")
    message_id = _require_int(message.get("id"), "id")
    from_id = _require_int(message.get("from_id"), "from_id")

    if not text.strip():
        _safe_send_vk_message(
            client,
            peer_id=peer_id,
            text=empty_message_reply,
            logger=logger,
        )
        return
    incoming_message = IncomingEvent(
        channel="vk",
        chat_id=str(peer_id),
        message_id=str(message_id),
        text=text,
        external_user_id=str(from_id),
    )
    completion_handler = _create_vk_completion_handler(
        client,
        logger=logger,
    )
    result = incoming_message_processor(incoming_message, completion_handler)
    if result.error is not None:
        logger.error("VK incoming message processing failed: %s", result.error)
    _safe_send_vk_message(
        client,
        peer_id=peer_id,
        text=result.render_text(),
        logger=logger,
    )


def _safe_send_vk_message(
    client: Any,
    *,
    peer_id: int,
    text: str,
    logger: logging.Logger,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Отправляет VK-сообщение с per-chunk retries и стабильным `random_id`."""
    chunks = split_vk_message(text)
    started_at = time.monotonic()
    logger.info(
        "VK response send started: peer_id=%s chunk_count=%s",
        peer_id,
        len(chunks),
    )
    for chunk in chunks:
        random_id = randint(1, 2_147_483_647)
        sent = safe_send(
            lambda chunk=chunk, random_id=random_id: client.send_message(
                peer_id=peer_id,
                text=chunk,
                random_id=random_id,
            ),
            logger=logger,
            channel="VK",
            target_id=str(peer_id),
            retry_delay_resolver=_vk_retry_delay,
            sleeper=sleeper,
        )
        if not sent:
            logger.error(
                "VK response send failed: peer_id=%s duration_seconds=%.3f",
                peer_id,
                time.monotonic() - started_at,
            )
            return
    logger.info(
        "VK response send completed: peer_id=%s duration_seconds=%.3f",
        peer_id,
        time.monotonic() - started_at,
    )


def _vk_retry_delay(error: Exception, failed_attempt: int) -> float | None:
    """Возвращает задержку только для типизированных временных ошибок VK."""
    if isinstance(error, VkTransientError):
        return exponential_retry_delay(failed_attempt)
    return None


def _create_vk_completion_handler(
    client: Any,
    *,
    logger: logging.Logger,
) -> ChannelCompletionHandler:
    """Создаёт callback фонового результата в исходный VK peer."""

    def notify(reply: BackgroundReply) -> None:
        """Доставляет нейтральный ответ в сохранённый VK-адрес."""
        if reply.address.channel != "vk":
            raise ValueError("VK adapter получил адрес другого канала")
        _safe_send_vk_message(
            client,
            peer_id=int(reply.address.chat_id),
            text=reply.response.render_text(),
            logger=logger,
        )

    return notify


def _recover_long_poll(
    payload: dict[str, Any],
    long_poll: LongPollServer,
    group_id: int,
    client: VkApiClient,
    logger: logging.Logger,
    sleeper: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> LongPollServer:
    """Восстанавливает параметры Long Poll после штатного ответа `failed`."""
    failed = payload.get("failed")
    if failed == 1:
        return LongPollServer(
            server=long_poll.server,
            key=long_poll.key,
            ts=str(payload.get("ts", long_poll.ts)),
        )
    return _get_long_poll_server_with_retry(
        client,
        group_id=group_id,
        logger=logger,
        stage="refresh_server",
        sleeper=sleeper,
        jitter=jitter,
    )


def _get_long_poll_server_with_retry(
    client: VkApiClient,
    *,
    group_id: int,
    logger: logging.Logger,
    stage: str,
    sleeper: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> LongPollServer:
    """Получает параметры Long Poll, повторяя только временные ошибки.

    Args:
        client: Клиент VK API.
        group_id: Идентификатор VK-группы.
        logger: Логгер диагностических событий.
        stage: Безопасное имя этапа для лога.
        sleeper: Функция ожидания между повторами.
        jitter: Генератор случайного смещения задержки.

    Returns:
        Актуальные параметры Long Poll.

    Raises:
        VkBotError: Если VK вернул постоянную ошибку.
    """
    return _retry_long_poll_request(
        lambda: client.get_long_poll_server(group_id=group_id),
        logger=logger,
        stage=stage,
        sleeper=sleeper,
        jitter=jitter,
    )


def _retry_long_poll_request(
    request: Callable[[], RetryResult],
    *,
    logger: logging.Logger,
    stage: str,
    sleeper: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> RetryResult:
    """Выполняет запрос Long Poll с повтором временных ошибок.

    Счётчик повторов сбрасывается после успешного ответа. В логи попадают только
    этап, номер попытки и задержка, поэтому параметры Long Poll и токены не
    раскрываются даже при ошибке сторонней библиотеки.

    Args:
        request: Операция VK API или Long Poll.
        logger: Логгер диагностических событий.
        stage: Безопасное имя этапа запроса.
        sleeper: Функция ожидания, заменяемая в тестах.
        jitter: Генератор jitter в границах задержки.

    Returns:
        Результат успешного запроса.

    Raises:
        VkBotError: Если запрос завершился постоянной ошибкой.
    """
    failed_attempt = 0
    while True:
        try:
            return request()
        except VkTransientError as error:
            failed_attempt += 1
            delay = _long_poll_retry_delay(failed_attempt, jitter=jitter)
            logger.warning(
                "VK long poll transient failure: stage=%s failed_attempt=%s "
                "delay_seconds=%.3f %s",
                stage,
                failed_attempt,
                delay,
                _safe_network_error_details(error),
            )
            sleeper(delay)


def _long_poll_retry_delay(
    failed_attempt: int,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
) -> float:
    """Возвращает ограниченную экспоненциальную задержку с jitter.

    Args:
        failed_attempt: Порядковый номер неуспешной попытки, начиная с единицы.
        jitter: Генератор случайного смещения задержки.

    Returns:
        Время ожидания в секундах, не превышающее заданный максимум.
    """
    if failed_attempt < 1:
        raise ValueError("failed_attempt must be positive")
    exponent = min(failed_attempt - 1, VK_LONG_POLL_RETRY_MAX_EXPONENT)
    base_delay = min(
        VK_LONG_POLL_RETRY_BASE_SECONDS * 2**exponent,
        VK_LONG_POLL_RETRY_MAX_SECONDS,
    )
    lower_bound = base_delay * (1 - VK_LONG_POLL_RETRY_JITTER_RATIO)
    upper_bound = min(
        VK_LONG_POLL_RETRY_MAX_SECONDS,
        base_delay * (1 + VK_LONG_POLL_RETRY_JITTER_RATIO),
    )
    return jitter(lower_bound, upper_bound)


def _safe_network_error_details(error: VkTransientError) -> str:
    """Возвращает безопасные для лога признаки временной сетевой ошибки.

    Args:
        error: Типизированная временная ошибка VK-запроса.

    Returns:
        Строка с типами исключений и числовым `errno`, если он доступен.
    """
    details = [f"error_type={type(error).__name__}"]
    cause = error.__cause__
    if cause is None:
        return " ".join(details)

    details.append(f"cause_type={type(cause).__name__}")
    if not isinstance(cause, URLError):
        return " ".join(details)

    reason = cause.reason
    details.append(f"reason_type={type(reason).__name__}")
    errno = getattr(reason, "errno", None)
    if isinstance(errno, int) and not isinstance(errno, bool):
        details.append(f"errno={errno}")
    return " ".join(details)


def _find_split_position(text: str, limit: int) -> int:
    window = text[:limit]
    for separator in ("\n\n", "\n", " "):
        position = window.rfind(separator)
        if position > 0:
            return position + len(separator)
    return limit


def _require_int(value: object, field_name: str) -> int:
    if not isinstance(value, int):
        raise VkBotError(f"VK message field is not int: {field_name}")
    return value
