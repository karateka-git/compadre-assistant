"""Проверяет режим администрирования через адаптеры без внешнего API."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import aiogram
import httpx

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_channel_message_processor, create_user_identity_service
from compadre_assistant.capabilities.admin.data.configuration import AdminConfigError
from compadre_assistant.capabilities.admin.domain.cost_report import GetGatewayCostReport
from compadre_assistant.capabilities.ask.data.sqlite.usage_repository import SQLiteUsageRepository
from compadre_assistant.capabilities.ask.domain.usage import TokenUsage, UsageObservation
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.ports import CommandMenuUpdate
from compadre_assistant.presentation.telegram.bot import _create_telegram_menu_update_handler, _register_handlers
from compadre_assistant.presentation.vk.bot import _create_vk_menu_update_handler, _handle_update
from tests.test_message_rendering import _HtmlReader
from tests.test_telegram_bot import FakeAiogram, MenuTelegramBot, TelegramMessage
from tests.test_vk_bot import FakeVkClient


class AdminChannelsTest(unittest.TestCase):
    """Проверяет общий отчёт и независимые режимы связанных чатов."""

    def setUp(self) -> None:
        """Готовит двух пользователей, два канала владельца и историю учёта."""
        directory = self.enterContext(TemporaryDirectory(prefix="admin-channels-"))
        self.path = Path(directory) / "app.db"
        self.logger = logging.getLogger("test.admin.channels")
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            owner = service.register_with_name(IncomingIdentity("telegram", "101", "101"), display_name="Владелец")
            code = service.create_link_code(owner.id)
            service.link(code=code.code, identity=IncomingIdentity("vk", "202", "202"))
            service.register_with_name(IncomingIdentity("telegram", "303", "303"), display_name="Другой")
        self.owner_id = owner.id
        self.enterContext(patch.dict("os.environ", {"ADMIN_TIMEWEB_API_TOKEN": "", "ADMIN_TIMEWEB_KEY_ID": ""}))

    def configure(self, value: str) -> None:
        """Подменяет операторское значение до сборки очередного процесса."""
        self.enterContext(patch.dict("os.environ", {"ADMIN_TRUSTED_APP_USER_IDS": value}))

    def configure_timeweb(self, respond, *, currency: str = "RUB") -> httpx.Client:
        """Подключает fake HTTP через тот же путь, что и штатный composition root."""
        self.enterContext(patch.dict("os.environ", {
            "ADMIN_TIMEWEB_API_TOKEN": "account-secret",
            "ADMIN_TIMEWEB_KEY_ID": "550e8400-e29b-41d4-a716-446655440000",
            "ADMIN_TIMEWEB_CURRENCY": currency,
        }))
        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.enterContext(patch("compadre_assistant.capabilities.admin.bootstrap.httpx.Client", return_value=client))
        return client

    def telegram(self, processor: ChannelMessageProcessor, text: str, *, user: int | None = 101,
                 chat: int = 101) -> tuple[str, object | None]:
        """Передаёт событие Telegram и возвращает текст и клавиатуру ответа."""
        aiogram = FakeAiogram()
        _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)
        message = TelegramMessage()
        message.chat = SimpleNamespace(id=chat)
        message.from_user = SimpleNamespace(id=user, username="owner") if user is not None else None
        message.message_id = 1
        message.text = text
        asyncio.run(aiogram.dispatcher.included_router.handler(message))
        return "\n".join("".join(_HtmlReader(answer).parts) for answer in message.answers), message.reply_markups[-1]

    def vk(self, processor: ChannelMessageProcessor, text: str, *, user: int = 202,
           peer: int = 202) -> FakeVkClient:
        """Передаёт событие VK через настоящий парсер Long Poll."""
        client = FakeVkClient()
        _handle_update({"type": "message_new", "object": {"message": {
            "peer_id": peer, "from_id": user, "id": 1, "text": text,
        }}}, incoming_message_processor=processor, client=client, logger=self.logger)
        return client

    def select_vk(self, processor: ChannelMessageProcessor, client: FakeVkClient, payload: str) -> str:
        """Выбирает кнопку из серверного сообщения VK."""
        _handle_update({"type": "message_event", "object": {
            "peer_id": 202, "user_id": 202, "event_id": "cost-button", "payload": payload,
        }}, incoming_message_processor=processor, client=client, logger=self.logger)
        return client.messages[-1][1]

    def test_report_uses_timeweb_in_both_channels_without_ask_runtime(self) -> None:
        """Отчёт берёт цены и число запросов из API, сохраняя историю SQLite."""
        self.configure(str(self.owner_id))
        moment = datetime(2026, 11, 14, 17, tzinfo=UTC)
        tokens = TokenUsage(prompt_tokens=100_000_000, completion_tokens=0)
        observation = UsageObservation(uuid4(), moment, "test", "model", tokens)
        self.assertTrue(SQLiteUsageRepository(self.path).store(observation))
        end = datetime(2026, 11, 14, 20, tzinfo=UTC)
        requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _statistics_response(request, ["0.0025", "0.0025", "null"], counts=[2, 2, 5])

        client = self.configure_timeweb(respond)
        with patch("compadre_assistant.capabilities.ask.bootstrap.load_ask_config") as load_ask, \
             patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model, \
             patch("compadre_assistant.capabilities.admin.domain.cost_report._utc_now",
                   return_value=end), \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.assertIn("войди в режим через /admin", self.telegram(processor, "/llm_costs")[0])
            self.assertIn("без аргументов", self.telegram(processor, "/admin /llm_costs")[0])
            self.assertIn("войди в режим через /admin", self.telegram(processor, "/llm_costs")[0])
            hello, keyboard = self.telegram(processor, "/admin")
            self.assertIn("Режим администрирования включён", hello)
            self.assertIsNotNone(keyboard)
            self.assertIn("/llm_costs <DAY|MONTH|YEAR> <timezone>", self.telegram(processor, "/help")[0])
            self.assertIn("войди в режим через /admin", self.vk(processor, "/llm_costs").messages[-1][1])
            self.assertFalse(requests)
            tg_report = self.telegram(processor, "/llm_costs")[0]
            self.assertIn("Сегодня — 0,01 ₽. Запросов: 4.", tg_report)
            self.assertIn("Текущий месяц — Известная сумма 0,01 ₽. Интервалов без стоимости — 1. Запросов: 9.", tg_report)
            self.assertEqual(tg_report.splitlines()[-1], "Текущий год — Полный период недоступен.")
            self.assertIn("до списания бонусов", tg_report)
            self.assertEqual(len(requests), 4)
            entered = self.vk(processor, "/admin")
            payload = json.loads(entered.keyboards[-1])["buttons"][0][0]["action"]["payload"]
            self.assertEqual(self.select_vk(processor, entered, payload), tg_report)
            self.assertEqual(len(requests), 8)
            moscow = self.vk(processor, "/llm_costs Europe/Moscow").messages[-1][1]
            self.assertIn("Europe/Moscow", moscow)
            self.assertIn("Сегодня — Известная сумма 0,01 ₽. Интервалов без стоимости — 1. Запросов: 9.", moscow)
            self.assertEqual(len(requests), 12)
            today = self.vk(processor, "/llm_costs day Europe/Moscow").messages[-1][1]
            self.assertIn("Запросов: 9.", today)
            self.assertNotIn("Текущий месяц", today)
            self.assertEqual(len(requests), 14)
            self.assertIn("Текущий месяц", self.telegram(processor, "/llm_costs MONTH")[0])
            self.assertEqual(len(requests), 16)
            self.assertIn("Полный период недоступен", self.telegram(processor, "/llm_costs YEAR")[0])
            self.assertEqual(len(requests), 16)
            self.assertIn("Asia/Omsk", self.vk(processor, "/llm_costs").messages[-1][1])
            self.assertIn("Часовой пояс не найден", self.telegram(processor, "/llm_costs /invalid")[0])
            self.assertIn("/llm_costs DAY Europe/Moscow", self.telegram(processor, "/llm_costs x y")[0])
            self.assertEqual(len(requests), 20)
            self.telegram(processor, "/exit")
            self.assertIn("войди в режим через /admin", self.telegram(processor, "/llm_costs")[0])
        load_ask.assert_not_called()
        model.assert_not_called()
        self.assertTrue(client.is_closed)
        for first, second in zip(requests[::2], requests[1::2], strict=True):
            self.assertEqual({first.url.path.rsplit("/", 1)[-1], second.url.path.rsplit("/", 1)[-1]},
                             {"token-usage", "calls"})
            self.assertEqual(first.url.params, second.url.params)
        with connect_database(self.path) as connection:
            saved = connection.execute("SELECT call_id, tokens_json FROM ask_usage").fetchall()
        self.assertEqual([row["call_id"] for row in saved], [str(observation.call_id)])
        self.assertEqual(json.loads(saved[0]["tokens_json"])["prompt_tokens"], tokens.prompt_tokens)

    def test_fractional_timezone_requires_complete_utc_hours_in_both_channels(self) -> None:
        """Проверяет границы DAY и отличает известный ноль от дефекта покрытия."""
        self.configure(str(self.owner_id))
        end = datetime(2026, 11, 14, 20, tzinfo=UTC)
        requests = []
        coverage = "complete"

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            data = _statistics_response(request, []).json()
            is_count = _is_count_request(request)
            point_key = "ai_key_calls_statistics" if is_count else "ai_key_token_statistics"
            points = data[point_key]
            if coverage == "missing_both" or (coverage == "missing_count" and is_count):
                points.pop()
            elif coverage == "duplicate_cost" and not is_count:
                points[-1] = points[0]
            data["meta"]["total"] = len(points)
            return httpx.Response(200, json=data)

        self.configure_timeweb(respond)
        command = "/llm_costs DAY Asia/Kathmandu"
        with patch("compadre_assistant.capabilities.admin.domain.cost_report._utc_now", return_value=end), \
             self.assertLogs("compadre_assistant.capabilities.admin.data.timeweb_statistics", level="WARNING"), \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.telegram(processor, "/admin")
            self.vk(processor, "/admin")
            for coverage, expected in (
                ("complete", "Сегодня — 0,00 ₽. Запросов: 0."),
                ("missing_count", "Сегодня — 0,00 ₽. Число запросов недоступно."),
                ("duplicate_cost", "Сегодня — Статистика Timeweb временно недоступна. Запросов: 0."),
                ("missing_both", "Сегодня — Полный период недоступен."),
            ):
                with self.subTest(coverage=coverage):
                    telegram = self.telegram(processor, command)[0]
                    vk = self.vk(processor, command).messages[-1][1]
                    self.assertIn("Asia/Kathmandu", telegram)
                    self.assertEqual(telegram.splitlines()[-1], expected)
                    self.assertEqual(vk, telegram)
                    self.assertNotIn("Текущий месяц", telegram)

        self.assertEqual(len(requests), 16)
        for request in requests:
            self.assertEqual(request.url.params["start_time"], "2026-11-14T18:15:00Z")
            self.assertEqual(request.url.params["end_time"], "2026-11-14T19:59:59.999999Z")
            self.assertEqual(request.url.params["interval"], "60")

    def test_groups_untrusted_and_unknown_users_never_read_costs(self) -> None:
        """До отчёта допускается только явный доверенный автор личного чата."""
        self.configure(str(self.owner_id))
        tg_group, vk_group = -404, 2_000_000_007
        tg_key = ChatModeKey(self.owner_id, "telegram", str(tg_group))
        vk_key = ChatModeKey(self.owner_id, "vk", str(vk_group))
        private_hint = "Открой личный чат с ботом, чтобы получить отчёт."
        with patch.object(GetGatewayCostReport, "execute", autospec=True) as execute, \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.assertEqual(self.telegram(processor, "/admin", chat=tg_group), (private_hint, None))
            entered = self.vk(processor, "/admin", peer=vk_group)
            self.assertEqual(entered.messages[-1], (vk_group, private_hint))
            self.assertIsNone(entered.keyboards[-1])
            with connect_database(self.path) as connection:
                modes = SQLiteChatModeRepository(connection)
                self.assertIsNone(modes.get(tg_key))
                self.assertIsNone(modes.get(vk_key))
                modes.save(ChatMode(vk_key, "ask"))
            self.assertIn("войди в режим через /admin", self.telegram(processor, "/llm_costs", chat=tg_group)[0])
            self.vk(processor, "/admin /llm_costs", peer=vk_group)
            with connect_database(self.path) as connection:
                modes = SQLiteChatModeRepository(connection)
                self.assertEqual(modes.get(vk_key).capability_id, "ask")
                # Проверяем защиту при состоянии, сохранённом старой сборкой.
                modes.save(ChatMode(tg_key, "admin"))
                modes.save(ChatMode(vk_key, "admin"))
            self.assertEqual(self.telegram(processor, "/llm_costs Invalid", chat=tg_group), (private_hint, None))
            reply = self.vk(processor, "/llm_costs Europe/Moscow", peer=vk_group)
            self.assertEqual(reply.messages[-1], (vk_group, private_hint))
            with connect_database(self.path) as connection:
                modes = SQLiteChatModeRepository(connection)
                self.assertIsNone(modes.get(tg_key))
                self.assertIsNone(modes.get(vk_key))
            self.assertIn("нет доступа", self.telegram(processor, "/llm_costs", user=303, chat=303)[0])
            self.assertIn("нет доступа", self.telegram(processor, "/llm_costs", user=None)[0])
            self.assertIn("/register", self.vk(processor, "/admin", user=405, peer=405).messages[-1][1])
            self.assertNotIn("/admin", self.telegram(processor, "/help", user=303, chat=303)[0])
            self.assertNotIn("/admin", self.telegram(processor, "/help", user=None)[0])
            execute.assert_not_called()

    def test_restart_revokes_existing_button_and_channel_menus(self) -> None:
        """Новая конфигурация блокирует сохранённый режим сразу после перезапуска."""
        self.configure(str(self.owner_id))
        with create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.telegram(processor, "/admin")
            vk_client = self.vk(processor, "/admin")
            payload = json.loads(vk_client.keyboards[-1])["buttons"][0][0]["action"]["payload"]
            self.assert_channel_menus(processor, include_costs=True)
            # Активность в группе не должна скрывать ранее сохранённый личный чат
            # от фоновой сверки меню нового процесса.
            self.telegram(processor, "/admin", chat=-404)
            self.vk(processor, "/admin", peer=2_000_000_007)

        self.configure("")
        with patch.object(GetGatewayCostReport, "execute", autospec=True) as execute, \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as restarted:
            self.assert_channel_menus(restarted, include_costs=False)
            self.assertIn("больше недоступна", self.select_vk(restarted, vk_client, payload))
            self.assertIn("нет доступа", self.telegram(restarted, "/llm_costs")[0])
            self.assertIn("нет доступа", self.vk(restarted, "/llm_costs").messages[-1][1])
            execute.assert_not_called()

    def test_disabled_and_invalid_admin_cannot_expose_report(self) -> None:
        """Отключённая сборка игнорирует профиль, а ошибочная настройка блокирует сборку."""
        self.configure("invalid")
        with patch("compadre_assistant.capabilities.admin.bootstrap.load_admin_timeweb_config") as reader:
            with create_channel_message_processor(database_path=self.path, enabled_capabilities=()) as disabled:
                self.assertNotIn("/admin", self.telegram(disabled, "/help")[0])
                self.assertIn("нет доступа", self.telegram(disabled, "/admin")[0])
            with self.assertRaises(AdminConfigError), create_channel_message_processor(
                    database_path=self.path, enabled_capabilities=("admin",)):
                self.fail("Сборка приняла ошибочную настройку")
        reader.assert_not_called()

    def test_large_totals_and_backend_failure_preserve_private_mode(self) -> None:
        """Ошибки чтения дают короткий ответ, а большие суммы округляются точно."""
        self.configure(str(self.owner_id))
        cost_requests = []
        count_requests = []
        whole = "9" * 39

        def respond(request: httpx.Request) -> httpx.Response:
            is_count = _is_count_request(request)
            requests = count_requests if is_count else cost_requests
            failure_index = 2 if is_count else 1
            requests.append(request)
            if len(requests) == failure_index:
                return httpx.Response(503, text="detail-not-for-chat")
            return _statistics_response(request, [f"{whole}.995"], counts=[4])

        self.configure_timeweb(respond, currency="EUR")
        with patch("compadre_assistant.capabilities.admin.domain.cost_report._utc_now",
                   return_value=datetime(2026, 2, 14, 20, tzinfo=UTC)), \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.telegram(processor, "/admin")
            with self.assertLogs("compadre_assistant.capabilities.admin.data.timeweb_statistics", level="WARNING") as diagnostics:
                failed = self.telegram(processor, "/llm_costs")[0]
            self.assertIn("Сегодня — Статистика Timeweb временно недоступна. Запросов: 4.", failed)
            self.assertNotIn("detail-not-for-chat", failed + str(diagnostics.output))
            rounded = "1" + "0" * 39
            self.assertIn(f"Текущий месяц — {rounded},00 EUR. Число запросов недоступно.", failed)
            self.assertIn(f"Текущий год — {rounded},00 EUR. Запросов: 4.", failed)
            self.assertIn(f"Сегодня — {rounded},00 EUR. Запросов: 4.", self.telegram(processor, "/llm_costs DAY")[0])

    def test_unconfigured_report_keeps_mode_in_both_channels(self) -> None:
        """Без токена и UUID режим остаётся доступен без запроса к API."""
        self.configure(str(self.owner_id))
        with patch("compadre_assistant.capabilities.admin.bootstrap.httpx.Client") as client, \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.telegram(processor, "/admin")
            self.vk(processor, "/admin")
            self.assertEqual(self.telegram(processor, "/llm_costs DAY")[0], "Статистика Timeweb не настроена.")
            self.assertEqual(self.vk(processor, "/llm_costs").messages[-1][1], "Статистика Timeweb не настроена.")
            client.assert_not_called()

    def assert_channel_menus(self, processor: ChannelMessageProcessor, *, include_costs: bool) -> None:
        """Публикует меню через настоящие обработчики двух каналов."""
        vk_client = FakeVkClient()
        vk_handler = _create_vk_menu_update_handler(vk_client, logger=self.logger)

        async def check_telegram() -> None:
            bot = MenuTelegramBot()
            handler = _create_telegram_menu_update_handler(bot, loop=asyncio.get_running_loop(),
                                                          aiogram=aiogram, logger=self.logger)

            def publish_both(update: CommandMenuUpdate) -> None:
                """Передаёт каждый снимок обоим транспортным обработчикам."""
                handler(update)
                vk_handler(update)

            await asyncio.to_thread(processor.reconcile_menus, publish_both)
            owner_commands = next(commands for commands, scope in bot.menu_calls if scope.chat_id == 101)
            commands = {command.command for command in owner_commands}
            self.assertEqual("llm_costs" in commands, include_costs)

        asyncio.run(check_telegram())
        commands = {button["action"]["label"] for row in json.loads(vk_client.keyboards[-1])["buttons"] for button in row}
        self.assertEqual("/llm_costs" in commands, include_costs)
        self.assertFalse(processor.menu_delivery_failures)


def _is_count_request(request: httpx.Request) -> bool:
    return request.url.path.endswith("/statistic/calls")


def _statistics_response(request: httpx.Request, amounts: list[str], *, counts: list[int] | None = None) -> httpx.Response:
    is_count = _is_count_request(request)
    points_key = "ai_key_calls_statistics" if is_count else "ai_key_token_statistics"
    field = "call_count" if is_count else "total_cost"
    values = [str(count) for count in counts or ()] if is_count else amounts
    start_hour = datetime.fromisoformat(request.url.params["start_time"]).replace(minute=0, second=0, microsecond=0)
    end_hour = datetime.fromisoformat(request.url.params["end_time"]).replace(minute=0, second=0, microsecond=0)
    step = timedelta(minutes=int(request.url.params["interval"]))
    points = []
    while start_hour <= end_hour:
        index = len(points)
        value = values[index] if index < len(values) else "0"
        points.append(f'{{"time":"{start_hour.isoformat()}","{field}":{value}}}')
        start_hour += step
    return httpx.Response(200, text=(
        f'{{"{points_key}":[{",".join(points)}],"meta":{{"total":{len(points)}}}}}'
    ))
