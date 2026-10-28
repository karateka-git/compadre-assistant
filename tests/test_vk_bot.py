from compadre_assistant.core.presentation.message_content import text_content
"""Тесты VK presentation adapter."""

import logging
from threading import Event, Thread
import unittest
from unittest.mock import patch
from urllib.error import URLError

from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    BackgroundReply, BoundReplyAction, CommandMenu, CommandMenuUpdate, CommandSuggestion, IncomingResponse,
)
from compadre_assistant.core.presentation.incoming.commands import ChatCommand
from compadre_assistant.presentation.vk.bot import (
    LongPollServer,
    VkApiClient,
    VkBotError,
    VkTransientError,
    _VkUpdateWorkers,
    _create_vk_menu_update_handler,
    _handle_update,
    _long_poll_retry_delay,
    _safe_send_vk_message,
    _serialize_vk_inline_keyboard,
    run_vk_bot,
)


class FakeVkClient:
    """Fake VK client для проверки adapter без реального VK API."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.random_ids: list[int | None] = []
        self.formats: list[str | None] = []
        self.keyboards: list[str | None] = []
        self.attempt_keyboards: list[str | None] = []

    def send_message(
        self,
        *,
        peer_id: int,
        text: str,
        random_id: int | None = None,
        format_data: str | None = None,
        keyboard: str | None = None,
    ) -> None:
        """Запоминает отправленное сообщение."""
        self.random_ids.append(random_id)
        self.attempt_keyboards.append(keyboard)
        self.messages.append((peer_id, text))
        self.formats.append(format_data)
        self.keyboards.append(keyboard)


class FailingVkClient(FakeVkClient):
    """Fake VK client, который имитирует ошибку отправки."""

    def send_message(
        self,
        *,
        peer_id: int,
        text: str,
        random_id: int | None = None,
        format_data: str | None = None,
        keyboard: str | None = None,
    ) -> None:
        """Имитирует ошибку VK API при отправке сообщения."""
        self.random_ids.append(random_id)
        raise VkBotError("send failed")


class FlakyVkClient(FakeVkClient):
    """Дважды имитирует временный сетевой сбой перед успешной отправкой."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def send_message(
        self,
        *,
        peer_id: int,
        text: str,
        random_id: int | None = None,
        format_data: str | None = None,
        keyboard: str | None = None,
    ) -> None:
        """Сохраняет `random_id` каждой попытки и завершается с третьей."""
        self.attempts += 1
        self.random_ids.append(random_id)
        self.attempt_keyboards.append(keyboard)
        if self.attempts < 3:
            raise VkTransientError("temporary failure")
        self.messages.append((peer_id, text))
        self.formats.append(format_data)


class FakeHttpResponse:
    """Минимальный context manager HTTP-ответа для тестов VK API."""

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        """Возвращает успешный JSON-ответ VK API."""
        return b'{"response": 1}'


class LongPollSequenceClient:
    """Имитирует заданную последовательность вызовов VK Long Poll."""

    def __init__(self, *, servers: list[object], waits: list[object]) -> None:
        self._servers = list(servers)
        self._waits = list(waits)
        self.server_calls = 0
        self.wait_calls = 0

    def get_long_poll_server(self, *, group_id: int) -> LongPollServer:
        """Возвращает следующий результат получения Long Poll server."""
        del group_id
        self.server_calls += 1
        return self._next(self._servers)

    def wait_long_poll(self, _long_poll: LongPollServer) -> dict[str, object]:
        """Возвращает следующий результат ожидания Long Poll."""
        self.wait_calls += 1
        return self._next(self._waits)

    @staticmethod
    def _next(results: list[object]) -> object:
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class LongPollMessageClient(LongPollSequenceClient, FakeVkClient):
    """Имитирует Long Poll и сохраняет ответы без обращения к VK."""

    def __init__(self, *, waits: list[object]) -> None:
        LongPollSequenceClient.__init__(
            self,
            servers=[LongPollServer(server="https://long-poll.example", key="key", ts="1")],
            waits=waits,
        )
        FakeVkClient.__init__(self)


class VkBotTest(unittest.TestCase):
    """Проверяет helpers VK adapter."""

    def test_vk_inline_keyboard_serializes_bound_actions_and_opaque_token(self) -> None:
        """Inline-клавиатура содержит только callback с token из контракта."""
        response = IncomingResponse(text_content("Текст /confirm core identity 1 2 yes"), bound_actions=(
            BoundReplyAction("Подтвердить", "opaque-1", "response-1"),
            BoundReplyAction("Отклонить", "opaque-2", "response-1"),
        ))
        keyboard = __import__("json").loads(_serialize_vk_inline_keyboard(response))
        self.assertTrue(keyboard["inline"])
        self.assertFalse(keyboard["one_time"])
        actions = [button["action"] for row in keyboard["buttons"] for button in row]
        self.assertEqual([action["type"] for action in actions], ["callback", "callback"])
        self.assertEqual([__import__("json").loads(action["payload"]) for action in actions],
                         [{"token": "opaque-1"}, {"token": "opaque-2"}])

    def test_vk_inline_keyboard_enforces_verified_limits_and_response_binding(self) -> None:
        """Сериализатор проверяет лимит 30, подпись, payload и единый response ID."""
        def response(actions):
            return IncomingResponse(text_content("text"), bound_actions=tuple(actions))

        too_many = [BoundReplyAction("a", f"t{i}", "r") for i in range(31)]
        with self.assertRaisesRegex(ValueError, "at most 30"):
            _serialize_vk_inline_keyboard(response(too_many))
        with self.assertRaisesRegex(ValueError, "1-40"):
            _serialize_vk_inline_keyboard(response([BoundReplyAction("x" * 41, "t", "r")]))
        with self.assertRaisesRegex(ValueError, "255 bytes"):
            _serialize_vk_inline_keyboard(response([BoundReplyAction("x", "t" * 260, "r")]))
        with self.assertRaisesRegex(ValueError, "one response"):
            _serialize_vk_inline_keyboard(response([
                BoundReplyAction("a", "t1", "r1"), BoundReplyAction("b", "t2", "r2"),
            ]))

    def test_vk_inline_keyboard_is_attached_only_to_last_chunk_and_preserves_text(self) -> None:
        """Клавиатура приходит только с последней частью без изменения текста."""
        client = FakeVkClient()
        response = IncomingResponse(
            text_content("abcdefghij"),
            bound_actions=(BoundReplyAction("Да", "token", "response"),),
        )
        _safe_send_vk_message(client, peer_id=22, response=response,
                              logger=logging.getLogger("test.vk.inline.chunks"), limit=4,
                              include_bound_actions=True)
        self.assertEqual("".join(text for _, text in client.messages), "abcdefghij")
        self.assertEqual(client.keyboards[:-1], [None, None])
        self.assertIsNotNone(client.keyboards[-1])

    def test_vk_inline_retry_reuses_keyboard_and_random_id(self) -> None:
        """Повтор последней части сохраняет клавиатуру и random_id."""
        client = FlakyVkClient()
        response = IncomingResponse(text_content("reply"), bound_actions=(
            BoundReplyAction("Далее", "opaque", "response"),
        ))
        _safe_send_vk_message(client, peer_id=22, response=response,
                              logger=logging.getLogger("test.vk.inline.retry"),
                              sleeper=lambda _delay: None, include_bound_actions=True)
        self.assertEqual(len(set(client.random_ids)), 1)
        self.assertEqual(len(set(client.attempt_keyboards)), 1)
        self.assertIsNotNone(client.attempt_keyboards[0])

    def test_vk_inline_runtime_gate_and_background_reply_stay_disabled(self) -> None:
        """Runtime gate скрывает кнопки и в обычном, и в фоновом ответе."""
        client = FakeVkClient()
        response = IncomingResponse(text_content("reply"), bound_actions=(
            BoundReplyAction("Далее", "opaque", "response"),
        ))
        _safe_send_vk_message(client, peer_id=22, response=response,
                              logger=logging.getLogger("test.vk.inline.gate"))
        callback = __import__("compadre_assistant.presentation.vk.bot", fromlist=["_create_vk_completion_handler"])
        callback._create_vk_completion_handler(client, logger=logging.getLogger("test.vk.inline.background"))(
            BackgroundReply(ReplyAddress("vk", "22"), response)
        )
        self.assertEqual(client.keyboards, [None, None])

    def test_handle_update_converts_vk_message_to_incoming_message(self) -> None:
        """VK update превращается в channel-agnostic IncomingEvent."""
        client = FakeVkClient()
        processed: list[IncomingEvent] = []

        def processor(
            message: IncomingEvent,
            _completion_handler,
        ) -> IncomingResponse:
            processed.append(message)
            return IncomingResponse(text_content("Пришли ссылку на статью."))

        _handle_update(
            {
                "type": "message_new",
                "object": {
                    "message": {
                        "id": 11,
                        "peer_id": 22,
                        "from_id": 33,
                        "text": "hello",
                    }
                },
            },
            incoming_message_processor=processor,
            client=client,
            logger=logging.getLogger("test"),
        )

        self.assertEqual(processed[0].channel, "vk")
        self.assertEqual(processed[0].chat_id, "22")
        self.assertEqual(processed[0].message_id, "11")
        self.assertEqual(processed[0].external_user_id, "33")
        self.assertEqual(client.messages[0][0], 22)
        self.assertIn("Пришли ссылку", client.messages[0][1])

    def test_handle_update_passes_menu_callback_and_keeps_plain_reply(self) -> None:
        """VK передаёт callback меню в processor и отдельно отправляет его ответ."""
        client = FakeVkClient()
        received_handlers = []

        def processor(_message, _completion_handler, menu_handler):
            received_handlers.append(menu_handler)
            menu_handler(CommandMenuUpdate(ReplyAddress("vk", "22"), CommandMenu()))
            return IncomingResponse(text_content("Обычный ответ"))

        _handle_update(
            _update(11, 22, "hello"),
            incoming_message_processor=processor,
            client=client,
            logger=logging.getLogger("test.vk.menu.callback"),
            menu_update_handler=_create_vk_menu_update_handler(
                client, logger=logging.getLogger("test.vk.menu.callback")
            ),
        )

        self.assertIsNotNone(received_handlers[0])
        self.assertEqual(client.messages[0], (22, "Меню команд обновлено."))
        self.assertEqual(client.messages[-1], (22, "Обычный ответ"))

    def test_handle_update_delivers_general_progress_reply(self) -> None:
        """VK отправляет квитанцию по нейтральному сигналу до запуска worker."""
        client = FakeVkClient()
        seen_before_processing: list[str] = []

        def processor(_message: IncomingEvent, _handler) -> IncomingResponse:
            """Запоминает уже отправленные ответы к началу обработки."""
            _handler(BackgroundReply(_message.reply_address, IncomingResponse(text_content("Принял, обрабатываю"))))
            seen_before_processing.extend(text for _, text in client.messages)
            return IncomingResponse(text_content("Готово"))

        _handle_update(
            {
                "type": "message_new",
                "object": {"message": {
                    "id": 11, "peer_id": 22, "from_id": 33,
                    "text": "долгое действие",
                }},
            },
            incoming_message_processor=processor,
            client=client,
            logger=logging.getLogger("test.vk.ack"),
        )

        self.assertEqual(len(seen_before_processing), 1)
        self.assertIn("Принял, обрабатываю", seen_before_processing[0])
        self.assertEqual(client.messages[-1], (22, "Готово"))

    def test_handle_update_does_not_raise_when_vk_send_fails(self) -> None:
        """Ошибка отправки VK-сообщения логируется и не роняет adapter."""
        client = FailingVkClient()
        logger = logging.getLogger("test.vk.safe_send")

        def processor(
            _message: IncomingEvent,
            _completion_handler,
        ) -> IncomingResponse:
            return IncomingResponse(text_content("Пришли ссылку на статью."))

        with self.assertLogs(logger, level="ERROR") as logs:
            _handle_update(
                {
                    "type": "message_new",
                    "object": {
                        "message": {
                            "id": 11,
                            "peer_id": 22,
                            "from_id": 33,
                            "text": "hello",
                        }
                    },
                },
                incoming_message_processor=processor,
                client=client,
                logger=logger,
            )
        self.assertIn("VK message send failed", logs.output[0])
        self.assertEqual(len(client.random_ids), 1)

    def test_safe_send_retries_vk_with_stable_random_id(self) -> None:
        """VK retry использует один idempotency ID и экспоненциальные задержки."""
        client = FlakyVkClient()
        logger = logging.getLogger("test.vk.retry")
        delays: list[float] = []

        with self.assertLogs(logger, level="WARNING") as logs:
            _safe_send_vk_message(
                client,
                peer_id=22,
                response=IncomingResponse(text_content("hello")),
                logger=logger,
                sleeper=delays.append,
            )

        self.assertEqual(client.messages, [(22, "hello")])
        self.assertEqual(client.attempts, 3)
        self.assertEqual(delays, [0.5, 1.0])
        self.assertEqual(len(set(client.random_ids)), 1)
        self.assertIsNotNone(client.random_ids[0])
        self.assertIn("failed_attempt=2/3", logs.output[-1])

    def test_handle_update_completion_callback_replies_to_same_peer(self) -> None:
        """Фоновый результат регистрации отправляется в исходный VK peer."""
        client = FakeVkClient()
        completion_handlers = []

        def processor(_message, completion_handler):
            completion_handlers.append(completion_handler)
            return IncomingResponse(text_content("Авторизация GitHub запускается."))

        _handle_update(
            {
                "type": "message_new",
                "object": {
                    "message": {
                        "id": 11,
                        "peer_id": 22,
                        "from_id": 33,
                        "text": "https://github.com/octocat/notes",
                    }
                },
            },
            incoming_message_processor=processor,
            client=client,
            logger=logging.getLogger("test.vk.github_completion"),
        )
        completion_handlers[0](
            BackgroundReply(ReplyAddress("vk", "22"), IncomingResponse(
                text_content("https://github.com/apps/compadre-assistant/installations/new "
                "Contents: read and write")
            ))
        )

        self.assertEqual(client.messages[-1][0], 22)
        self.assertIn("installations/new", client.messages[-1][1])
        self.assertIn("read and write", client.messages[-1][1])

    def test_completion_callbacks_keep_original_peers(self) -> None:
        """Фоновые ответы двух чатов доходят до их исходных VK peer."""
        client = FakeVkClient()
        completion_handlers = []

        def processor(_message, completion_handler):
            completion_handlers.append(completion_handler)
            return IncomingResponse(text_content("Подключение запущено."))

        for message_id, peer_id in ((11, 22), (12, 44)):
            _handle_update(
                {
                    "type": "message_new",
                    "object": {
                        "message": {
                            "id": message_id,
                            "peer_id": peer_id,
                            "from_id": 33,
                            "text": "https://github.com/octocat/notes",
                        }
                    },
                },
                incoming_message_processor=processor,
                client=client,
                logger=logging.getLogger("test.vk.github_completion_routing"),
            )

        completion_handlers[1](BackgroundReply(ReplyAddress("vk", "44"), IncomingResponse(text_content("Второй чат готов."))))
        completion_handlers[0](BackgroundReply(ReplyAddress("vk", "22"), IncomingResponse(text_content("Первый чат готов."))))
        self.assertEqual(
            client.messages[-2:],
            [(44, "Второй чат готов."), (22, "Первый чат готов.")],
        )

    def test_api_call_sends_vk_method_params_in_post_body(self) -> None:
        """VK API methods send params in POST body, not in request URI."""
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHttpResponse()

        client = VkApiClient(token="token")
        with patch("compadre_assistant.presentation.vk.bot.urlopen", fake_urlopen):
            client.send_message(peer_id=22, text="hello")

        request = captured["request"]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.vk.com/method/messages.send")
        self.assertIn(b"message=hello", request.data)
        self.assertIn(b"access_token=token", request.data)

    def test_menu_handler_builds_persistent_text_keyboard_and_uses_exact_commands(self) -> None:
        """Меню содержит текстовые команды, включая явный режим очистки."""
        client = FakeVkClient()
        publish = _create_vk_menu_update_handler(client, logger=logging.getLogger("test.vk.menu"))
        publish(CommandMenuUpdate(ReplyAddress("vk", "22"), CommandMenu((
            CommandSuggestion("/help", "Помощь"), CommandSuggestion("/cancel", "Отмена"),
        ))))

        keyboard = __import__("json").loads(client.keyboards[0])
        self.assertFalse(keyboard["inline"])
        self.assertFalse(keyboard["one_time"])
        self.assertEqual(
            [button["action"] for row in keyboard["buttons"] for button in row],
            [{"type": "text", "label": "/help"}, {"type": "text", "label": "/cancel"}],
        )
        self.assertEqual(client.messages[0], (22, "Меню команд обновлено."))

        publish(CommandMenuUpdate(ReplyAddress("vk", "22"), CommandMenu()))
        self.assertEqual(__import__("json").loads(client.keyboards[1])["buttons"], [])

    def test_menu_handler_preserves_full_main_command_list_with_vk_row_limits(self) -> None:
        """Полный набор основных команд помещается в допустимые строки без потерь."""
        commands = [command.value for command in ChatCommand if not command.arguments_hint]
        client = FakeVkClient()
        publish = _create_vk_menu_update_handler(client, logger=logging.getLogger("test.vk.menu.full"))
        publish(CommandMenuUpdate(ReplyAddress("vk", "22"), CommandMenu(tuple(
            CommandSuggestion(command, command) for command in commands
        ))))
        keyboard = __import__("json").loads(client.keyboards[0])
        self.assertLessEqual(len(keyboard["buttons"]), 10)
        self.assertTrue(all(len(row) <= 5 for row in keyboard["buttons"]))
        self.assertEqual(
            [button["action"]["label"] for row in keyboard["buttons"] for button in row],
            commands,
        )

        oversized = CommandMenu(tuple(
            CommandSuggestion(f"/c{index}", f"Команда {index}") for index in range(51)
        ))
        with self.assertRaisesRegex(ValueError, "at most 50"):
            publish(CommandMenuUpdate(ReplyAddress("vk", "22"), oversized))

    def test_menu_handler_ignores_other_channels_and_non_private_peers(self) -> None:
        """Клавиатура не публикуется в Telegram, сообщества и некорректные адреса."""
        client = FakeVkClient()
        publish = _create_vk_menu_update_handler(client, logger=logging.getLogger("test.vk.menu.scope"))
        menu = CommandMenu((CommandSuggestion("/help", "Помощь"),))
        for address in (ReplyAddress("telegram", "22"), ReplyAddress("vk", "2000000001"),
                        ReplyAddress("vk", "not-an-id")):
            publish(CommandMenuUpdate(address, menu))
        self.assertEqual(client.messages, [])

    def test_menu_handler_keeps_retry_identity_and_propagates_api_error(self) -> None:
        """Повтор одного меню переиспользует random_id, а ошибка доходит до caller."""
        client = FailingVkClient()
        publish = _create_vk_menu_update_handler(client, logger=logging.getLogger("test.vk.menu.retry"))
        update = CommandMenuUpdate(ReplyAddress("vk", "22"), CommandMenu())
        with self.assertRaisesRegex(VkBotError, "send failed"):
            publish(update)
        first_id = client.random_ids[-1]
        with self.assertRaisesRegex(VkBotError, "send failed"):
            publish(update)
        self.assertEqual(client.random_ids[-1], first_id)

    def test_menu_handler_uses_a_new_retry_identity_after_a_menu_transition(self) -> None:
        """Возврат A после B начинает новую серию, а повтор ошибки A сохраняет ID."""
        client = FailingVkClient()
        publish = _create_vk_menu_update_handler(client, logger=logging.getLogger("test.vk.menu.transition"))
        menu_a = CommandMenu((CommandSuggestion("/help", "Помощь"),))
        menu_b = CommandMenu((CommandSuggestion("/status", "Состояние"),))
        for menu in (menu_a, menu_b, menu_a, menu_a):
            with self.assertRaises(VkBotError):
                publish(CommandMenuUpdate(ReplyAddress("vk", "22"), menu))
        first_a, menu_b_id, second_a, retried_a = client.random_ids
        self.assertNotEqual(first_a, menu_b_id)
        self.assertNotEqual(menu_b_id, second_a)
        self.assertEqual(second_a, retried_a)

    def test_vk_runtime_starts_and_stops_menu_reconciliation_without_changing_replies(self) -> None:
        """Runtime запускает сверку и сохраняет штатную доставку текстового ответа."""
        client = LongPollMessageClient(waits=[KeyboardInterrupt()])
        started = Event()
        stopped = Event()

        def processor(_message, _completion_handler):
            return IncomingResponse(text_content("Ответ без изменений"))

        def reconcile(handler, stop):
            started.set()
            handler(CommandMenuUpdate(ReplyAddress("vk", "22"), CommandMenu()))
            stop.wait(2)
            stopped.set()

        processor.run_menu_reconciliation = reconcile
        with self.assertRaises(KeyboardInterrupt):
            run_vk_bot(
                token="token", group_id=1, incoming_message_processor=processor,
                logger=logging.getLogger("test.vk.menu.lifecycle"), client=client,
            )

        self.assertTrue(started.is_set())
        self.assertTrue(stopped.is_set())
        self.assertEqual(client.keyboards, ['{"one_time":false,"inline":false,"buttons":[]}'])
        self.assertTrue(client.messages)

    def test_vk_runtime_logs_menu_reconciliation_thread_that_misses_shutdown(self) -> None:
        """VK runtime сообщает, если поток сверки пережил ограниченное ожидание."""
        class StuckThread:
            def __init__(self, *, target, name, daemon=None):
                self.target = target

            def start(self):
                pass

            def join(self, timeout=None):
                self.timeout = timeout

            def is_alive(self):
                return True

        client = LongPollSequenceClient(
            servers=[LongPollServer(server="https://long-poll.example", key="key", ts="1")],
            waits=[KeyboardInterrupt()],
        )

        def processor(_message, _completion_handler):
            return IncomingResponse(text_content("Ответ"))

        processor.run_menu_reconciliation = lambda _handler, _stop: None
        logger = logging.getLogger("test.vk.menu.shutdown_timeout")
        with patch("compadre_assistant.presentation.vk.bot.Thread", StuckThread):
            with self.assertLogs(logger, level="ERROR") as logs:
                with self.assertRaises(KeyboardInterrupt):
                    run_vk_bot(
                        token="token", group_id=1, incoming_message_processor=processor,
                        logger=logger, client=client, worker_count=1,
                    )
        self.assertIn("VK menu reconciliation did not stop before timeout", logs.output[0])

    def test_vk_send_message_includes_keyboard_without_changing_plain_message_params(self) -> None:
        """VK API получает JSON клавиатуры как параметр обычного messages.send."""
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeHttpResponse()

        client = VkApiClient(token="token")
        with patch("compadre_assistant.presentation.vk.bot.urlopen", fake_urlopen):
            client.send_message(peer_id=22, text="menu", random_id=123, keyboard='{"buttons":[]}')
            client.send_message(peer_id=22, text="plain", random_id=124)

        menu_body = requests[0].data.decode("utf-8")
        plain_body = requests[1].data.decode("utf-8")
        self.assertIn("keyboard=%7B%22buttons%22%3A%5B%5D%7D", menu_body)
        self.assertIn("message=plain", plain_body)
        self.assertNotIn("keyboard=", plain_body)

    def test_api_call_classifies_network_failure_as_transient(self) -> None:
        """Оборванное соединение VK допускает безопасный повтор отправки."""
        client = VkApiClient(token="token")

        with patch(
            "compadre_assistant.presentation.vk.bot.urlopen",
            side_effect=URLError("connection closed"),
        ):
            with self.assertRaises(VkTransientError):
                client.send_message(peer_id=22, text="hello", random_id=123)

    def test_run_vk_bot_retries_initial_server_after_transient_error(self) -> None:
        """Временный сбой при первом получении server не завершает VK-бота."""
        client = LongPollSequenceClient(
            servers=[
                _network_transient_error(),
                VkTransientError("temporary server failure with key=secret"),
                LongPollServer(server="https://long-poll.example", key="key", ts="1"),
            ],
            waits=[KeyboardInterrupt()],
        )
        delays: list[float] = []
        logger = logging.getLogger("test.vk.long_poll.initial")

        with self.assertLogs(logger, level="WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                run_vk_bot(
                    token="token",
                    group_id=1,
                    incoming_message_processor=_unused_processor,
                    logger=logger,
                    client=client,  # type: ignore[arg-type]
                    retry_sleeper=delays.append,
                    retry_jitter=lambda _lower, upper: upper,
                )

        self.assertEqual(client.server_calls, 3)
        self.assertEqual(delays, [0.6, 1.2])
        self.assertIn("stage=initial_server", logs.output[0])
        logged_text = "\n".join(logs.output)
        self.assertIn("error_type=VkTransientError", logged_text)
        self.assertIn("cause_type=URLError", logged_text)
        self.assertIn("reason_type=ConnectionRefusedError", logged_text)
        self.assertIn("errno=111", logged_text)
        self.assertNotIn("secret", logged_text)
        self.assertNotIn("long-poll.example", logged_text)

    def test_long_poll_retry_delay_is_bounded_for_very_large_attempt(self) -> None:
        """Длительный outage не вызывает переполнение при вычислении backoff."""
        delay = _long_poll_retry_delay(
            10**100,
            jitter=lambda _lower, upper: upper,
        )

        self.assertEqual(delay, 30.0)

    def test_run_vk_bot_retries_wait_and_server_refresh_with_reset_delay(self) -> None:
        """Успех ожидания сбрасывает backoff перед повтором обновления server."""
        client = LongPollSequenceClient(
            servers=[
                LongPollServer(server="https://long-poll.example", key="key", ts="1"),
                VkTransientError("temporary refresh failure"),
                LongPollServer(server="https://long-poll.example", key="key-2", ts="2"),
            ],
            waits=[
                VkTransientError("temporary wait failure"),
                {"failed": 2},
                KeyboardInterrupt(),
            ],
        )
        delays: list[float] = []
        logger = logging.getLogger("test.vk.long_poll.refresh")

        with self.assertLogs(logger, level="WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                run_vk_bot(
                    token="token",
                    group_id=1,
                    incoming_message_processor=_unused_processor,
                    logger=logger,
                    client=client,  # type: ignore[arg-type]
                    retry_sleeper=delays.append,
                    retry_jitter=lambda _lower, upper: upper,
                )

        self.assertEqual(client.wait_calls, 3)
        self.assertEqual(client.server_calls, 3)
        self.assertEqual(delays, [0.6, 0.6])
        self.assertIn("stage=wait", logs.output[0])
        self.assertIn("stage=refresh_server", logs.output[1])

    def test_run_vk_bot_propagates_permanent_authorization_error(self) -> None:
        """Постоянная ошибка авторизации завершает VK-бот без повторов."""
        client = LongPollSequenceClient(
            servers=[VkBotError("VK API error 15: access denied")],
            waits=[],
        )
        delays: list[float] = []

        with self.assertRaisesRegex(VkBotError, "error 15"):
            run_vk_bot(
                token="token",
                group_id=1,
                incoming_message_processor=_unused_processor,
                logger=logging.getLogger("test.vk.long_poll.auth"),
                client=client,  # type: ignore[arg-type]
                retry_sleeper=delays.append,
            )

        self.assertEqual(client.server_calls, 1)
        self.assertEqual(delays, [])

    def test_long_poll_keeps_chat_order_without_blocking_another_chat(self) -> None:
        """Задержка A1 не задерживает B1 и не позволяет A2 обогнать A1."""
        a1_started = Event()
        release_a1 = Event()
        b1_finished = Event()
        calls: list[str] = []
        client = LongPollMessageClient(waits=[
            {"ts": "2", "updates": [_update(1, 11, "A1"), _update(2, 22, "B1")]},
            {"ts": "3", "updates": [_update(3, 11, "A2")]},
            KeyboardInterrupt(),
        ])
        stopped = Event()

        def processor(event: IncomingEvent, _handler) -> IncomingResponse:
            calls.append(event.text)
            if event.text == "A1":
                a1_started.set()
                self.assertTrue(release_a1.wait(3))
            if event.text == "B1":
                b1_finished.set()
            return IncomingResponse(text_content(event.text))

        def run() -> None:
            try:
                run_vk_bot(
                    token="token", group_id=1, incoming_message_processor=processor,
                    logger=logging.getLogger("test.vk.workers.order"),
                    client=client, worker_count=2, max_queue_size=2,  # type: ignore[arg-type]
                )
            except KeyboardInterrupt:
                stopped.set()

        runner = Thread(target=run)
        runner.start()
        try:
            self.assertTrue(a1_started.wait(3))
            self.assertTrue(b1_finished.wait(3))
            self.assertFalse(stopped.is_set())
            self.assertEqual(calls.count("A2"), 0)
        finally:
            release_a1.set()
            runner.join(3)

        self.assertFalse(runner.is_alive())
        self.assertTrue(stopped.is_set())
        self.assertEqual([text for text in calls if text.startswith("A")], ["A1", "A2"])
        self.assertEqual([peer_id for peer_id, _ in client.messages], [22, 11, 11])

    def test_worker_queue_blocks_producer_without_dropping_updates(self) -> None:
        """Полная очередь останавливает producer до освобождения места."""
        started = Event()
        release = Event()
        producer_started = Event()
        submitted = Event()
        handled: list[int] = []

        def handle(update: dict[str, object]) -> None:
            number = int(update["number"])
            if number == 1:
                started.set()
                self.assertTrue(release.wait(3))
            handled.append(number)

        workers = _VkUpdateWorkers(
            handler=handle, logger=logging.getLogger("test.vk.workers.queue"),
            worker_count=1, max_queue_size=1,
        )
        workers.submit("chat", {"number": 1})
        self.assertTrue(started.wait(3))
        workers.submit("chat", {"number": 2})

        def submit_third() -> None:
            producer_started.set()
            workers.submit("chat", {"number": 3})
            submitted.set()

        producer = Thread(target=submit_third)
        producer.start()
        try:
            self.assertTrue(producer_started.wait(3))
            self.assertFalse(submitted.wait(0.1))
        finally:
            release.set()
            producer.join(3)
            workers.close()

        self.assertFalse(producer.is_alive())
        self.assertTrue(submitted.is_set())
        self.assertEqual(handled, [1, 2, 3])

    def test_shutdown_drains_queue_and_worker_error_does_not_stop_it(self) -> None:
        """После остановки poll принятые updates завершаются даже после ошибки."""
        client = LongPollMessageClient(waits=[
            {"ts": "2", "updates": [_update(1, 11, "bad"), _update(2, 11, "good")]},
            KeyboardInterrupt(),
        ])

        def processor(event: IncomingEvent, _handler) -> IncomingResponse:
            if event.text == "bad":
                raise RuntimeError("test processor failure")
            return IncomingResponse(text_content(event.text))

        logger = logging.getLogger("test.vk.workers.shutdown")
        with self.assertLogs(logger, level="ERROR") as logs:
            with self.assertRaises(KeyboardInterrupt):
                run_vk_bot(
                    token="token", group_id=1, incoming_message_processor=processor,
                    logger=logger, client=client, worker_count=1, max_queue_size=1,  # type: ignore[arg-type]
                )

        self.assertEqual(client.messages, [(11, "good")])
        self.assertIn("VK update handling failed", "\n".join(logs.output))


def _update(message_id: int, peer_id: int, text: str) -> dict[str, object]:
    """Создаёт сообщение VK для сценариев Long Poll."""
    return {"type": "message_new", "object": {"message": {
        "id": message_id, "peer_id": peer_id, "from_id": peer_id, "text": text,
    }}}


def _unused_processor(
    _message: IncomingEvent,
    _completion_handler,
) -> IncomingResponse:
    """Возвращает результат для сценариев Long Poll без входящих updates."""
    return IncomingResponse(text_content("Пришли ссылку на статью."))


def _network_transient_error() -> VkTransientError:
    """Создаёт временную ошибку с вложенной причиной и секретным текстом."""
    error = VkTransientError("temporary error with token=secret")
    error.__cause__ = URLError(
        ConnectionRefusedError(111, "https://long-poll.example/?key=secret")
    )
    return error


if __name__ == "__main__":
    unittest.main()
