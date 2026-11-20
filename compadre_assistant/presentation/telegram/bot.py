from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, InvalidStateError
from concurrent.futures import TimeoutError as FutureTimeoutError
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit

from compadre_assistant.core.presentation.incoming.action_contracts import ActionLayoutLimits
from compadre_assistant.core.presentation.incoming.commands import ChatCommand
from compadre_assistant.core.presentation.incoming.events import (
    ImageAttachment, ImageLoadError, IncomingEvent, ReplyAddress,
)
from compadre_assistant.core.presentation.incoming.ports import (
    BackgroundReply, ChannelCompletionHandler, CommandMenuUpdate,
    CommandMenuUpdateHandler, IncomingResponse,
)
from compadre_assistant.core.presentation.message_content import text_content
from compadre_assistant.presentation.telegram.rendering import TelegramChunk, render_telegram_content
from compadre_assistant.presentation.shared.safe_send import (
    exponential_retry_delay,
    safe_send_async,
)


TELEGRAM_SAFE_MESSAGE_LIMIT = 3900
TELEGRAM_POLLING_RETRY_BASE_SECONDS = 0.5
TELEGRAM_POLLING_RETRY_MAX_SECONDS = 30.0
# При этой степени базовая задержка уже достигает установленного потолка.
TELEGRAM_POLLING_RETRY_MAX_EXPONENT = 6
TELEGRAM_MENU_API_TIMEOUT_SECONDS = 15.0
TELEGRAM_MENU_SHUTDOWN_TIMEOUT_SECONDS = TELEGRAM_MENU_API_TIMEOUT_SECONDS + 2.0
# Ограничения действуют на загруженное фото и на оба запроса к Telegram.
_PHOTO_MAX_BYTES = 10 * 1024 * 1024
_PHOTO_TIMEOUT_SECONDS = 15
# Кнопки разрешены вместе с зарегистрированным ниже callback-handler.
TELEGRAM_INLINE_ACTIONS_ENABLED = True
# Общий бюджет клавиатуры и компактных подписей; ряд Telegram содержит до 8 кнопок.
TELEGRAM_ACTION_LAYOUT = ActionLayoutLimits(
    max_buttons=100, max_rows=100, max_columns=8, max_label_length=64,
)
IncomingMessageProcessor = Callable[
    [IncomingEvent, ChannelCompletionHandler | None, CommandMenuUpdateHandler | None],
    IncomingResponse,
]


class TelegramBotError(RuntimeError):
    """Ошибка запуска Telegram adapter."""


class _FatalPollingError(BaseException):
    """Прерывает внутренний retry aiogram для неустранимой polling-ошибки.

    Aiogram повторяет любые исключения при запросе обновлений, включая ошибки
    неверного токена и одновременного polling. Этот служебный тип наследуется
    от `BaseException`, чтобы такие ошибки не были перехвачены циклом retry.
    """

    def __init__(self, error: Exception) -> None:
        """Сохраняет исходную ошибку Telegram API.

        Args:
            error: Неустранимая ошибка, полученная от Telegram API.
        """
        self.error = error
        super().__init__(type(error).__name__)


def run_telegram_bot(
    *,
    token: str,
    proxy_url: str | None = None,
    incoming_message_processor: IncomingMessageProcessor,
    logger: logging.Logger,
    empty_message_reply: str = "Пришли текстовое сообщение или выбери действие в меню.",
) -> None:
    """Запускает Telegram-бота в polling-режиме.

    Args:
        token: Токен Telegram Bot API.
        proxy_url: Адрес прокси для Bot API или `None` для прямого подключения.
        incoming_message_processor: Обработчик входящих сообщений.
        logger: Логгер безопасных runtime-событий.
        empty_message_reply: Ответ на сообщение без текста и фото.
    """
    try:
        asyncio.run(
            _run_telegram_bot(
                token=token,
                proxy_url=proxy_url,
                incoming_message_processor=incoming_message_processor,
                logger=logger,
                empty_message_reply=empty_message_reply,
            )
        )
    except TelegramBotError:
        raise
    except Exception as error:
        raise TelegramBotError(
            f"Telegram bot failed: {type(error).__name__}"
        ) from error


async def _run_telegram_bot(
    *,
    token: str,
    proxy_url: str | None = None,
    incoming_message_processor: IncomingMessageProcessor,
    logger: logging.Logger,
    empty_message_reply: str = "Пришли текстовое сообщение или выбери действие в меню.",
) -> None:
    """Асинхронно запускает polling Telegram-бота."""
    aiogram = _load_aiogram()
    bot = _create_polling_bot(aiogram, token=token, proxy_url=proxy_url)
    dispatcher = aiogram.Dispatcher()
    reconciliation_stop = threading.Event()
    publish_menu = _create_telegram_menu_update_handler(
        bot, loop=asyncio.get_running_loop(), aiogram=aiogram, logger=logger,
    )

    def menu_update_handler(update: CommandMenuUpdate) -> bool | None:
        if reconciliation_stop.is_set():
            return False
        return publish_menu(update)

    _register_handlers(
        dispatcher,
        aiogram,
        incoming_message_processor=incoming_message_processor,
        logger=logger,
        empty_message_reply=empty_message_reply,
        menu_update_handler=menu_update_handler,
    )

    logger.info("Telegram bot polling started")
    reconciliation_thread = _start_telegram_menu_reconciliation(
        incoming_message_processor,
        menu_update_handler,
        reconciliation_stop,
        logger=logger,
    )
    try:
        await safe_send_async(
            lambda: bot.set_my_commands(commands=_telegram_help_commands(aiogram),
                                        scope=aiogram.types.BotCommandScopeDefault()),
            logger=logger, channel="Telegram", target_id="default", retry_delay_resolver=_telegram_retry_delay,
        )
        await _start_polling_with_retries(
            dispatcher,
            bot,
            aiogram=aiogram,
            logger=logger,
        )
    except _FatalPollingError as error:
        raise TelegramBotError(
            "Telegram polling cannot continue: "
            f"{type(error.error).__name__}"
        ) from error.error
    finally:
        reconciliation_stop.set()
        if reconciliation_thread is not None:
            await asyncio.to_thread(
                reconciliation_thread.join,
                TELEGRAM_MENU_SHUTDOWN_TIMEOUT_SECONDS,
            )
            if reconciliation_thread.is_alive():
                logger.error("Telegram menu reconciliation did not stop before timeout")
        stop_actions = getattr(incoming_message_processor, "close_deferred_actions", None)
        try:
            if callable(stop_actions):
                # Загрузчик уже запущенной работы может ждать этот event loop.
                await asyncio.to_thread(stop_actions)
        finally:
            await bot.session.close()


def _create_polling_bot(
    aiogram: Any,
    *,
    token: str,
    proxy_url: str | None = None,
) -> Any:
    """Создаёт Bot, который не повторяет неустранимые polling-ошибки.

    Временные ошибки намеренно остаются обычными исключениями: их retry и
    backoff выполняет `Dispatcher.start_polling` из aiogram.

    Args:
        aiogram: Загруженный модуль aiogram.
        token: Токен Telegram Bot API.
        proxy_url: Адрес SOCKS/HTTP-прокси либо `None`.

    Returns:
        Экземпляр совместимого с aiogram `Bot`.
    """

    class PollingBot(aiogram.Bot):
        """Передаёт фатальные ошибки polling за пределы retry aiogram."""

        async def __call__(self, method: Any, **kwargs: Any) -> Any:
            """Выполняет Telegram API-вызов с классификацией ошибок polling."""
            try:
                return await super().__call__(method, **kwargs)
            except (
                aiogram.exceptions.TelegramUnauthorizedError,
                aiogram.exceptions.TelegramConflictError,
            ) as error:
                raise _FatalPollingError(error) from error

    if proxy_url is None:
        return PollingBot(token=token)

    from aiogram.client.session.aiohttp import AiohttpSession

    return PollingBot(token=token, session=AiohttpSession(proxy=proxy_url))


async def _start_polling_with_retries(
    dispatcher: Any,
    bot: Any,
    *,
    aiogram: Any,
    logger: logging.Logger,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Запускает polling с retry временных ошибок до цикла обновлений.

    Основной цикл aiogram самостоятельно повторяет ошибки получения обновлений.
    Этот retry нужен для `bot.me()` и других ошибок, которые возникают раньше
    либо выходят из `Dispatcher.start_polling`; он продолжается до успеха.

    Args:
        dispatcher: Настроенный dispatcher aiogram.
        bot: Экземпляр Telegram Bot API-клиента.
        aiogram: Загруженный модуль aiogram.
        logger: Logger Telegram adapter'а.
        sleeper: Асинхронное ожидание между попытками, подменяемое в тестах.

    Raises:
        Exception: Неустранимая ошибка Telegram API.
    """
    failed_attempt = 0
    while True:
        try:
            await dispatcher.start_polling(bot, close_bot_session=False)
            return
        except _FatalPollingError:
            raise
        except Exception as error:
            if not _is_temporary_polling_error(error, aiogram=aiogram):
                raise
            failed_attempt += 1
            delay = _telegram_polling_retry_delay(failed_attempt)
            logger.warning(
                "Telegram polling startup retry: failed_attempt=%s "
                "delay_seconds=%.2f error_type=%s",
                failed_attempt,
                delay,
                type(error).__name__,
            )
            await sleeper(delay)


def _is_temporary_polling_error(error: Exception, *, aiogram: Any) -> bool:
    """Проверяет, можно ли повторить ошибку запуска Telegram polling."""
    return isinstance(
        error,
        (
            aiogram.exceptions.TelegramNetworkError,
            aiogram.exceptions.TelegramServerError,
            ConnectionError,
            TimeoutError,
            OSError,
        ),
    )


def _telegram_polling_retry_delay(failed_attempt: int) -> float:
    """Возвращает ограниченную экспоненциальную задержку retry polling.

    Степень ограничена до возведения в степень, поэтому многодневная
    недоступность Telegram не приводит к росту числа или времени ожидания.
    """
    if failed_attempt < 1:
        raise ValueError("failed_attempt must be positive")
    exponent = min(failed_attempt - 1, TELEGRAM_POLLING_RETRY_MAX_EXPONENT)
    return min(
        TELEGRAM_POLLING_RETRY_BASE_SECONDS * 2**exponent,
        TELEGRAM_POLLING_RETRY_MAX_SECONDS,
    )


def _register_handlers(
    dispatcher: Any,
    aiogram: Any,
    *,
    incoming_message_processor: IncomingMessageProcessor,
    logger: logging.Logger,
    empty_message_reply: str = "Пришли текстовое сообщение или выбери действие в меню.",
    menu_update_handler: CommandMenuUpdateHandler | None = None,
) -> None:
    """Регистрирует минимальные handlers Telegram adapter."""
    router = aiogram.Router()

    @router.message()
    async def handle_message(message: Any) -> None:
        """Передаёт текст или ленивое вложение общему входу приложения."""
        photo = getattr(message, "photo", None)
        if photo is None and not message.text:
            check_busy = getattr(incoming_message_processor, "reply_for_unsupported_input", None)
            address = ReplyAddress("telegram", str(message.chat.id))
            busy = await asyncio.to_thread(check_busy, address) if callable(check_busy) else None
            await safe_send_telegram_reply(
                message,
                busy if isinstance(busy, IncomingResponse) else IncomingResponse(text_content(empty_message_reply)),
                logger=logger,
            )
            return

        loop = asyncio.get_running_loop()
        images = ()
        if photo is not None:
            images = (_telegram_photo_attachment(message.bot, photo, loop),)
        incoming_message = _incoming_message_from_telegram(message, images=images)
        completion_handler = _create_telegram_completion_handler(
            message,
            loop=loop,
            logger=logger,
        )
        result = await _process_telegram_update(
            incoming_message_processor, (incoming_message, completion_handler), menu_update_handler,
        )
        if result.error is not None:
            logger.error("Telegram incoming message processing failed: %s", result.error)
        await safe_send_telegram_reply(message, result, logger=logger)

    @router.callback_query()
    async def handle_action(callback: Any) -> None:
        """Подтверждает нажатие и передаёт непрозрачный token общему маршруту."""
        try:
            await callback.answer()
        except Exception:
            logger.exception("Telegram callback acknowledgement failed")

        source = getattr(callback, "message", None)
        chat = getattr(source, "chat", None)
        source_id = _telegram_message_id(source)
        author = getattr(callback, "from_user", None)
        callback_id = getattr(callback, "id", None)
        if (chat is None or getattr(chat, "id", None) is None
                or author is None or getattr(author, "id", None) is None
                or callback_id is None or not str(callback_id)):
            logger.warning("Telegram callback missing chat, author or callback ID")
            return

        event = IncomingEvent(
            channel="telegram",
            chat_id=str(chat.id),
            message_id=str(callback_id),
            text="callback",
            external_user_id=str(author.id),
            username=getattr(author, "username", None),
            display_name=(getattr(author, "full_name", None)
                          or getattr(author, "first_name", None)),
            callback_message_id=source_id,
            action_layout=TELEGRAM_ACTION_LAYOUT if TELEGRAM_INLINE_ACTIONS_ENABLED else ActionLayoutLimits(),
        )
        target = _TelegramReplyTarget(callback.bot, event.chat_id)
        completion_handler = _create_telegram_completion_handler(
            callback, loop=asyncio.get_running_loop(), logger=logger,
        )
        token = callback.data if isinstance(getattr(callback, "data", None), str) else ""
        select_action = incoming_message_processor.select_action
        result = await _process_telegram_update(select_action, (event, token, completion_handler), menu_update_handler)
        if result.error is not None:
            logger.error("Telegram callback processing failed: %s", result.error)
        managed_target = result.view_delivery.target_message_id if result.view_delivery is not None else None
        delivered = await safe_send_telegram_reply(target, result, logger=logger)
        if (delivered and source_id is not None and not result.preserve_source_actions
                and source_id != managed_target):
            await _clear_telegram_inline_keyboard(target, source_id, logger)

    dispatcher.include_router(router)


async def _process_telegram_update(
    process: Callable[..., IncomingResponse], args: tuple[Any, ...],
    menu_update_handler: CommandMenuUpdateHandler | None,
) -> IncomingResponse:
    """Освобождает квитанцию, если handler отменён до получения ответа worker."""
    if menu_update_handler is not None:
        args += (menu_update_handler,)
    worker = asyncio.create_task(asyncio.to_thread(process, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        def discard(task: asyncio.Task[IncomingResponse]) -> None:
            if task.cancelled() or task.exception() is not None:
                return
            after_delivery = task.result().after_delivery
            if after_delivery is not None:
                cleanup = asyncio.get_running_loop().run_in_executor(None, after_delivery, False)
                cleanup.add_done_callback(_report_telegram_delivery_failure)

        worker.add_done_callback(discard)
        raise


def _report_telegram_delivery_failure(future: asyncio.Future[None]) -> None:
    if not future.cancelled() and (error := future.exception()) is not None:
        logging.getLogger(__name__).error("Telegram delivery notification failed: error_type=%s", type(error).__name__)


async def _notify_telegram_delivery(response: IncomingResponse, delivered: bool) -> None:
    """Подтверждает отправку из потока, откуда callback может использовать event loop."""
    if response.after_delivery is not None:
        await asyncio.to_thread(response.after_delivery, delivered)


def _create_telegram_menu_update_handler(
    bot: Any,
    *,
    loop: asyncio.AbstractEventLoop,
    aiogram: Any,
    logger: logging.Logger,
) -> CommandMenuUpdateHandler:
    """Создаёт синхронный callback публикации меню личного чата Telegram.

    Callback вызывается из рабочего потока processor'а. Он передаёт запрос
    event loop и ждёт ограниченное время, чтобы ошибка Telegram API вернулась
    в механизм повторной сверки, не блокируя сам event loop.
    """

    def publish(update: CommandMenuUpdate) -> bool:
        """Публикует команды только для положительного числового Telegram ID."""
        if update.address.channel != "telegram":
            return False
        try:
            chat_id = int(update.address.chat_id)
        except (TypeError, ValueError):
            logger.warning("Skipping Telegram menu with non-numeric chat ID")
            return False
        if chat_id <= 0:
            logger.warning("Skipping Telegram menu for a non-private chat")
            return False

        scope = aiogram.types.BotCommandScopeChat(chat_id=chat_id)
        future = asyncio.run_coroutine_threadsafe(
            bot.set_my_commands(commands=_telegram_help_commands(aiogram), scope=scope), loop,
        )
        try:
            future.result(timeout=TELEGRAM_MENU_API_TIMEOUT_SECONDS)
        except _FatalPollingError as error:
            # PollingBot прерывает retry при фатальной ошибке. Меню обрабатывается
            # отдельно и должно передать исходную ошибку processor'у для retry.
            raise error.error from error
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError("Telegram menu API call timed out")
        return True

    return publish


def _telegram_help_commands(aiogram: Any) -> list[Any]:
    """Даёт один доступный вход в справку для всех системных меню Telegram."""
    return [aiogram.types.BotCommand(command=ChatCommand.HELP.value.removeprefix("/"), description="Помощь")]


def _start_telegram_menu_reconciliation(
    processor: IncomingMessageProcessor,
    handler: CommandMenuUpdateHandler,
    stop: threading.Event,
    *,
    logger: logging.Logger,
) -> threading.Thread | None:
    """Запускает периодическое восстановление меню, если его поддерживает processor."""
    reconcile = getattr(processor, "run_menu_reconciliation", None)
    if not callable(reconcile):
        return None

    def run() -> None:
        """Выполняет сверку до остановки владельцем Telegram runtime."""
        try:
            reconcile(handler, stop)
        except Exception:
            logger.exception("Telegram menu reconciliation stopped unexpectedly")

    thread = threading.Thread(target=run, name="telegram-menu-reconciliation", daemon=True)
    thread.start()
    return thread


def _create_telegram_completion_handler(
    message: Any,
    *,
    loop: asyncio.AbstractEventLoop,
    logger: logging.Logger,
) -> ChannelCompletionHandler:
    """Создаёт thread-safe callback фонового результата в исходный Telegram chat."""

    bot = message.bot

    def notify(reply: BackgroundReply) -> None:
        """Планирует доставку нейтрального ответа в сохранённый Telegram-адрес."""
        if reply.address.channel != "telegram":
            raise ValueError("Telegram adapter получил адрес другого канала")
        target = _TelegramReplyTarget(bot, reply.address.chat_id)
        try:
            asyncio.run_coroutine_threadsafe(
                safe_send_telegram_reply(target, reply.response, logger=logger),
                loop,
            )
        except RuntimeError as error:
            logger.error(
                "Telegram background reply scheduling failed: %s",
                error,
            )

    return notify


class _TelegramReplyTarget:
    """Доставляет ответ через Bot без удержания исходного Message."""

    def __init__(self, bot: Any, chat_id: str) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.bot = bot

    async def answer(self, text: str, *, parse_mode: str, reply_markup: Any = None) -> Any:
        """Доставляет один фрагмент в сохранённый чат и возвращает сообщение Bot API."""
        kwargs: dict[str, Any] = {"chat_id": self.chat.id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await self.bot.send_message(**kwargs)


class _PhotoBuffer(BytesIO):
    """Останавливает чтение Telegram-файла при превышении лимита."""

    def write(self, data: bytes) -> int:
        """Записывает очередной блок SDK в пределах лимита.

        Args:
            data: Блок байтов в порядке чтения файла.

        Returns:
            Число записанных байтов.

        Raises:
            ImageLoadError: Блок не помещается в оставшийся объём.
        """
        if self.tell() + len(data) > _PHOTO_MAX_BYTES:
            raise ImageLoadError()
        return super().write(data)


def _telegram_photo_attachment(
    bot: Any, variants: list[Any], loop: asyncio.AbstractEventLoop,
) -> ImageAttachment:
    """Сохраняет ленивый доступ к лучшему по разрешению варианту фото."""
    eligible = (
        size for size in variants
        if size.file_id and (size.file_size is None or 0 < size.file_size <= _PHOTO_MAX_BYTES)
    )
    photo = max(
        eligible,
        key=lambda size: size.width * size.height,
        default=None,
    )
    file_id = photo.file_id if photo is not None else None

    async def download() -> bytes:
        """Ограничивает getFile и потоковую загрузку одним таймаутом."""
        try:
            with _PhotoBuffer() as destination:
                async with asyncio.timeout(_PHOTO_TIMEOUT_SECONDS):
                    await bot.download(file_id, destination=destination, seek=False)
                return destination.getvalue()
        except _FatalPollingError:
            # Этот тип предназначен для polling retry и наследуется от BaseException.
            raise ImageLoadError() from None

    def load() -> bytes:
        """Запрашивает загрузку в цикле SDK из синхронного worker."""
        if file_id is None or not loop.is_running():
            raise ImageLoadError()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            raise ImageLoadError()

        result: Future[bytes] = Future()

        def start() -> None:
            """Связывает задачу SDK и ожидание worker после входа в loop."""
            if result.cancelled():
                return
            # Создаём корутину здесь, чтобы при закрытии loop в очереди
            # остался только callback и не потребовалось закрывать корутину.
            task = loop.create_task(download())

            def forward_cancellation(completed: Future[bytes]) -> None:
                if completed.cancelled():
                    try:
                        loop.call_soon_threadsafe(task.cancel)
                    except RuntimeError:
                        pass  # Цикл уже закрыт.

            def finish(completed: asyncio.Task[bytes]) -> None:
                if completed.cancelled():
                    result.cancel()
                    return
                try:
                    if completed.exception() is None:
                        result.set_result(completed.result())
                    else:
                        result.set_exception(ImageLoadError())
                except InvalidStateError:
                    pass  # Worker уже отменил ожидание по таймауту.

            result.add_done_callback(forward_cancellation)
            task.add_done_callback(finish)

        try:
            loop.call_soon_threadsafe(start)
        except RuntimeError:
            raise ImageLoadError() from None
        try:
            # Ограничивает ожидание worker и в случае остановки event loop.
            return result.result(timeout=_PHOTO_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            result.cancel()
            raise ImageLoadError() from None

    return ImageAttachment(load)


def _incoming_message_from_telegram(
    message: Any, *, images: tuple[ImageAttachment, ...] = (),
) -> IncomingEvent:
    """Преобразует Telegram message в application-модель."""
    telegram_user = getattr(message, "from_user", None)
    display_name = None
    if telegram_user is not None:
        display_name = (
            getattr(telegram_user, "full_name", None)
            or getattr(telegram_user, "first_name", None)
        )

    return IncomingEvent(
        channel="telegram",
        chat_id=str(message.chat.id),
        message_id=str(message.message_id),
        text="" if images else (message.text or ""),
        external_user_id=(
            str(telegram_user.id)
            if telegram_user is not None and getattr(telegram_user, "id", None) is not None
            else None
        ),
        username=(
            getattr(telegram_user, "username", None)
            if telegram_user is not None
            else None
        ),
        display_name=display_name,
        images=images,
        action_layout=TELEGRAM_ACTION_LAYOUT if TELEGRAM_INLINE_ACTIONS_ENABLED else ActionLayoutLimits(),
    )


async def send_telegram_reply(
    message: Any,
    response: IncomingResponse,
    *,
    limit: int = TELEGRAM_SAFE_MESSAGE_LIMIT,
) -> None:
    """Отправляет длинный Telegram-ответ безопасными частями.

    Args:
        message: Aiogram message или совместимый fake в тестах.
        response: Нейтральное содержимое ответа и действия.
        limit: Максимальная длина одного сообщения с запасом ниже лимита Telegram.

    Raises:
        ValueError: Лимит не вмещает даже один символ с оформлением.
        Exception: Ошибка отправки готовой части через Telegram API.
    """
    chunks = render_telegram_content(response.content, response.actions, limit=limit)
    for index, chunk in enumerate(chunks):
        keyboard = None
        if TELEGRAM_INLINE_ACTIONS_ENABLED and index == len(chunks) - 1:
            keyboard = _optional_telegram_inline_keyboard(response, logging.getLogger(__name__))
        kwargs: dict[str, Any] = {"parse_mode": chunk.parse_mode}
        if keyboard is not None:
            kwargs["reply_markup"] = keyboard
        await message.answer(chunk.text, **kwargs)


async def safe_send_telegram_reply(
    message: Any,
    response: IncomingResponse,
    *,
    logger: logging.Logger,
    limit: int = TELEGRAM_SAFE_MESSAGE_LIMIT,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    include_bound_actions: bool | None = None,
) -> bool:
    """Доставляет Telegram-ответ и подтверждает управляемое представление.

    Args:
        message: Aiogram message или совместимый fake.
        response: Нейтральное содержимое ответа и действия.
        logger: Logger Telegram adapter'а.
        limit: Безопасный предел длины одного сообщения.
        sleeper: Асинхронное ожидание между тестируемыми retry-попытками.
        include_bound_actions: Показывать ли inline-кнопки общего ответа.
            None использует тот же флаг канала, что и расчёт вместимости входа.

    Returns:
        Успех доставки всех частей. Управляемое обновление требует
        подтверждённый ID; текстовый fallback без кнопок не активирует
        представление. Только после успеха можно снять исходные кнопки.

    Raises:
        ValueError: Лимит не вмещает даже один символ с оформлением.
    """
    if include_bound_actions is None:
        include_bound_actions = TELEGRAM_INLINE_ACTIONS_ENABLED
    delivered = False
    try:
        send = _send_telegram_view if response.view_delivery is not None else _send_telegram_reply
        delivered = await send(
            message, response, logger=logger, limit=limit, sleeper=sleeper,
            include_bound_actions=include_bound_actions,
        )
        if delivered and response.retire_views_after_delivery is not None:
            for message_id in response.retire_views_after_delivery():
                await _clear_telegram_inline_keyboard(message, message_id, logger)
    finally:
        await _notify_telegram_delivery(response, delivered)
    return delivered


async def _clear_telegram_inline_keyboard(message: Any, message_id: str, logger: logging.Logger) -> None:
    """Снимает кнопки старого сообщения, сохраняя уже доставленный ответ."""
    try:
        await message.bot.edit_message_reply_markup(
            chat_id=int(message.chat.id), message_id=int(message_id), reply_markup=None,
        )
    except Exception:
        logger.exception("Telegram inline keyboard removal failed")


def _telegram_message_id(message: Any) -> str | None:
    """Извлекает ID сообщения из объекта Telegram, исключая служебный ноль."""
    identifier = getattr(message, "message_id", None)
    return str(identifier) if type(identifier) is int and identifier > 0 else None


async def _send_telegram_view(
    message: Any, response: IncomingResponse, *, logger: logging.Logger, limit: int,
    sleeper: Callable[[float], Awaitable[None]], include_bound_actions: bool,
) -> bool:
    """Подтверждает ID сообщения с кнопками и даёт ручной путь при сбое."""
    delivery = response.view_delivery
    assert delivery is not None
    target = delivery.target_message_id
    confirmed_id = None
    finish = delivery.finish
    if delivery.start():
        try:
            chunks = render_telegram_content(response.content, response.actions, limit=limit)
            multipart = len(chunks) > 1
            finish_replacement = delivery.finish_replacement
            if finish_replacement is not None and (
                delivery.allow_multiple_messages if multipart else delivery.target_has_multiple_messages
            ):
                finish = lambda message_id: finish_replacement(message_id, multipart)
                confirmed_id = await _send_telegram_view_replacement(
                    message, chunks, response, logger=logger, sleeper=sleeper,
                    include_bound_actions=include_bound_actions,
                )
                if confirmed_id is not None or target is None:
                    return confirmed_id is not None
            elif len(chunks) == 1:
                chunk = chunks[0]
                keyboard = _optional_telegram_inline_keyboard(response, logger) if include_bound_actions else None
                target_id = str(message.chat.id)

                async def deliver() -> None:
                    nonlocal confirmed_id
                    kwargs = {"parse_mode": chunk.parse_mode}
                    if target is None:
                        if keyboard is not None:
                            kwargs["reply_markup"] = keyboard
                        published = await message.answer(chunk.text, **kwargs)
                        confirmed_id = _telegram_message_id(published)
                    else:
                        # Пустая клавиатура снимает кнопки в том же запросе, что меняет текст.
                        markup = keyboard if keyboard is not None else _load_aiogram().types.InlineKeyboardMarkup(
                            inline_keyboard=[],
                        )
                        updated = await message.bot.edit_message_text(
                            text=chunk.text, chat_id=message.chat.id, message_id=int(target),
                            reply_markup=markup, **kwargs,
                        )
                        if _telegram_message_id(updated) == target:
                            confirmed_id = target

                sent = await safe_send_async(
                    deliver, logger=logger, channel="Telegram", target_id=target_id,
                    retry_delay_resolver=_telegram_retry_delay, sleeper=sleeper,
                )
                if confirmed_id is not None or target is None:
                    return sent and confirmed_id is not None
            elif target is None:
                # Разбитый на части ответ сохраняет ручной путь, но не получает цель редактирования.
                return await _send_telegram_reply(
                    message, response, logger=logger, limit=limit,
                    sleeper=sleeper, include_bound_actions=False,
                )
        finally:
            for message_id in finish(confirmed_id):
                await _clear_telegram_inline_keyboard(message, message_id, logger)

    if delivery.unavailable is not None and target is not None:
        await _send_telegram_reply(
            message, IncomingResponse(delivery.unavailable), logger=logger, limit=limit,
            sleeper=sleeper, include_bound_actions=False,
        )
    return False


async def _send_telegram_view_replacement(
    message: Any, chunks: tuple[TelegramChunk, ...], response: IncomingResponse, *,
    logger: logging.Logger, sleeper: Callable[[float], Awaitable[None]], include_bound_actions: bool,
) -> str | None:
    """Отправляет части новой публикации и подтверждает ID сообщения с кнопками."""
    keyboard = _optional_telegram_inline_keyboard(response, logger) if include_bound_actions else None
    confirmed_id = None
    for index, chunk in enumerate(chunks):
        kwargs: dict[str, Any] = {"parse_mode": chunk.parse_mode}
        if keyboard is not None and index == len(chunks) - 1:
            kwargs["reply_markup"] = keyboard

        async def deliver() -> None:
            nonlocal confirmed_id
            confirmed_id = _telegram_message_id(await message.answer(chunk.text, **kwargs))

        sent = await safe_send_async(
            deliver, logger=logger, channel="Telegram", target_id=str(message.chat.id),
            retry_delay_resolver=_telegram_retry_delay, sleeper=sleeper,
        )
        if not sent or confirmed_id is None:
            return None
    return confirmed_id


async def _send_telegram_reply(
    message: Any, response: IncomingResponse, *, logger: logging.Logger, limit: int,
    sleeper: Callable[[float], Awaitable[None]], include_bound_actions: bool,
) -> bool:
    """Доставляет все части ответа и возвращает факт успешной доставки."""
    chunks = render_telegram_content(response.content, response.actions, limit=limit)
    keyboard = (
        _optional_telegram_inline_keyboard(response, logger)
        if include_bound_actions else None
    )
    started_at = time.monotonic()
    target_id = str(message.chat.id)
    logger.info(
        "Telegram response send started: chat_id=%s chunk_count=%s",
        target_id,
        len(chunks),
    )
    for index, chunk in enumerate(chunks):
        send_kwargs: dict[str, Any] = {"parse_mode": chunk.parse_mode}
        if keyboard is not None and index == len(chunks) - 1:
            send_kwargs["reply_markup"] = keyboard
        sent = await safe_send_async(
            lambda chunk=chunk, send_kwargs=send_kwargs: message.answer(chunk.text, **send_kwargs),
            logger=logger,
            channel="Telegram",
            target_id=target_id,
            retry_delay_resolver=_telegram_retry_delay,
            sleeper=sleeper,
        )
        if not sent:
            logger.error(
                "Telegram response send failed: chat_id=%s duration_seconds=%.3f",
                target_id,
                time.monotonic() - started_at,
            )
            return False
    logger.info(
        "Telegram response send completed: chat_id=%s duration_seconds=%.3f",
        target_id,
        time.monotonic() - started_at,
    )
    return bool(chunks)


def _optional_telegram_inline_keyboard(response: IncomingResponse, logger: logging.Logger) -> Any | None:
    """Сохраняет текстовый путь, если действия не помещаются в клавиатуру."""
    try:
        return _serialize_telegram_inline_keyboard(response, _load_aiogram())
    except ValueError:
        # Ошибка локальной валидации известна до API-вызова: можно убрать
        # необязательные кнопки без риска повторной доставки сообщения.
        logger.warning("Telegram inline keyboard omitted: invalid action layout")
        return None


def _serialize_telegram_inline_keyboard(response: IncomingResponse, aiogram: Any) -> Any | None:
    """Собирает клавиатуру из bound actions одного ответа.

    Args:
        response: Ответ с действиями, выданными общим processor'ом.
        aiogram: Модуль aiogram для создания типов Bot API.

    Returns:
        Клавиатуру Telegram или `None`, если действий нет.

    Raises:
        ValueError: Если набор нарушает лимиты Telegram или принадлежит разным ответам.
    """
    actions = response.bound_actions
    if not actions:
        return None
    limits = TELEGRAM_ACTION_LAYOUT
    if len(actions) > limits.max_buttons:
        raise ValueError("Telegram inline keyboard supports at most 100 buttons")
    if len({action.response_id for action in actions}) != 1:
        raise ValueError("Telegram inline buttons must belong to one response")
    rows = []
    seen_tokens: set[str] = set()
    seen_rows: set[int] = set()
    previous_row = None
    for action in actions:
        token = action.token
        if action.url is not None:
            parsed = urlsplit(action.url)
            if token or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Telegram URL button requires an HTTP(S) URL without callback_data")
            button = aiogram.types.InlineKeyboardButton(text=action.label, url=action.url)
        else:
            if not token or len(token.encode("utf-8")) > 64:
                raise ValueError("Telegram callback_data must contain 1-64 bytes")
            if token in seen_tokens:
                raise ValueError("Telegram callback_data tokens must be unique")
            seen_tokens.add(token)
            button = aiogram.types.InlineKeyboardButton(text=action.label, callback_data=token)
        if not action.label or len(action.label) > limits.max_label_length:
            raise ValueError("Telegram inline button text must contain 1-64 characters")
        if action.row is not None and (type(action.row) is not int or action.row < 0):
            raise ValueError("Telegram action row must be a nonnegative integer")
        if action.row is not None and action.row == previous_row:
            rows[-1].append(button)
        else:
            if action.row is not None and action.row in seen_rows:
                raise ValueError("Telegram actions in the same row must be consecutive")
            rows.append([button])
        if action.row is not None:
            seen_rows.add(action.row)
        previous_row = action.row
        if len(rows) > limits.max_rows or len(rows[-1]) > limits.max_columns:
            raise ValueError("Telegram inline keyboard exceeds row or column limit")
    return aiogram.types.InlineKeyboardMarkup(inline_keyboard=rows)


def _telegram_retry_delay(error: Exception, failed_attempt: int) -> float | None:
    """Возвращает задержку только для временных ошибок Telegram API и сети."""
    class_names = {base.__name__ for base in type(error).__mro__}
    if "TelegramRetryAfter" in class_names:
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after >= 0:
            return float(retry_after)
        return exponential_retry_delay(failed_attempt)
    if class_names.intersection({"TelegramNetworkError", "TelegramServerError"}):
        return exponential_retry_delay(failed_attempt)
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return exponential_retry_delay(failed_attempt)
    return None


def _load_aiogram() -> Any:
    """Загружает `aiogram` только при реальном запуске Telegram adapter."""
    try:
        import aiogram
        import aiogram.filters
    except ModuleNotFoundError as error:
        raise TelegramBotError("aiogram is not installed") from error

    return aiogram
