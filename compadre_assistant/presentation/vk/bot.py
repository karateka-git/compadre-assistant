from __future__ import annotations

import json
import logging
import random
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from random import randint
from threading import Condition, Event, Lock, Thread
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    BackgroundReply, ChannelCompletionHandler, CommandMenuUpdate,
    CommandMenuUpdateHandler, IncomingResponse,
)
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.vk.rendering import render_vk_content
from compadre_assistant.presentation.shared.safe_send import (
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
VK_INLINE_ACTIONS_ENABLED = True
# При большей степени базовая задержка уже превышает ограничение в 30 секунд.
VK_LONG_POLL_RETRY_MAX_EXPONENT = 6
RetryResult = TypeVar("RetryResult")
IncomingMessageProcessor = Callable[
    [IncomingEvent, ChannelCompletionHandler | None, CommandMenuUpdateHandler | None],
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
    empty_message_reply: str = "Пришли текстовое сообщение или выбери возможность через /help.",
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
    menu_update_handler = _create_vk_menu_update_handler(api_client, logger=logger)
    runtime_menu_handler = (
        menu_update_handler
        if callable(getattr(incoming_message_processor, "run_menu_reconciliation", None))
        else None
    )
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
            menu_update_handler=runtime_menu_handler,
        ),
        logger=logger,
        worker_count=worker_count,
        max_queue_size=max_queue_size,
    )
    menu_stop = Event()
    menu_thread = _start_vk_menu_reconciliation(
        incoming_message_processor,
        menu_update_handler,
        menu_stop,
        logger=logger,
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
                if update.get("type") not in {"message_new", "message_event"}:
                    continue
                object_data = update.get("object")
                if update["type"] == "message_event":
                    chat_id = str(object_data.get("peer_id")) if isinstance(object_data, dict) else ""
                else:
                    message = object_data.get("message") if isinstance(object_data, dict) else None
                    chat_id = str(message.get("peer_id")) if isinstance(message, dict) else ""
                workers.submit(chat_id, update)
    finally:
        menu_stop.set()
        if menu_thread is not None:
            menu_thread.join(timeout=VK_LONG_POLL_WAIT_SECONDS + 5)
            if menu_thread.is_alive():
                logger.error("VK menu reconciliation did not stop before timeout")
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
        self._inline_messages: OrderedDict[tuple[int, int], str] = OrderedDict()
        self._inline_lock = Lock()

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
        format_data: str | None = None,
        keyboard: str | None = None,
    ) -> None:
        """Отправляет одну готовую часть VK без повторного разбиения.

        Args:
            peer_id: ID диалога-получателя.
            text: Готовый текст части, уже ограниченный renderer канала.
            random_id: Стабильный ID одной retry-серии или `None` для нового ID.
            format_data: JSON диапазонов именно этой части или `None` для plain.
            keyboard: JSON VK-клавиатуры; `None` оставляет обычное сообщение без клавиатуры.
        """
        # Разбиение после рендеринга нарушило бы offsets и границы retry-серии.
        params = {
            "peer_id": str(peer_id),
            "message": text,
            "random_id": str(random_id if random_id is not None else randint(1, 2_147_483_647)),
        }
        if format_data is not None:
            params["format_data"] = format_data
        if keyboard is not None:
            params["keyboard"] = keyboard
        result = self._api_call("messages.send", params).get("response")
        if (keyboard is not None and format_data is None and isinstance(result, int)
                and not isinstance(result, bool) and result > 0):
            try:
                is_inline = json.loads(keyboard).get("inline") is True
            except (ValueError, AttributeError):
                is_inline = False
            if is_inline:
                with self._inline_lock:
                    self._inline_messages[peer_id, result] = text
                    if len(self._inline_messages) > 512:
                        self._inline_messages.popitem(last=False)

    def answer_message_event(self, *, event_id: str, user_id: int, peer_id: int) -> None:
        """Подтверждает нажатие callback-кнопки до обработки действия.

        Args:
            event_id: ID фактического события VK.
            user_id: ID пользователя, присланный VK.
            peer_id: ID исходного диалога.
        """
        self._api_call("messages.sendMessageEventAnswer", {
            "event_id": event_id, "user_id": str(user_id), "peer_id": str(peer_id),
        })

    def clear_inline_keyboard(self, *, peer_id: int, conversation_message_id: int) -> None:
        """Убирает кнопки исходного plain-сообщения при сохранённом тексте.

        Если исходный ответ не найден в памяти или имел format_data, редактирование
        пропускается: VK messages.edit не принимает format_data.
        """
        result = self._api_call("messages.getByConversationMessageId", {
            "peer_id": str(peer_id),
            "conversation_message_ids": str(conversation_message_id),
        }).get("response")
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return
        message = items[0]
        message_id = message.get("id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        with self._inline_lock:
            original_text = self._inline_messages.get((peer_id, message_id))
        if original_text is None or message.get("text") != original_text:
            return
        self._api_call("messages.edit", {
            "peer_id": str(peer_id), "cmid": str(conversation_message_id),
            "message": original_text,
            "keyboard": '{"inline":true,"buttons":[]}',
        })
        with self._inline_lock:
            self._inline_messages.pop((peer_id, message_id), None)

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


def _handle_update(
    update: dict[str, Any],
    *,
    incoming_message_processor: IncomingMessageProcessor,
    client: VkApiClient,
    logger: logging.Logger,
    empty_message_reply: str = "Пришли текстовое сообщение или выбери возможность через /help.",
    menu_update_handler: CommandMenuUpdateHandler | None = None,
) -> None:
    if update.get("type") == "message_event":
        _handle_message_event(
            update, incoming_message_processor=incoming_message_processor,
            client=client, logger=logger, menu_update_handler=menu_update_handler,
        )
        return
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

    if not text:
        _safe_send_vk_message(
            client,
            peer_id=peer_id,
            response=IncomingResponse(text_content(empty_message_reply)),
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
    if menu_update_handler is None:
        result = incoming_message_processor(incoming_message, completion_handler)  # type: ignore[call-arg]
    else:
        result = incoming_message_processor(incoming_message, completion_handler, menu_update_handler)
    if result.error is not None:
        logger.error("VK incoming message processing failed: %s", result.error)
    _safe_send_vk_message(
        client,
        peer_id=peer_id,
        response=result,
        logger=logger,
    )


def _handle_message_event(
    update: dict[str, Any], *, incoming_message_processor: IncomingMessageProcessor,
    client: VkApiClient, logger: logging.Logger,
    menu_update_handler: CommandMenuUpdateHandler | None,
) -> None:
    """Подтверждает VK callback и выбирает действие только по opaque token."""
    data = update.get("object")
    if not isinstance(data, dict):
        return
    peer_id = data.get("peer_id")
    user_id = data.get("user_id")
    event_id = data.get("event_id")
    if (not isinstance(peer_id, int) or isinstance(peer_id, bool)
            or not isinstance(user_id, int) or isinstance(user_id, bool)
            or not isinstance(event_id, str) or not event_id):
        return
    try:
        client.answer_message_event(event_id=event_id, user_id=user_id, peer_id=peer_id)
    except VkBotError as error:
        logger.warning("VK message event ack failed: %s", error)
    payload = data.get("payload")
    if not isinstance(payload, str):
        return
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return
    if not isinstance(parsed, dict) or set(parsed) != {"token"}:
        return
    token = parsed["token"]
    if not isinstance(token, str) or not token:
        return
    select_action = getattr(incoming_message_processor, "select_action", None)
    if not callable(select_action):
        return
    event = IncomingEvent(
        channel="vk", chat_id=str(peer_id), message_id=event_id,
        text=token, external_user_id=str(user_id),
    )
    completion_handler = _create_vk_completion_handler(client, logger=logger)
    if menu_update_handler is None:
        result = select_action(event, token, completion_handler)
    else:
        result = select_action(event, token, completion_handler, menu_update_handler)
    if result.error is not None:
        logger.error("VK action selection failed: %s", result.error)
    cmid = data.get("conversation_message_id")
    if isinstance(cmid, int) and not isinstance(cmid, bool) and cmid > 0:
        try:
            client.clear_inline_keyboard(peer_id=peer_id, conversation_message_id=cmid)
        except VkBotError as error:
            logger.warning("VK inline keyboard cleanup failed: %s", error)
    _safe_send_vk_message(client, peer_id=peer_id, response=result, logger=logger)


def _create_vk_menu_update_handler(
    client: Any, *, logger: logging.Logger,
) -> CommandMenuUpdateHandler:
    """Создаёт обработчик постоянной текстовой клавиатуры VK.

    VK применяет клавиатуру через `messages.send`, поэтому обновление
    сопровождается коротким служебным сообщением. Повтор одной попытки
    сохраняет `random_id`, а переход меню создаёт новую серию публикации.
    """
    state_guard = Lock()
    peer_locks: dict[int, Lock] = {}
    last_attempt: dict[int, tuple[str, int]] = {}

    def publish(update: CommandMenuUpdate) -> None:
        """Отправляет клавиатуру только в личный VK-адрес."""
        if update.address.channel != "vk":
            return
        try:
            peer_id = int(update.address.chat_id)
        except (TypeError, ValueError):
            logger.warning("Skipping VK menu with non-numeric peer ID")
            return
        if not 0 < peer_id < 2_000_000_000:
            logger.warning("Skipping VK menu for a non-private peer")
            return

        items = update.menu.items
        choices = update.menu.pending_choices
        if len(items) > 50:
            raise ValueError("VK keyboard supports at most 50 buttons")
        # Временный выбор не должен вытеснять команды или срывать обновление
        # клавиатуры, если публичный ID длиннее лимита VK.
        valid_choices = tuple(value for value in choices if 1 <= len(value) <= 40)
        visible_choices = valid_choices[:50 - len(items)]
        if len(visible_choices) != len(choices):
            logger.warning("VK pending keyboard choices omitted: limit or invalid label")
        buttons: list[dict[str, object]] = [
            {"action": {"type": "text", "label": value}, "color": "primary"}
            for value in visible_choices
        ]
        for item in items:
            label = item.command
            if not label or len(label) > 40:
                raise ValueError("VK text button command must contain 1-40 characters")
            buttons.append({"action": {"type": "text", "label": label}, "color": "secondary"})
        rows = [buttons[start:start + 5] for start in range(0, len(buttons), 5)]
        keyboard = json.dumps(
            {"one_time": False, "inline": False, "buttons": rows},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with state_guard:
            peer_lock = peer_locks.setdefault(peer_id, Lock())
        with peer_lock:
            previous = last_attempt.get(peer_id)
            if previous is not None and previous[0] == keyboard:
                random_id = previous[1]
            else:
                # Новый ID для перехода A→B→A; повтор того же состояния после
                # ошибки сохраняет ID, а новый runtime автоматически начинает
                # новую retry-серию.
                random_id = randint(1, 2_147_483_647)
                last_attempt[peer_id] = (keyboard, random_id)
            # Ошибка намеренно выходит наружу: reconciliation оставит адрес на повтор.
            client.send_message(
                peer_id=peer_id,
                text="Меню команд обновлено.",
                keyboard=keyboard,
                random_id=random_id,
            )

    return publish


def _start_vk_menu_reconciliation(
    processor: IncomingMessageProcessor,
    handler: CommandMenuUpdateHandler,
    stop: Event,
    *,
    logger: logging.Logger,
) -> Thread | None:
    """Запускает восстановление меню при старте и его периодическую сверку."""
    reconcile = getattr(processor, "run_menu_reconciliation", None)
    if not callable(reconcile):
        return None

    def run() -> None:
        """Сверяет меню до завершения VK runtime."""
        try:
            reconcile(handler, stop)
        except Exception:
            logger.exception("VK menu reconciliation stopped unexpectedly")

    thread = Thread(target=run, name="vk-menu-reconciliation", daemon=True)
    thread.start()
    return thread


def _safe_send_vk_message(
    client: Any,
    *,
    peer_id: int,
    response: IncomingResponse,
    logger: logging.Logger,
    sleeper: Callable[[float], None] = time.sleep,
    limit: int = VK_SAFE_MESSAGE_LIMIT,
    include_bound_actions: bool | None = None,
) -> None:
    """Отправляет VK-сообщение с per-chunk retries и стабильным `random_id`."""
    chunks = render_vk_content(response.content, response.actions, limit=limit)
    show_bound_actions = (
        VK_INLINE_ACTIONS_ENABLED
        if include_bound_actions is None else include_bound_actions
    )
    keyboard = None
    if show_bound_actions:
        try:
            keyboard = _serialize_vk_inline_keyboard(response)
        except ValueError:
            # Необязательная клавиатура не должна скрывать текстовые команды.
            # Решение принимается до API-вызова, поэтому повторной отправки нет.
            logger.warning("VK inline keyboard omitted: invalid action layout")
    started_at = time.monotonic()
    logger.info(
        "VK response send started: peer_id=%s chunk_count=%s",
        peer_id,
        len(chunks),
    )
    for index, chunk in enumerate(chunks):
        random_id = randint(1, 2_147_483_647)
        chunk_keyboard = keyboard if index == len(chunks) - 1 else None
        sent = safe_send(
            lambda chunk=chunk, random_id=random_id, keyboard=chunk_keyboard: client.send_message(
                peer_id=peer_id,
                text=chunk.text,
                format_data=chunk.format_data,
                keyboard=chunk_keyboard,
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


def _serialize_vk_inline_keyboard(response: IncomingResponse) -> str | None:
    """Сериализует только действия, привязанные к конкретному ответу VK.

    Args:
        response: Ответ с явно связанными действиями.

    Returns:
        JSON inline-клавиатуры или `None`, если действий нет.

    Raises:
        ValueError: Если действия нарушают ограничения VK или относятся к
            разным ответам.
    """
    actions = response.bound_actions
    if not actions:
        return None
    # Для inline VK поддерживает до 6 строк и 5 кнопок в строке.
    if len(actions) > 30:
        raise ValueError("VK inline keyboard supports at most 30 buttons")
    if len({action.response_id for action in actions}) != 1:
        raise ValueError("VK inline buttons must belong to one response")
    seen_tokens: set[str] = set()
    rows: list[list[dict[str, object]]] = []
    for start in range(0, len(actions), 5):
        row: list[dict[str, object]] = []
        for action in actions[start:start + 5]:
            token = action.token
            if not token:
                raise ValueError("VK callback token must not be empty")
            if token in seen_tokens:
                raise ValueError("VK callback tokens must be unique")
            seen_tokens.add(token)
            if not action.label or len(action.label) > 40:
                raise ValueError("VK callback label must contain 1-40 characters")
            payload = json.dumps({"token": token}, ensure_ascii=False, separators=(",", ":"))
            if len(payload.encode("utf-8")) > 255:
                raise ValueError("VK callback payload must contain at most 255 bytes")
            row.append({
                "action": {"type": "callback", "label": action.label, "payload": payload},
                "color": "secondary",
            })
        rows.append(row)
    return json.dumps(
        {"one_time": False, "inline": True, "buttons": rows},
        ensure_ascii=False,
        separators=(",", ":"),
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
            response=reply.response,
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


def _require_int(value: object, field_name: str) -> int:
    if not isinstance(value, int):
        raise VkBotError(f"VK message field is not int: {field_name}")
    return value
