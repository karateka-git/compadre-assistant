from __future__ import annotations

import json
import logging
import random
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from functools import partial
from hashlib import blake2s
from random import randint
from threading import Condition, Event, Lock, Thread
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    BackgroundReply, ChannelCompletionHandler, CommandMenuUpdate,
    CommandMenuUpdateHandler, IncomingResponse,
)
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.vk.photos import vk_photo_attachments
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
# Пустая inline-клавиатура удаляет кнопки редактируемого сообщения.
VK_EMPTY_INLINE_KEYBOARD = '{"inline":true,"buttons":[]}'
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
    menu_stop = Event()
    menu_update_handler = _create_vk_menu_update_handler(api_client, logger=logger, stop=menu_stop)

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
        try:
            workers.close()
        finally:
            stop_actions = getattr(incoming_message_processor, "close_deferred_actions", None)
            if callable(stop_actions):
                stop_actions()


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
    ) -> int | None:
        """Отправляет одну готовую часть VK без повторного разбиения.

        Args:
            peer_id: ID диалога-получателя.
            text: Готовый текст части, уже ограниченный renderer канала.
            random_id: Стабильный ID одной retry-серии или `None` для нового ID.
            format_data: JSON диапазонов именно этой части или `None` для plain.
            keyboard: JSON VK-клавиатуры; `None` оставляет обычное сообщение без клавиатуры.

        Returns:
            Подтверждённый ID сообщения, если VK его вернул.
        """
        # Разбиение после рендеринга нарушило бы offsets и границы retry-серии.
        params = {"peer_id": str(peer_id)}
        if format_data is not None:
            params["format_data"] = format_data
        result = self._send_message(params, text=text, random_id=random_id, keyboard=keyboard)
        if type(result) is not int or result <= 0:
            return None
        if keyboard is not None and format_data is None:
            try:
                is_inline = json.loads(keyboard).get("inline") is True
            except (ValueError, AttributeError):
                is_inline = False
            if is_inline:
                with self._inline_lock:
                    self._inline_messages[peer_id, result] = text
                    if len(self._inline_messages) > 512:
                        self._inline_messages.popitem(last=False)
        return result

    def send_editable_message(
        self, *, peer_id: int, text: str, random_id: int, keyboard: str | None,
    ) -> int:
        """Публикует обычный текст и получает ID сообщения внутри одного диалога.

        Args:
            peer_id: ID диалога-получателя.
            text: Готовый текст одного сообщения без format_data.
            random_id: Стабильный ID retry-серии.
            keyboard: Необязательная inline-клавиатура.

        Returns:
            Положительный conversation_message_id из результата для этого peer.

        Raises:
            VkBotError: VK не подтвердил отправку или не вернул ID диалога и сообщения.
        """
        # Форма peer_ids возвращает cmid, используемый и в callback, и в messages.edit.
        result = self._send_message({"peer_ids": str(peer_id)}, text=text, random_id=random_id, keyboard=keyboard)
        item = result[0] if isinstance(result, list) and len(result) == 1 else None
        if isinstance(item, dict) and type(item.get("peer_id")) is int and item["peer_id"] == peer_id:
            if item.get("error") is not None:
                error = item["error"]
                code = error.get("error_code") if isinstance(error, dict) else None
                if type(code) is int and code in VK_TRANSIENT_API_ERROR_CODES:
                    raise VkTransientError(f"VK individual send error (code {code})")
                raise VkBotError("VK did not send editable message to its recipient")
            cmid = item.get("conversation_message_id")
            if type(cmid) is int and cmid > 0:
                return cmid
        raise VkBotError("VK send returned no confirmed conversation message ID")

    def _send_message(
        self, params: dict[str, str], *, text: str, random_id: int | None, keyboard: str | None,
    ) -> object:
        """Передаёт один готовый текст выбранной форме messages.send."""
        params["message"] = text
        params["random_id"] = str(random_id if random_id is not None else randint(1, 2_147_483_647))
        if keyboard is not None:
            params["keyboard"] = keyboard
        return self._api_call("messages.send", params).get("response")

    def edit_message(
        self, *, peer_id: int, conversation_message_id: int, text: str, keyboard: str | None,
    ) -> None:
        """За один вызов заменяет обычный текст сообщения и его inline-кнопки.

        Args:
            peer_id: Диалог, в котором был доставлен редактируемый ответ.
            conversation_message_id: ID сообщения внутри этого диалога.
            text: Новый текст без format_data.
            keyboard: Новая inline-клавиатура или None для снятия кнопок.

        Raises:
            VkBotError: VK не подтвердил изменение сообщения.
        """
        result = self._api_call("messages.edit", {
            "peer_id": str(peer_id), "cmid": str(conversation_message_id),
            "message": text, "keyboard": keyboard if keyboard is not None else VK_EMPTY_INLINE_KEYBOARD,
        }).get("response")
        if type(result) is not int or result != 1:
            raise VkBotError("VK did not confirm message edit")

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

    def clear_inline_keyboard(
        self, *, peer_id: int, conversation_message_id: int, managed: bool = False,
    ) -> None:
        """Убирает кнопки plain-сообщения, сохраняя его текст.

        Args:
            peer_id: Исходный диалог.
            conversation_message_id: ID сообщения в этом диалоге.
            managed: Ядро подтвердило вывод plain-сообщения из обращения.

        Raises:
            VkBotError: API отклонил получение текста или снятие клавиатуры.
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
        # Для callback без подтверждённого перехода нужны локальные следы plain
        # отправки. У управляемого представления plain гарантирован транспортом.
        original_text = message.get("text")
        if not isinstance(original_text, str):
            return
        if not managed:
            with self._inline_lock:
                if self._inline_messages.get((peer_id, message_id)) != original_text:
                    return
        self.edit_message(peer_id=peer_id, conversation_message_id=conversation_message_id,
                          text=original_text, keyboard=None)
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
    images = vk_photo_attachments(message.get("attachments"))
    text = "" if images else str(message.get("text") or "")
    peer_id = _require_int(message.get("peer_id"), "peer_id")
    message_id = _require_int(message.get("id"), "id")
    from_id = _require_int(message.get("from_id"), "from_id")

    if not text and not images:
        check_busy = getattr(incoming_message_processor, "reply_for_unsupported_input", None)
        busy = check_busy(ReplyAddress("vk", str(peer_id))) if callable(check_busy) else None
        _safe_send_vk_message(
            client,
            peer_id=peer_id,
            response=busy if isinstance(busy, IncomingResponse) else IncomingResponse(text_content(empty_message_reply)),
            logger=logger,
        )
        return
    incoming_message = IncomingEvent(
        channel="vk",
        chat_id=str(peer_id),
        message_id=str(message_id),
        text=text,
        external_user_id=str(from_id),
        images=images,
        menu_choice=not images and _is_vk_menu_choice(message.get("payload")),
    )
    deliver_menu = _response_menu_delivery(incoming_message_processor, menu_update_handler)
    completion_handler = _create_vk_completion_handler(
        client,
        logger=logger,
        origin=incoming_message,
        deliver_menu=deliver_menu,
    )
    if menu_update_handler is None:
        result = incoming_message_processor(incoming_message, completion_handler)  # type: ignore[call-arg]
    elif deliver_menu is None:
        result = incoming_message_processor(incoming_message, completion_handler, menu_update_handler)
    else:
        result = incoming_message_processor(  # type: ignore[call-arg]
            incoming_message, completion_handler, menu_update_handler, defer_origin_menu=True,
        )
    if result.error is not None:
        logger.error("VK incoming message processing failed: %s", result.error)
    _safe_send_vk_message(
        client,
        peer_id=peer_id,
        response=result,
        logger=logger,
        deliver_menu=_menu_delivery_for_event(deliver_menu, incoming_message),
    )


def _is_vk_menu_choice(payload: object) -> bool:
    """Распознаёт маркер текстовой кнопки, который не содержит команд."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return False
    return isinstance(payload, dict) and set(payload) == {"menu"} and payload["menu"] is True


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
    # VK Long Poll может уже разобрать JSON payload в объект, хотя схема
    # описывает строку. Обе формы проходят одинаковую строгую проверку ниже.
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            return
    else:
        parsed = payload
    if not isinstance(parsed, dict) or set(parsed) != {"token"}:
        return
    token = parsed["token"]
    if not isinstance(token, str) or not token:
        return
    select_action = getattr(incoming_message_processor, "select_action", None)
    if not callable(select_action):
        return
    cmid = data.get("conversation_message_id")
    source_id = str(cmid) if type(cmid) is int and cmid > 0 else None
    event = IncomingEvent(
        channel="vk", chat_id=str(peer_id), message_id=event_id,
        text=token, external_user_id=str(user_id), callback_message_id=source_id,
    )
    deliver_menu = _response_menu_delivery(incoming_message_processor, menu_update_handler)
    completion_handler = _create_vk_completion_handler(client, logger=logger, origin=event, deliver_menu=deliver_menu)
    if menu_update_handler is None:
        result = select_action(event, token, completion_handler)
    elif deliver_menu is None:
        result = select_action(event, token, completion_handler, menu_update_handler)
    else:
        result = select_action(event, token, completion_handler, menu_update_handler, defer_origin_menu=True)
    if result.error is not None:
        logger.error("VK action selection failed: %s", result.error)
    managed_target = result.view_delivery.target_message_id if result.view_delivery is not None else None
    if source_id is not None and not result.preserve_source_actions and source_id != managed_target:
        try:
            client.clear_inline_keyboard(peer_id=peer_id, conversation_message_id=int(source_id))
        except VkBotError as error:
            logger.warning("VK inline keyboard cleanup failed: %s", error)
    _safe_send_vk_message(client, peer_id=peer_id, response=result, logger=logger,
                          deliver_menu=_menu_delivery_for_event(deliver_menu, event))


_SendWithMenu = Callable[[CommandMenuUpdate | None], tuple[bool, bool | None]]
_DeliverMenu = Callable[[IncomingEvent, _SendWithMenu], bool]


def _response_menu_delivery(
    processor: IncomingMessageProcessor, handler: CommandMenuUpdateHandler | None,
) -> _DeliverMenu | None:
    deliver = getattr(processor, "deliver_response_menu", None)
    if handler is None or not callable(deliver):
        return None

    def deliver_reply(event: IncomingEvent, send: _SendWithMenu) -> bool:
        return deliver(event, handler, send)

    return deliver_reply


def _menu_delivery_for_event(deliver: _DeliverMenu | None, event: IncomingEvent) -> Callable[[_SendWithMenu], bool] | None:
    return (lambda send: deliver(event, send)) if deliver is not None else None


def _serialize_vk_command_menu(update: CommandMenuUpdate) -> str:
    items = update.menu.items
    if len(items) > 50:
        raise ValueError("VK keyboard supports at most 50 buttons")
    # Сохраняем два столбца, пока это укладывается в предел 10 рядов VK.
    columns = max(2, (len(items) + 9) // 10)
    rows: list[list[dict[str, object]]] = []
    for start in range(0, len(items), columns):
        row: list[dict[str, object]] = []
        for item in items[start:start + columns]:
            label = item.label if item.label is not None else item.command
            if not label or len(label) > 40:
                raise ValueError("VK text button label must contain 1-40 characters")
            action = {"type": "text", "label": label}
            if item.label is not None:
                action["payload"] = '{"menu":true}'
            row.append({"action": action, "color": "secondary"})
        rows.append(row)
    return json.dumps(
        {"one_time": False, "inline": False, "buttons": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _create_vk_menu_update_handler(
    client: Any, *, logger: logging.Logger, stop: Event | None = None,
) -> _VkMenuUpdateHandler:
    """Создаёт обработчик меню для содержательных сообщений VK."""
    return _VkMenuUpdateHandler(client, logger=logger, stop=stop)


class _VkMenuUpdateHandler:
    """Доставляет нижнее меню с уведомлением об истечении ожидания."""

    def __init__(self, client: Any, *, logger: logging.Logger, stop: Event | None) -> None:
        self._client = client
        self._logger = logger
        self._stop = stop

    def send_expiry(
        self, request_id: str, response: IncomingResponse, update: CommandMenuUpdate,
    ) -> bool:
        """Отправляет уведомление с новым меню, связывая повторы по ID ожидания.

        Args:
            request_id: ID истёкшего ожидания для стабильного random_id.
            response: Готовый текст об истечении.
            update: Проверенный снимок нижней клавиатуры исходного чата.

        Returns:
            True, если VK подтвердил доставку уведомления.
        """
        if (self._stop is not None and self._stop.is_set()) or update.address.channel != "vk":
            return False
        try:
            peer_id = int(update.address.chat_id)
        except (TypeError, ValueError):
            self._logger.warning("Skipping VK expiry notice with non-numeric peer ID")
            return False
        if not 0 < peer_id < 2_000_000_000:
            return False
        chunk, = render_vk_content(response.content)
        keyboard = _serialize_vk_command_menu(update)
        digest = blake2s(request_id.encode(), digest_size=4).digest()
        random_id = int.from_bytes(digest, "big") % 2_147_483_647 + 1
        self._client.send_message(
            peer_id=peer_id, text=chunk.text, format_data=chunk.format_data,
            keyboard=keyboard, random_id=random_id,
        )
        return True

    def __call__(self, update: CommandMenuUpdate) -> bool:
        """Оставляет меню без публикации, когда к нему нет сообщения."""
        return False


def _start_vk_menu_reconciliation(
    processor: IncomingMessageProcessor,
    handler: _VkMenuUpdateHandler,
    stop: Event,
    *,
    logger: logging.Logger,
) -> Thread | None:
    """Запускает периодическую сверку ожиданий в личных чатах VK."""
    reconcile = getattr(processor, "run_menu_reconciliation", None)
    if not callable(reconcile):
        return None

    def run() -> None:
        """Сверяет меню до завершения VK runtime."""
        try:
            reconcile(handler, stop, notify_expiry=handler.send_expiry)
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
    deliver_menu: Callable[[_SendWithMenu], bool] | None = None,
) -> None:
    """Доставляет VK-ответ, учитывая отдельное подтверждение нижнего меню."""
    delivered = False
    replacement_confirmed = False

    def confirm_replacement() -> None:
        nonlocal replacement_confirmed
        replacement_confirmed = True

    def send(update: CommandMenuUpdate | None) -> tuple[bool, bool | None]:
        keyboard = None
        if update is not None and update.address == ReplyAddress("vk", str(peer_id)) and 0 < peer_id < 2_000_000_000:
            try:
                keyboard = _serialize_vk_command_menu(update)
            except ValueError:
                logger.warning("VK command menu omitted: invalid keyboard layout")
        deliver = (_send_vk_view if response.view_delivery is not None
                   else partial(_send_vk_message, on_confirmed_reply=confirm_replacement))
        return deliver(
            client, peer_id=peer_id, response=response, logger=logger, sleeper=sleeper,
            limit=limit, include_bound_actions=include_bound_actions, menu_keyboard=keyboard,
        )

    try:
        delivered = (deliver_menu(send) if deliver_menu is not None and response.command_menu_delivery != "preserve"
                     else send(None)[0])
        if replacement_confirmed and response.retire_views_after_delivery is not None:
            _clear_vk_managed_keyboards(client, peer_id, response.retire_views_after_delivery(), logger)
    finally:
        if response.after_delivery is not None:
            response.after_delivery(delivered)


def _clear_vk_managed_keyboards(client: Any, peer_id: int, message_ids: tuple[str, ...], logger: logging.Logger) -> None:
    """Снимает клавиатуры по ID завершённых представлений своего диалога."""
    for message_id in message_ids:
        try:
            client.clear_inline_keyboard(peer_id=peer_id, conversation_message_id=int(message_id), managed=True)
        except VkBotError as error:
            logger.warning("VK inline keyboard removal failed: %s", error)


def _send_vk_view(
    client: Any, *, peer_id: int, response: IncomingResponse, logger: logging.Logger,
    sleeper: Callable[[float], None], limit: int, include_bound_actions: bool | None,
    menu_keyboard: str | None,
) -> tuple[bool, bool | None]:
    """Доставляет одно редактируемое сообщение и, при изменении, нижнее меню."""
    delivery = response.view_delivery
    assert delivery is not None
    target = delivery.target_message_id
    confirmed_id = None
    if delivery.start():
        try:
            chunks = render_vk_content(response.content, response.actions, limit=limit)
            if len(chunks) == 1:
                text = chunks[0].text
                keyboard = _optional_vk_inline_keyboard(response, logger, include_bound_actions)
                random_id = randint(1, 2_147_483_647)

                def publish() -> None:
                    nonlocal confirmed_id
                    cmid = client.send_editable_message(
                        peer_id=peer_id, text=text, keyboard=keyboard, random_id=random_id,
                    )
                    confirmed_id = str(cmid)

                def edit() -> None:
                    nonlocal confirmed_id
                    client.edit_message(
                        peer_id=peer_id, conversation_message_id=int(target), text=text, keyboard=keyboard,
                    )
                    confirmed_id = target

                safe_send(
                    publish if target is None else edit,
                    logger=logger, channel="VK", target_id=str(peer_id),
                    retry_delay_resolver=_vk_retry_delay, sleeper=sleeper,
                )
            elif target is None:
                # Несколько сообщений сохраняют ручной путь без активных inline-кнопок.
                return _send_vk_message(
                    client, peer_id=peer_id, response=response, logger=logger, sleeper=sleeper,
                    limit=limit, include_bound_actions=False, menu_keyboard=menu_keyboard,
                    plain=True,
                )
        finally:
            _clear_vk_managed_keyboards(client, peer_id, delivery.finish(confirmed_id), logger)

    if confirmed_id is not None:
        if menu_keyboard is None:
            return True, None
        # Одна клавиатура на сообщение VK: нижнее меню сопровождает следующую команду.
        followup = IncomingResponse(text_content("Что сделать дальше?"))
        _, menu_delivered = _send_vk_message(
            client, peer_id=peer_id, response=followup, logger=logger, sleeper=sleeper,
            limit=limit, include_bound_actions=False, menu_keyboard=menu_keyboard,
        )
        return True, menu_delivered
    if target is not None and delivery.unavailable is not None:
        _, menu_delivered = _send_vk_message(
            client, peer_id=peer_id, response=IncomingResponse(delivery.unavailable), logger=logger,
            sleeper=sleeper, limit=limit, include_bound_actions=False, menu_keyboard=menu_keyboard,
        )
        return False, menu_delivered
    return False, None


def _send_vk_message(
    client: Any, *, peer_id: int, response: IncomingResponse, logger: logging.Logger,
    sleeper: Callable[[float], None], limit: int, include_bound_actions: bool | None,
    menu_keyboard: str | None, plain: bool = False, on_confirmed_reply: Callable[[], None] | None = None,
) -> tuple[bool, bool | None]:
    """Возвращает результаты доставки ответа и подходящей части с меню."""
    chunks = render_vk_content(response.content, response.actions, limit=limit)
    keyboard = None if menu_keyboard is not None and response.command_menu_delivery == "prefer" else (
        _optional_vk_inline_keyboard(response, logger, include_bound_actions)
    )
    # VK принимает одну клавиатуру на сообщение. У короткого ответа
    # нижнее меню идёт с основным текстом, а кнопки — с отдельной подсказкой.
    reply_chunk_count = len(chunks)
    if menu_keyboard is not None and keyboard is not None and reply_chunk_count == 1:
        hint = "Выбери действие." if response.actions else "Что сделать дальше?"
        chunks += render_vk_content(text_content(hint), limit=limit)
        menu_index = 0
    else:
        menu_index = len(chunks) - (2 if keyboard is not None else 1)
    has_menu = menu_keyboard is not None and menu_index >= 0
    menu_delivered = False if has_menu else None
    main_confirmed = True
    started_at = time.monotonic()
    logger.info(
        "VK response send started: peer_id=%s chunk_count=%s",
        peer_id,
        len(chunks),
    )
    for index, chunk in enumerate(chunks):
        random_id = randint(1, 2_147_483_647)
        chunk_keyboard = (menu_keyboard if has_menu and index == menu_index
                          else keyboard if index == len(chunks) - 1 else None)
        def deliver_chunk() -> None:
            nonlocal main_confirmed
            message_id = client.send_message(
                peer_id=peer_id,
                text=chunk.text,
                format_data=None if plain else chunk.format_data,
                keyboard=chunk_keyboard,
                random_id=random_id,
            )
            if index < reply_chunk_count and message_id is None:
                main_confirmed = False

        sent = safe_send(
            deliver_chunk,
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
            # Сбой отдельной подсказки не отменяет уже доставленный ответ.
            return index >= reply_chunk_count, menu_delivered
        if has_menu and index == menu_index:
            menu_delivered = True
        if index == reply_chunk_count - 1 and main_confirmed and on_confirmed_reply is not None:
            on_confirmed_reply()
    logger.info(
        "VK response send completed: peer_id=%s duration_seconds=%.3f",
        peer_id,
        time.monotonic() - started_at,
    )
    return bool(chunks), menu_delivered


def _optional_vk_inline_keyboard(
    response: IncomingResponse, logger: logging.Logger, enabled: bool | None,
) -> str | None:
    """Сохраняет ручной путь при локальной ошибке сериализации кнопок."""
    if not (VK_INLINE_ACTIONS_ENABLED if enabled is None else enabled):
        return None
    try:
        return _serialize_vk_inline_keyboard(response)
    except ValueError:
        logger.warning("VK inline keyboard omitted: invalid action layout")
        return None


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
    rows: dict[int, list[dict[str, object]]] = {}
    explicit_rows = any(action.row is not None for action in actions)
    for index, action in enumerate(actions):
        row_number = action.row if explicit_rows else index // 5
        if row_number is None or not 0 <= row_number < 6:
            raise ValueError("VK inline keyboard row must be between 0 and 5")
        row = rows.setdefault(row_number, [])
        if len(row) >= 5:
            raise ValueError("VK inline keyboard supports at most 5 buttons per row")
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
    return json.dumps(
        {"one_time": False, "inline": True, "buttons": [rows[index] for index in sorted(rows)]},
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
    origin: IncomingEvent | None = None,
    deliver_menu: _DeliverMenu | None = None,
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
            deliver_menu=_menu_delivery_for_event(deliver_menu, origin)
            if origin is not None and reply.address == origin.reply_address else None,
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
