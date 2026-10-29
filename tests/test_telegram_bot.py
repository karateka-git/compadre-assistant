from compadre_assistant.core.presentation.message_content import text_content
"""Тесты низкоуровневых helpers Telegram adapter."""

import asyncio
import logging
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiogram.exceptions import (
    TelegramConflictError as AiogramTelegramConflictError,
    TelegramNetworkError as AiogramTelegramNetworkError,
    TelegramServerError,
    TelegramUnauthorizedError as AiogramTelegramUnauthorizedError,
)
from aiogram.methods import GetMe

from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    BackgroundReply, BoundReplyAction, ConfirmationAction, CommandMenu, CommandMenuUpdate,
    CommandSuggestion, IncomingResponse, ReplyAction,
)
from compadre_assistant.presentation.telegram.bot import (
    TelegramBotError,
    _FatalPollingError,
    _create_telegram_completion_handler,
    _create_telegram_menu_update_handler,
    _register_handlers,
    _create_polling_bot,
    _run_telegram_bot,
    _start_polling_with_retries,
    _start_telegram_menu_reconciliation,
    _telegram_polling_retry_delay,
    safe_send_telegram_reply,
    _serialize_telegram_inline_keyboard,
    _TelegramReplyTarget,
)


class FailingTelegramMessage:
    """Fake Telegram message, который имитирует ошибку отправки."""

    class Chat:
        """Минимальная модель Telegram chat для helper-теста."""

        id = 42

    chat = Chat()

    def __init__(self) -> None:
        self.attempts = 0

    async def answer(self, _text: str, *, parse_mode: str) -> None:
        """Имитирует ошибку Telegram API при отправке ответа."""
        self.attempts += 1
        raise RuntimeError("send failed")


class TelegramNetworkError(RuntimeError):
    """Имитирует типизированную временную сетевую ошибку aiogram."""


class TelegramRetryAfter(RuntimeError):
    """Имитирует Telegram rate limit с обязательной серверной задержкой."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class FakeTelegramSession:
    """Запоминает закрытие HTTP-сессии fake Telegram Bot."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        """Имитирует штатное закрытие HTTP-сессии."""
        self.close_calls += 1


class FakePollingBot:
    """Минимальный Bot для проверки обёртки polling без сети."""

    request_error: Exception | None = None

    def __init__(self, *, token: str, session: FakeTelegramSession | None = None) -> None:
        self.token = token
        self.session = session or FakeTelegramSession()

    async def __call__(self, _method: object, **_kwargs: object) -> str:
        """Возвращает ответ или поднимает настроенную ошибку API."""
        if self.request_error is not None:
            raise self.request_error
        return "ok"

    async def set_my_commands(self, *, commands: list[object], scope: object) -> str:
        """Имитирует setMyCommands через тот же Bot.__call__, что и aiogram."""
        return await self(object(), commands=commands, scope=scope)


class FakeRouter:
    """Поддерживает регистрацию handler'а в тестовом aiogram-модуле."""

    def __init__(self) -> None:
        self.handler = None
        self.callback_handler = None

    def message(self):  # type: ignore[no-untyped-def]
        """Возвращает декоратор, оставляющий handler без изменений."""
        def register(handler):  # type: ignore[no-untyped-def]
            """Сохраняет зарегистрированный обработчик тестового router."""
            self.handler = handler
            return handler

        return register

    def callback_query(self):  # type: ignore[no-untyped-def]
        """Сохраняет обработчик callback-query отдельно от сообщений."""
        def register(handler):  # type: ignore[no-untyped-def]
            self.callback_handler = handler
            return handler

        return register


class FakePollingDispatcher:
    """Сохраняет параметры запуска polling и имитирует его завершение."""

    def __init__(self) -> None:
        self.included_router: FakeRouter | None = None
        self.start_arguments: tuple[FakePollingBot, dict[str, object]] | None = None

    def include_router(self, router: FakeRouter) -> None:
        """Сохраняет зарегистрированный router."""
        self.included_router = router

    async def start_polling(
        self,
        bot: FakePollingBot,
        **kwargs: object,
    ) -> None:
        """Имитирует штатную остановку polling."""
        self.start_arguments = (bot, kwargs)


class FakeAiogram:
    """Минимальная поверхность aiogram для unit-тестов запуска polling."""

    Bot = FakePollingBot
    Router = FakeRouter
    exceptions = SimpleNamespace(
        TelegramConflictError=AiogramTelegramConflictError,
        TelegramNetworkError=AiogramTelegramNetworkError,
        TelegramServerError=TelegramServerError,
        TelegramUnauthorizedError=AiogramTelegramUnauthorizedError,
    )

    def __init__(self) -> None:
        self.dispatcher = FakePollingDispatcher()

    def Dispatcher(self) -> FakePollingDispatcher:
        """Возвращает контролируемый fake dispatcher."""
        return self.dispatcher


class TelegramMessage:
    """Fake Telegram message, сохраняющий отправленные ответы."""

    class Chat:
        """Минимальная модель Telegram chat для helper-теста."""

        id = 42

    chat = Chat()

    def __init__(self) -> None:
        self.bot = self
        self.answers: list[str] = []
        self.parse_modes: list[str] = []
        self.reply_markups: list[object | None] = []

    async def answer(self, text: str, *, parse_mode: str, reply_markup: object = None) -> None:
        """Сохраняет отправленный текст."""
        self.answers.append(text)
        self.parse_modes.append(parse_mode)
        self.reply_markups.append(reply_markup)

    async def send_message(self, *, chat_id: str, text: str, parse_mode: str, reply_markup: object = None) -> None:
        """Доставляет фоновый ответ по сохранённому адресу fake bot."""
        self.sent_chat_id = chat_id
        self.answers.append(text)
        self.parse_modes.append(parse_mode)
        self.reply_markups.append(reply_markup)


class MenuTelegramBot:
    """Сохраняет вызовы публикации меню и умеет имитировать отказ API."""

    def __init__(self) -> None:
        self.menu_calls: list[tuple[list[object], object]] = []
        self.menu_error: Exception | None = None

    async def set_my_commands(self, *, commands: list[object], scope: object) -> None:
        """Запоминает меню либо возвращает настроенную ошибку API."""
        if self.menu_error is not None:
            raise self.menu_error
        self.menu_calls.append((commands, scope))


class FlakyTelegramMessage(TelegramMessage):
    """Один раз роняет выбранный chunk, затем принимает повтор."""

    def __init__(self, failing_chunk: str) -> None:
        super().__init__()
        self.failing_chunk = failing_chunk
        self.calls: list[str] = []
        self.failed = False

    async def answer(self, text: str, *, parse_mode: str) -> None:
        """Сохраняет попытку и один раз имитирует временный сетевой сбой."""
        self.calls.append(text)
        if text == self.failing_chunk and not self.failed:
            self.failed = True
            raise TelegramNetworkError("temporary failure")
        self.answers.append(text)
        self.parse_modes.append(parse_mode)


class RateLimitedTelegramMessage(TelegramMessage):
    """Один раз возвращает Telegram `RetryAfter`, затем принимает сообщение."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def answer(self, text: str, *, parse_mode: str) -> None:
        """Требует задержку 2.5 секунды перед успешным повтором."""
        self.attempts += 1
        if self.attempts == 1:
            raise TelegramRetryAfter(2.5)
        self.answers.append(text)
        self.parse_modes.append(parse_mode)


class TelegramBotHelpersTest(unittest.TestCase):
    """Проверяет helpers, не требующие реального Telegram."""

    def test_bound_actions_serialize_only_opaque_tokens_and_validate_limits(self) -> None:
        """Кнопки используют токены bound actions и Telegram callback limits."""
        from aiogram.types import InlineKeyboardMarkup

        response = IncomingResponse(text_content("Запусти /help"), bound_actions=(
            BoundReplyAction("Справка", "a" * 32, "response-1"),
            BoundReplyAction("Повторить", "b" * 32, "response-1"),
        ))
        keyboard = _serialize_telegram_inline_keyboard(response, __import__("aiogram"))
        self.assertIsInstance(keyboard, InlineKeyboardMarkup)
        self.assertEqual([(row[0].text, row[0].callback_data) for row in keyboard.inline_keyboard],
                         [("Справка", "a" * 32), ("Повторить", "b" * 32)])
        plain_actions = IncomingResponse(
            text_content("Выполни /help"),
            actions=(ConfirmationAction("c1", "Подтвердить", "/confirm x"),),
            suggested_actions=(ReplyAction("Справка", "/help"),),
        )
        self.assertIsNone(_serialize_telegram_inline_keyboard(plain_actions, __import__("aiogram")))
        invalid = IncomingResponse(text_content("x"), bound_actions=(
            BoundReplyAction("Ок", "т" * 33, "response-1"),
        ))
        with self.assertRaises(ValueError):
            _serialize_telegram_inline_keyboard(invalid, __import__("aiogram"))

    def test_keyboard_attaches_to_last_chunk_only(self) -> None:
        """После регистрации callback-handler кнопки прикрепляются к финалу."""
        message = TelegramMessage()
        response = IncomingResponse(text_content("abcdefghij"), bound_actions=(
            BoundReplyAction("Дальше", "x" * 32, "response-1"),
        ))

        async def run() -> None:
            await safe_send_telegram_reply(message, response, logger=logging.getLogger("test.telegram.inline"), limit=4)

        asyncio.run(run())
        self.assertEqual(message.reply_markups[:2], [None, None])
        self.assertEqual(message.reply_markups[2].inline_keyboard[0][0].callback_data, "x" * 32)
        message.answers.clear()
        message.reply_markups.clear()

        async def enabled_run() -> None:
            await safe_send_telegram_reply(
                message, response, logger=logging.getLogger("test.telegram.inline"), limit=4,
                include_bound_actions=True,
            )

        asyncio.run(enabled_run())
        self.assertEqual(message.answers, ["abcd", "efgh", "ij"])
        self.assertEqual(message.reply_markups[:2], [None, None])
        self.assertEqual(message.reply_markups[2].inline_keyboard[0][0].callback_data, "x" * 32)

    def test_background_keyboard_is_sent_with_original_reply(self) -> None:
        """Фоновая доставка использует тот же сериализатор и адрес ответа."""
        class Bot:
            """Запоминает адрес и markup фонового Bot API-вызова."""

            async def send_message(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        bot = Bot()
        target = _TelegramReplyTarget(bot, "42")
        response = IncomingResponse(text_content("Готово"), bound_actions=(
            BoundReplyAction("Открыть", "z" * 32, "response-1"),
        ))

        async def run() -> None:
            await safe_send_telegram_reply(
                target, response, logger=logging.getLogger("test.telegram.inline.background"),
                include_bound_actions=True,
            )

        asyncio.run(run())
        self.assertEqual(bot.kwargs["chat_id"], "42")
        self.assertEqual(bot.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "z" * 32)

    def test_final_chunk_retry_keeps_keyboard_bound_to_that_response(self) -> None:
        """Retry последнего фрагмента не переносит кнопку на иной текст."""
        class FailingLastChunk(TelegramMessage):
            """Один раз отклоняет последний фрагмент и сохраняет обе попытки."""

            def __init__(self) -> None:
                super().__init__()
                self.attempt_markups: list[object | None] = []
                self.failed_last_chunk = False

            async def answer(self, text: str, *, parse_mode: str, reply_markup: object = None) -> None:
                self.attempt_markups.append(reply_markup)
                if text == "ij" and not self.failed_last_chunk:
                    self.failed_last_chunk = True
                    raise TelegramNetworkError("temporary failure")
                await super().answer(text, parse_mode=parse_mode, reply_markup=reply_markup)

        message = FailingLastChunk()
        response = IncomingResponse(text_content("abcdefghij"), bound_actions=(
            BoundReplyAction("Дальше", "y" * 32, "response-2"),
        ))
        delays: list[float] = []

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        async def run() -> None:
            await safe_send_telegram_reply(
                message, response, logger=logging.getLogger("test.telegram.inline.retry"), limit=4,
                include_bound_actions=True, sleeper=sleeper,
            )

        with self.assertLogs("test.telegram.inline.retry", level="WARNING"):
            asyncio.run(run())
        self.assertEqual(message.answers, ["abcd", "efgh", "ij"])
        self.assertIsNone(message.attempt_markups[0])
        self.assertIsNone(message.attempt_markups[1])
        self.assertIsNotNone(message.attempt_markups[2])
        self.assertIs(message.attempt_markups[2], message.attempt_markups[3])
        self.assertEqual(delays, [0.5])

    def test_handler_delivers_general_progress_reply(self) -> None:
        """Telegram отправляет раннюю квитанцию по сигналу возможности."""
        aiogram = FakeAiogram()
        message = TelegramMessage()
        message.text = "долгое действие"
        message.message_id = 7
        message.from_user = None

        def processor(_event, _completion_handler) -> IncomingResponse:
            """Сохраняет ответы, отправленные до вызова worker."""
            _completion_handler(BackgroundReply(_event.reply_address, IncomingResponse(text_content("Принял, обрабатываю"))))
            return IncomingResponse(text_content("Готово"))

        _register_handlers(
            aiogram.dispatcher,
            aiogram,
            incoming_message_processor=processor,
            logger=logging.getLogger("test.telegram.ack"),
        )
        asyncio.run(aiogram.dispatcher.included_router.handler(message))

        self.assertEqual(len(message.answers), 2)
        self.assertIn("Принял, обрабатываю", message.answers[0])
        self.assertEqual(message.answers[-1], "Готово")

    def test_callback_ack_selects_opaque_token_and_removes_keyboard(self) -> None:
        """Callback подтверждается до выбора и передаёт фактическую identity."""
        order: list[str] = []

        class Bot:
            """Запоминает очистку markup и ответ callback."""

            async def edit_message_reply_markup(self, **kwargs: object) -> None:
                order.append("edit")
                self.edit_kwargs = kwargs

            async def send_message(self, **kwargs: object) -> None:
                order.append("send")
                self.sent = kwargs

        class Callback:
            """Имитирует Telegram callback с чужим текстом исходного сообщения."""

            def __init__(self) -> None:
                self.bot = Bot()
                self.id = "callback-7"
                self.data = "opaque-token"
                self.from_user = SimpleNamespace(id=99, username="tester", full_name="Автор")
                self.message = SimpleNamespace(
                    chat=SimpleNamespace(id=42), message_id=18, text="/disable obsidian",
                    from_user=SimpleNamespace(id=123),
                )

            async def answer(self) -> None:
                order.append("ack")

        class Processor:
            """Фиксирует только API выбора, не разбирая callback текст."""

            def select_action(self, event, token, completion, menu=None):  # type: ignore[no-untyped-def]
                order.append("select")
                self.selected = event, token, completion, menu
                return IncomingResponse(text_content("Выбрано"))

        aiogram = FakeAiogram()
        processor = Processor()
        callback = Callback()
        _register_handlers(
            aiogram.dispatcher, aiogram, incoming_message_processor=processor,
            logger=logging.getLogger("test.telegram.callback"),
        )
        asyncio.run(aiogram.dispatcher.included_router.callback_handler(callback))
        event, token, completion, menu = processor.selected
        self.assertEqual(order, ["ack", "select", "edit", "send"])
        self.assertEqual((event.channel, event.chat_id, event.message_id, event.external_user_id),
                         ("telegram", "42", "callback-7", "99"))
        self.assertEqual((event.username, event.display_name, event.text),
                         ("tester", "Автор", "callback"))
        self.assertEqual(token, "opaque-token")
        self.assertTrue(callable(completion))
        self.assertIsNone(menu)
        self.assertEqual(callback.bot.edit_kwargs,
                         {"chat_id": 42, "message_id": 18, "reply_markup": None})
        self.assertEqual(callback.bot.sent["text"], "Выбрано")

    def test_invalid_or_repeated_callback_and_edit_error_keep_response(self) -> None:
        """Неверный и повторный token остаются отказом даже при сбое edit API."""
        class Bot:
            """Отклоняет очистку клавиатуры, но принимает ответ."""

            def __init__(self) -> None:
                self.answers: list[str] = []

            async def edit_message_reply_markup(self, **_kwargs: object) -> None:
                raise RuntimeError("edit failed")

            async def send_message(self, **kwargs: object) -> None:
                self.answers.append(str(kwargs["text"]))

        class Callback:
            """Сохраняет факт acknowledgement каждого нажатия."""

            def __init__(self, token: str) -> None:
                self.bot = bot
                self.id = token + "-id"
                self.data = token
                self.from_user = SimpleNamespace(id=42)
                self.message = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=7)
                self.acked = False

            async def answer(self) -> None:
                self.acked = True

        class Processor:
            """Имитирует одноразовое принятие token общим маршрутом."""

            def __init__(self) -> None:
                self.used: set[str] = set()
                self.executions = 0

            def select_action(self, _event, token, _completion):  # type: ignore[no-untyped-def]
                if token != "valid" or token in self.used:
                    return IncomingResponse(text_content("Кнопка недоступна"))
                self.used.add(token)
                self.executions += 1
                return IncomingResponse(text_content("Выполнено"))

        bot = Bot()
        processor = Processor()
        aiogram = FakeAiogram()
        _register_handlers(
            aiogram.dispatcher, aiogram, incoming_message_processor=processor,
            logger=logging.getLogger("test.telegram.callback.edit"),
        )
        callbacks = [Callback("bad"), Callback("valid"), Callback("valid")]
        async def run() -> None:
            for callback in callbacks:
                await aiogram.dispatcher.included_router.callback_handler(callback)

        with self.assertLogs("test.telegram.callback.edit", level="ERROR"):
            asyncio.run(run())
        self.assertTrue(all(callback.acked for callback in callbacks))
        self.assertEqual(processor.executions, 1)
        self.assertEqual(bot.answers, ["Кнопка недоступна", "Выполнено", "Кнопка недоступна"])

    def test_callback_keeps_background_reply_and_menu_update_path(self) -> None:
        """Выбор доставляет фоновый ответ и передаёт callback меню processor'у."""
        class Bot:
            """Сохраняет оба ответа через общий Bot API."""

            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []

            async def edit_message_reply_markup(self, **_kwargs: object) -> None:
                pass

            async def send_message(self, **kwargs: object) -> None:
                self.sent.append(kwargs)

        class Callback:
            """Предоставляет минимальную identity и адрес callback."""

            bot = Bot()
            id = "cb-background"
            data = "token"
            from_user = SimpleNamespace(id=42)
            message = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=5)

            async def answer(self) -> None:
                pass

        class Processor:
            """Выдаёт фоновый ответ и обновление меню при выборе."""

            def select_action(self, event, token, completion, menu):  # type: ignore[no-untyped-def]
                self.args = event, token
                completion(BackgroundReply(event.reply_address, IncomingResponse(text_content("Фон"))))
                menu(CommandMenuUpdate(event.reply_address, CommandMenu()))
                return IncomingResponse(text_content("Итог"))

        aiogram = FakeAiogram()
        processor = Processor()
        updates: list[CommandMenuUpdate] = []
        _register_handlers(
            aiogram.dispatcher, aiogram, incoming_message_processor=processor,
            logger=logging.getLogger("test.telegram.callback.background"),
            menu_update_handler=updates.append,
        )

        async def run() -> None:
            await aiogram.dispatcher.included_router.callback_handler(Callback())
            for _ in range(10):
                if len(Callback.bot.sent) == 2:
                    break
                await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual({item["text"] for item in Callback.bot.sent}, {"Фон", "Итог"})
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].address, ReplyAddress("telegram", "42"))

    def test_menu_publication_uses_chat_scope_and_exact_commands(self) -> None:
        """Меню публикуется в личном scope с точными именами и описаниями."""
        from aiogram.types import BotCommandScopeChat

        bot = MenuTelegramBot()
        loop = asyncio.new_event_loop()
        try:
            handler = _create_telegram_menu_update_handler(
                bot, loop=loop, aiogram=__import__("aiogram"),
                logger=logging.getLogger("test.telegram.menu"),
            )
            update = CommandMenuUpdate(
                ReplyAddress("telegram", "42"),
                CommandMenu((CommandSuggestion("/help", "Показать справку"),
                             CommandSuggestion("/ask", "Задать вопрос"))),
            )
            async def invoke() -> None:
                await asyncio.to_thread(handler, update)
            loop.run_until_complete(invoke())
        finally:
            loop.close()
        self.assertEqual(len(bot.menu_calls), 1)
        commands, scope = bot.menu_calls[0]
        self.assertEqual([(item.command, item.description) for item in commands],
                         [("help", "Показать справку"), ("ask", "Задать вопрос")])
        self.assertIsInstance(scope, BotCommandScopeChat)
        self.assertEqual(scope.chat_id, 42)

    def test_empty_menu_clears_commands_and_foreign_or_group_addresses_are_ignored(self) -> None:
        """Пустой список очищает меню, а другой канал и group не получают публикацию."""
        bot = MenuTelegramBot()
        loop = asyncio.new_event_loop()
        try:
            handler = _create_telegram_menu_update_handler(
                bot, loop=loop, aiogram=__import__("aiogram"),
                logger=logging.getLogger("test.telegram.menu"),
            )
            async def invoke() -> None:
                await asyncio.to_thread(handler, CommandMenuUpdate(
                    ReplyAddress("telegram", "42"), CommandMenu(),
                ))
                await asyncio.to_thread(handler, CommandMenuUpdate(
                    ReplyAddress("vk", "42"), CommandMenu(),
                ))
                await asyncio.to_thread(handler, CommandMenuUpdate(
                    ReplyAddress("telegram", "-10042"), CommandMenu(),
                ))
                await asyncio.to_thread(handler, CommandMenuUpdate(
                    ReplyAddress("telegram", "not-an-id"), CommandMenu(),
                ))
            loop.run_until_complete(invoke())
        finally:
            loop.close()
        self.assertEqual(len(bot.menu_calls), 1)
        self.assertEqual(bot.menu_calls[0][0], [])

    def test_menu_api_error_propagates_for_processor_retry(self) -> None:
        """Ошибка Telegram API выходит из callback, чтобы processor повторил публикацию."""
        bot = MenuTelegramBot()
        loop = asyncio.new_event_loop()
        try:
            handler = _create_telegram_menu_update_handler(
                bot, loop=loop, aiogram=__import__("aiogram"),
                logger=logging.getLogger("test.telegram.menu"),
            )
            update = CommandMenuUpdate(ReplyAddress("telegram", "42"), CommandMenu())
            async def invoke() -> None:
                await asyncio.to_thread(handler, update)
            bot.menu_error = RuntimeError("telegram unavailable")
            with self.assertRaisesRegex(RuntimeError, "telegram unavailable"):
                loop.run_until_complete(invoke())
            bot.menu_error = None
            loop.run_until_complete(invoke())
        finally:
            loop.close()
        self.assertEqual(len(bot.menu_calls), 1)

    def test_menu_unwraps_polling_fatal_error_for_processor_retry(self) -> None:
        """Фатальная polling-обёртка меню возвращает в processor исходный API error."""
        unauthorized = AiogramTelegramUnauthorizedError(GetMe(), "Unauthorized")
        FakePollingBot.request_error = unauthorized
        bot = _create_polling_bot(FakeAiogram(), token="token")
        loop = asyncio.new_event_loop()
        try:
            handler = _create_telegram_menu_update_handler(
                bot, loop=loop, aiogram=__import__("aiogram"),
                logger=logging.getLogger("test.telegram.menu"),
            )
            async def invoke() -> None:
                await asyncio.to_thread(handler, CommandMenuUpdate(
                    ReplyAddress("telegram", "42"), CommandMenu(),
                ))
            with self.assertRaises(AiogramTelegramUnauthorizedError) as raised:
                loop.run_until_complete(invoke())
            self.assertIs(raised.exception, unauthorized)
        finally:
            loop.close()
            FakePollingBot.request_error = None

    def test_reconciliation_starts_immediately_and_stops_with_runtime(self) -> None:
        """Фоновая сверка запускается и завершается по сигналу владельца runtime."""
        class ReconciliationProcessor:
            """Фиксирует callback первой сверки и ожидает stop."""

            def __init__(self) -> None:
                self.called = threading.Event()

            def run_menu_reconciliation(self, handler, stop) -> None:  # type: ignore[no-untyped-def]
                """Имитирует первый проход и периодическое ожидание."""
                handler(CommandMenuUpdate(ReplyAddress("telegram", "42"), CommandMenu()))
                self.called.set()
                stop.wait()

        processor = ReconciliationProcessor()
        stop = threading.Event()
        updates: list[CommandMenuUpdate] = []
        thread = _start_telegram_menu_reconciliation(
            processor, updates.append, stop,
            logger=logging.getLogger("test.telegram.reconciliation"),
        )
        self.assertIsNotNone(thread)
        self.assertTrue(processor.called.wait(1))
        self.assertEqual(len(updates), 1)
        stop.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_safe_send_telegram_reply_does_not_raise_when_send_fails(self) -> None:
        """Ошибка отправки Telegram-сообщения логируется и не роняет handler."""
        message = FailingTelegramMessage()
        logger = logging.getLogger("test.telegram.safe_send")

        async def run() -> None:
            await safe_send_telegram_reply(
                message,
                IncomingResponse(text_content("hello")),
                logger=logger,
            )

        with self.assertLogs(logger, level="ERROR") as logs:
            asyncio.run(run())
        self.assertIn("Telegram message send failed", logs.output[0])
        self.assertEqual(message.attempts, 1)

    def test_safe_send_retries_only_failed_telegram_chunk(self) -> None:
        """Временный сбой не дублирует уже отправленные части длинного ответа."""
        message = FlakyTelegramMessage("efgh")
        logger = logging.getLogger("test.telegram.retry")
        delays: list[float] = []

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        async def run() -> None:
            await safe_send_telegram_reply(
                message,
                IncomingResponse(text_content("abcdefghij")),
                logger=logger,
                limit=4,
                sleeper=sleeper,
            )

        with self.assertLogs(logger, level="WARNING") as logs:
            asyncio.run(run())

        self.assertEqual(message.calls, ["abcd", "efgh", "efgh", "ij"])
        self.assertEqual(message.answers, ["abcd", "efgh", "ij"])
        self.assertEqual(delays, [0.5])
        self.assertIn("failed_attempt=1/3", logs.output[0])

    def test_safe_send_respects_telegram_retry_after(self) -> None:
        """Rate limit Telegram задаёт задержку вместо стандартного backoff."""
        message = RateLimitedTelegramMessage()
        logger = logging.getLogger("test.telegram.retry_after")
        delays: list[float] = []

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        async def run() -> None:
            await safe_send_telegram_reply(
                message,
                IncomingResponse(text_content("hello")),
                logger=logger,
                sleeper=sleeper,
            )

        with self.assertLogs(logger, level="WARNING"):
            asyncio.run(run())

        self.assertEqual(message.answers, ["hello"])
        self.assertEqual(message.attempts, 2)
        self.assertEqual(delays, [2.5])

    def test_polling_bot_leaves_temporary_error_for_aiogram_retry(self) -> None:
        """Сетевая ошибка остаётся обычной, чтобы её повторил aiogram."""
        FakePollingBot.request_error = AiogramTelegramNetworkError(
            GetMe(),
            "temporary failure",
        )
        bot = _create_polling_bot(FakeAiogram(), token="token")

        async def run() -> None:
            with self.assertRaises(AiogramTelegramNetworkError):
                await bot(object())

        try:
            asyncio.run(run())
        finally:
            FakePollingBot.request_error = None

    def test_polling_bot_uses_proxy_only_when_configured(self) -> None:
        """SOCKS-сессия создаётся только для явно настроенного Telegram-прокси."""
        proxy_session = FakeTelegramSession()
        with patch(
            "aiogram.client.session.aiohttp.AiohttpSession",
            return_value=proxy_session,
        ) as session_factory:
            direct_bot = _create_polling_bot(FakeAiogram(), token="token")
            proxied_bot = _create_polling_bot(
                FakeAiogram(),
                token="token",
                proxy_url="socks5://10.77.77.1:1080",
            )

        self.assertIsNot(direct_bot.session, proxy_session)
        self.assertIs(proxied_bot.session, proxy_session)
        session_factory.assert_called_once_with(proxy="socks5://10.77.77.1:1080")

    def test_polling_bot_interrupts_retry_for_conflict(self) -> None:
        """Конфликт polling выходит из внутреннего бесконечного retry aiogram."""
        conflict = AiogramTelegramConflictError(
            GetMe(),
            "terminated by other getUpdates request",
        )
        FakePollingBot.request_error = conflict
        bot = _create_polling_bot(FakeAiogram(), token="token")

        async def run() -> None:
            with self.assertRaises(_FatalPollingError) as raised:
                await bot(object())
            self.assertIs(raised.exception.error, conflict)

        try:
            asyncio.run(run())
        finally:
            FakePollingBot.request_error = None

    def test_polling_bot_interrupts_retry_for_bad_token(self) -> None:
        """Неверный токен не маскируется бесконечными повторными попытками."""
        unauthorized = AiogramTelegramUnauthorizedError(GetMe(), "Unauthorized")
        FakePollingBot.request_error = unauthorized
        bot = _create_polling_bot(FakeAiogram(), token="token")

        async def run() -> None:
            with self.assertRaises(_FatalPollingError) as raised:
                await bot(object())
            self.assertIs(raised.exception.error, unauthorized)

        try:
            asyncio.run(run())
        finally:
            FakePollingBot.request_error = None

    def test_run_polling_closes_session_once_after_graceful_stop(self) -> None:
        """Штатная остановка закрывает session ровно один раз."""
        aiogram = FakeAiogram()
        logger = logging.getLogger("test.telegram.polling_stop")

        async def run() -> None:
            await _run_telegram_bot(
                token="token",
                incoming_message_processor=lambda _message, _handler: None,  # type: ignore[return-value]
                logger=logger,
            )

        with patch(
            "compadre_assistant.presentation.telegram.bot._load_aiogram",
            return_value=aiogram,
        ):
            asyncio.run(run())

        self.assertIsNotNone(aiogram.dispatcher.start_arguments)
        bot, kwargs = aiogram.dispatcher.start_arguments
        self.assertFalse(kwargs["close_bot_session"])
        self.assertEqual(bot.session.close_calls, 1)

    def test_run_polling_reports_fatal_api_error(self) -> None:
        """Неустранимая ошибка Telegram API превращается в adapter-ошибку."""
        aiogram = FakeAiogram()
        logger = logging.getLogger("test.telegram.polling_fatal")

        async def raise_conflict(
            bot: FakePollingBot,
            **_kwargs: object,
        ) -> None:
            FakePollingBot.request_error = AiogramTelegramConflictError(
                GetMe(),
                "conflict",
            )
            try:
                await bot(object())
            finally:
                FakePollingBot.request_error = None

        aiogram.dispatcher.start_polling = raise_conflict  # type: ignore[method-assign]

        async def run() -> None:
            with self.assertRaisesRegex(TelegramBotError, "TelegramConflictError"):
                await _run_telegram_bot(
                    token="token",
                    incoming_message_processor=lambda _message, _handler: None,  # type: ignore[return-value]
                    logger=logger,
                )

        with patch(
            "compadre_assistant.presentation.telegram.bot._load_aiogram",
            return_value=aiogram,
        ):
            asyncio.run(run())

    def test_start_polling_retries_many_temporary_errors_then_succeeds(self) -> None:
        """Временные сбои до цикла обновлений не завершают adapter."""
        aiogram = FakeAiogram()
        bot = FakePollingBot(token="token")
        logger = logging.getLogger("test.telegram.polling_startup_retry")
        calls = 0
        delays: list[float] = []

        async def start_with_one_network_failure(
            _bot: FakePollingBot,
            **_kwargs: object,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls <= 8:
                raise AiogramTelegramNetworkError(GetMe(), "temporary failure")

        async def sleeper(delay: float) -> None:
            delays.append(delay)

        aiogram.dispatcher.start_polling = start_with_one_network_failure  # type: ignore[method-assign]

        async def run() -> None:
            await _start_polling_with_retries(
                aiogram.dispatcher,
                bot,
                aiogram=aiogram,
                logger=logger,
                sleeper=sleeper,
            )

        with self.assertLogs(logger, level="WARNING") as logs:
            asyncio.run(run())

        self.assertEqual(calls, 9)
        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0])
        self.assertIn("error_type=TelegramNetworkError", logs.output[0])

    def test_polling_retry_delay_caps_without_unbounded_exponent(self) -> None:
        """Большой номер сбоя не увеличивает задержку выше заданного потолка."""
        self.assertEqual(_telegram_polling_retry_delay(7), 30.0)
        self.assertEqual(_telegram_polling_retry_delay(10_000), 30.0)

    def test_completion_callback_replies_in_telegram_loop(self) -> None:
        """Фоновый результат безопасно возвращается в Telegram event loop."""
        message = TelegramMessage()
        logger = logging.getLogger("test.telegram.github_completion")

        async def run() -> None:
            handler = _create_telegram_completion_handler(
                message,
                loop=asyncio.get_running_loop(),
                logger=logger,
            )
            await asyncio.to_thread(
                handler,
                BackgroundReply(ReplyAddress("telegram", "100"), IncomingResponse(
                    text_content("https://github.com/apps/compadre-assistant/installations/new "
                    "Contents: read and write")
                )),
            )
            for _attempt in range(10):
                if message.answers:
                    break
                await asyncio.sleep(0)

        asyncio.run(run())

        self.assertEqual(len(message.answers), 1)
        self.assertIn("installations/new", message.answers[0])
        self.assertIn("read and write", message.answers[0])


if __name__ == "__main__":
    unittest.main()
