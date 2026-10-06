from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
import time
from collections.abc import Awaitable, Callable
from typing import Any

from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import BackgroundReply, ChannelCompletionHandler, IncomingResponse
from obs_chat_bot.presentation.shared.safe_send import (
    exponential_retry_delay,
    safe_send_async,
)


TELEGRAM_SAFE_MESSAGE_LIMIT = 3900
TELEGRAM_POLLING_RETRY_BASE_SECONDS = 0.5
TELEGRAM_POLLING_RETRY_MAX_SECONDS = 30.0
# При этой степени базовая задержка уже достигает установленного потолка.
TELEGRAM_POLLING_RETRY_MAX_EXPONENT = 6
IncomingMessageProcessor = Callable[
    [IncomingEvent, ChannelCompletionHandler | None],
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
    empty_message_reply: str = "Пришли текстовое сообщение или выбери возможность через `/help`.",
) -> None:
    """Запускает Telegram-бота в polling-режиме.

    Args:
        token: Токен Telegram Bot API.
        proxy_url: Адрес прокси для Bot API или `None` для прямого подключения.
        incoming_message_processor: Обработчик входящих сообщений.
        logger: Логгер безопасных runtime-событий.
        empty_message_reply: Ответ на сообщение без текста.
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
    empty_message_reply: str = "Пришли текстовое сообщение или выбери возможность через `/help`.",
) -> None:
    """Асинхронно запускает polling Telegram-бота."""
    aiogram = _load_aiogram()
    bot = _create_polling_bot(aiogram, token=token, proxy_url=proxy_url)
    dispatcher = aiogram.Dispatcher()

    _register_handlers(
        dispatcher,
        aiogram,
        incoming_message_processor=incoming_message_processor,
        logger=logger,
        empty_message_reply=empty_message_reply,
    )

    logger.info("Telegram bot polling started")
    try:
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
    empty_message_reply: str = "Пришли текстовое сообщение или выбери возможность через `/help`.",
) -> None:
    """Регистрирует минимальные handlers Telegram adapter."""
    router = aiogram.Router()

    @router.message()
    async def handle_text(message: Any) -> None:
        """Передаёт текстовое сообщение общему входу приложения."""
        if not message.text:
            await safe_send_telegram_reply(
                message,
                empty_message_reply,
                logger=logger,
            )
            return

        incoming_message = _incoming_message_from_telegram(message)
        completion_handler = _create_telegram_completion_handler(
            message,
            loop=asyncio.get_running_loop(),
            logger=logger,
        )
        result = await asyncio.to_thread(
            incoming_message_processor,
            incoming_message,
            completion_handler,
        )
        if result.error is not None:
            logger.error("Telegram incoming message processing failed: %s", result.error)
        await safe_send_telegram_reply(message, result.render_text(), logger=logger)

    dispatcher.include_router(router)


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
        text = reply.response.render_text()
        try:
            asyncio.run_coroutine_threadsafe(
                safe_send_telegram_reply(target, text, logger=logger),
                loop,
            )
        except RuntimeError as error:
            logger.error(
                "Telegram background reply scheduling failed: %s",
                error,
            )

    return notify


class _TelegramReplyTarget:
    """Отправляет фоновый ответ через Bot без удержания исходного Message."""

    def __init__(self, bot: Any, chat_id: str) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self._bot = bot

    async def answer(self, text: str) -> None:
        """Доставляет один фрагмент ответа в сохранённый чат."""
        await self._bot.send_message(chat_id=self.chat.id, text=text)


def _incoming_message_from_telegram(message: Any) -> IncomingEvent:
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
        text=message.text or "",
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
    )


async def send_telegram_reply(
    message: Any,
    text: str,
    *,
    limit: int = TELEGRAM_SAFE_MESSAGE_LIMIT,
) -> None:
    """Отправляет длинный Telegram-ответ безопасными частями.

    Args:
        message: Aiogram message или совместимый fake в тестах.
        text: Текст ответа.
        limit: Максимальная длина одного сообщения с запасом ниже лимита Telegram.
    """
    for chunk in split_telegram_message(text, limit=limit):
        await message.answer(chunk)


async def safe_send_telegram_reply(
    message: Any,
    text: str,
    *,
    logger: logging.Logger,
    limit: int = TELEGRAM_SAFE_MESSAGE_LIMIT,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Отправляет Telegram-ответ с per-chunk retries временных ошибок.

    Args:
        message: Aiogram message или совместимый fake.
        text: Текст ответа.
        logger: Logger Telegram adapter'а.
        limit: Безопасный предел длины одного сообщения.
        sleeper: Асинхронное ожидание между тестируемыми retry-попытками.
    """
    chunks = split_telegram_message(text, limit=limit)
    started_at = time.monotonic()
    target_id = str(message.chat.id)
    logger.info(
        "Telegram response send started: chat_id=%s chunk_count=%s",
        target_id,
        len(chunks),
    )
    for chunk in chunks:
        sent = await safe_send_async(
            lambda chunk=chunk: message.answer(chunk),
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
            return
    logger.info(
        "Telegram response send completed: chat_id=%s duration_seconds=%.3f",
        target_id,
        time.monotonic() - started_at,
    )


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


def split_telegram_message(text: str, *, limit: int = TELEGRAM_SAFE_MESSAGE_LIMIT) -> list[str]:
    """Делит текст на части, не превышающие безопасный лимит Telegram.

    Args:
        text: Исходный ответ.
        limit: Максимальная длина одного фрагмента.

    Returns:
        Непустой список фрагментов для последовательной отправки.
    """
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


def _find_split_position(text: str, limit: int) -> int:
    """Находит позицию разреза по абзацу, строке или пробелу."""
    window = text[:limit]
    for separator in ("\n\n", "\n", " "):
        position = window.rfind(separator)
        if position > 0:
            return position + len(separator)
    return limit


def _load_aiogram() -> Any:
    """Загружает `aiogram` только при реальном запуске Telegram adapter."""
    try:
        import aiogram
        import aiogram.filters
    except ModuleNotFoundError as error:
        raise TelegramBotError("aiogram is not installed") from error

    return aiogram
