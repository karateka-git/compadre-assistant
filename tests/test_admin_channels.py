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

import aiogram
import httpx

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_channel_message_processor, create_user_identity_service
from compadre_assistant.capabilities.admin.data.configuration import AdminConfigError
from compadre_assistant.capabilities.admin.domain.cost_report import (
    CostReportRow, CostReportStatus, GatewayCostReport, GetGatewayCostReport, ReportPeriod,
)
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.data.sqlite.chat_mode_repository import SQLiteChatModeRepository
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.presentation.telegram.bot import _create_telegram_menu_update_handler, _register_handlers
from compadre_assistant.presentation.vk.bot import _create_vk_menu_update_handler, _handle_update
from tests.test_message_rendering import _HtmlReader
from tests.test_telegram_bot import FakeAiogram, MenuTelegramBot, TelegramMessage
from tests.test_vk_bot import FakeVkClient


class AdminChannelsTest(unittest.TestCase):
    """Проверяет общий отчёт и независимые режимы связанных чатов."""

    def setUp(self) -> None:
        """Готовит двух пользователей и два канала владельца."""
        directory = self.enterContext(TemporaryDirectory(prefix="admin-channels-"))
        self.path = Path(directory) / "app.db"
        self.logger = logging.getLogger("test.admin.channels")
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            owner = service.register_with_name(IncomingIdentity("telegram", "101", "101"), display_name="Владелец")
            code = service.create_link_code(owner.id, target_channel="vk")
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

    def select_telegram(self, processor: ChannelMessageProcessor, keyboard, label: str) -> str:
        """Нажимает кнопку показанного отчёта через callback Telegram."""
        aiogram = FakeAiogram()
        _register_handlers(aiogram.dispatcher, aiogram, incoming_message_processor=processor, logger=self.logger)
        button = next(button for row in keyboard.inline_keyboard for button in row if button.text == label)
        message = TelegramMessage()

        async def acknowledge():
            pass

        async def edit(**kwargs):
            pass

        message.edit_message_reply_markup = edit
        callback = SimpleNamespace(
            answer=acknowledge, id="cost-button", data=button.callback_data,
            from_user=SimpleNamespace(id=101), bot=message,
            message=SimpleNamespace(chat=SimpleNamespace(id=101), message_id=1),
        )
        asyncio.run(aiogram.dispatcher.included_router.callback_handler(callback))
        return "".join(_HtmlReader(message.answers[-1]).parts)

    def vk(
        self, processor: ChannelMessageProcessor, text: str, *, user: int = 202, peer: int = 202,
        client: FakeVkClient | None = None, publish=None, payload: str | None = None,
    ) -> FakeVkClient:
        """Передаёт ввод или нижнюю кнопку VK через настоящий парсер Long Poll."""
        if client is None:
            client = FakeVkClient()
        _handle_update({"type": "message_new", "object": {"message": {
            "peer_id": peer, "from_id": user, "id": 1, "text": text, "payload": payload,
        }}}, incoming_message_processor=processor, client=client, logger=self.logger, menu_update_handler=publish)
        return client

    def select_vk(
        self, processor: ChannelMessageProcessor, client: FakeVkClient, payload: str, *, publish=None,
    ) -> str:
        """Выбирает кнопку из серверного сообщения VK."""
        _handle_update({"type": "message_event", "object": {
            "peer_id": 202, "user_id": 202, "event_id": "cost-button", "payload": payload,
            "conversation_message_id": 1,
        }}, incoming_message_processor=processor, client=client, logger=self.logger, menu_update_handler=publish)
        return client.messages[-1][1]

    def test_report_uses_timeweb_in_both_channels_without_ask_runtime(self) -> None:
        """Отчёт берёт цены и число запросов из API, сохраняя историю SQLite."""
        self.configure(str(self.owner_id))
        started_at_us = int(datetime(2026, 11, 14, 17, tzinfo=UTC).timestamp()) * 1_000_000
        historical = ("00000000-0000-0000-0000-000000000001", started_at_us, "test", "model",
                      '{"prompt_tokens": 100000000, "completion_tokens": 0}')
        with connect_database(self.path) as connection, connection:
            connection.execute("INSERT INTO ask_usage VALUES (?, ?, ?, ?, ?)", historical)
        end = datetime(2026, 11, 14, 20, tzinfo=UTC)
        requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _statistics_response(request, ["0.0025", "0.0025", "null"], counts=[2, 2, 5])

        client = self.configure_timeweb(respond)
        with patch("compadre_assistant.capabilities.ai.bootstrap.load_ai_config") as load_ask, \
             patch("compadre_assistant.capabilities.ai.bootstrap.OpenAIModel") as model, \
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
            self.assertIn("Текущий год — Полный период недоступен.", tg_report.splitlines())
            self.assertIn("до списания бонусов", tg_report)
            self.assertEqual(len(requests), 4)
            vk = FakeVkClient()
            publish = _create_vk_menu_update_handler(vk, logger=self.logger)
            self.vk(processor, "/admin", client=vk, publish=publish)
            self.assertEqual(len(vk.messages), 1)
            summary_button = _vk_buttons(vk.keyboards[-1], inline=False)["Показать расходы ИИ"]
            self.vk(processor, "Показать расходы ИИ", client=vk, publish=publish,
                    payload=summary_button["payload"])
            self.assertEqual(len(vk.messages), 2)
            self.assertEqual(vk.messages[-1][1], tg_report)
            self.assertEqual(len(requests), 8)

            self.vk(processor, "/llm_costs Europe/Moscow", client=vk, publish=publish)
            moscow = vk.messages[-1][1]
            self.assertIn("/llm_costs DAY Europe/Moscow", moscow)
            self.assertIn("Вместо DAY можно указать MONTH или YEAR.", moscow)
            self.assertIn("Сегодня — Известная сумма 0,01 ₽. Интервалов без стоимости — 1. Запросов: 9.", moscow)
            self.assertEqual(len(requests), 12)
            periods = _vk_buttons(vk.keyboards[-1], inline=True)
            self.assertEqual(set(periods), {"Сегодня", "Текущий месяц", "Текущий год"})
            today = self.select_vk(processor, vk, periods["Сегодня"]["payload"], publish=publish)
            self.assertIn("Часовой пояс Europe/Moscow", today)
            self.assertIn("Запросов: 9.", today)
            self.assertNotIn("Текущий месяц —", today)
            self.assertIn("Чтобы открыть сводку, введи /llm_costs Europe/Moscow.", today)
            other_periods = _vk_buttons(vk.keyboards[-1], inline=True)
            self.assertEqual(set(other_periods), {"Текущий месяц", "Текущий год", "Сводка"})
            self.assertEqual(len(requests), 14)
            self.assertEqual(self.select_vk(processor, vk, other_periods["Сводка"]["payload"], publish=publish),
                             moscow)
            self.assertEqual(len(requests), 18)

            month, keyboard = self.telegram(processor, "/llm_costs MONTH")
            self.assertIn("Текущий месяц", month)
            self.assertEqual(len(requests), 20)
            year = self.select_telegram(processor, keyboard, "Текущий год")
            self.assertEqual(year, self.telegram(processor, "/llm_costs YEAR")[0])
            self.assertIn("Полный период недоступен", year)
            self.assertEqual(len(requests), 20)

            self.vk(processor, "/llm_costs Europe/Moscow DAY", client=vk, publish=publish)
            self.assertIn("/llm_costs DAY Europe/Moscow", vk.messages[-1][1])
            self.assertIsNone(vk.keyboards[-1])
            self.assertEqual(len(vk.messages), 6)
            self.assertEqual(len(requests), 20)
            self.assertIn("Asia/Omsk", self.vk(processor, "/llm_costs").messages[-1][1])
            self.assertIn("Часовой пояс не найден", self.telegram(processor, "/llm_costs /invalid")[0])
            self.assertIn("/llm_costs DAY Europe/Moscow", self.telegram(processor, "/llm_costs x y")[0])
            self.assertEqual(len(requests), 24)
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
            saved = connection.execute("SELECT * FROM ask_usage").fetchall()
        self.assertEqual([tuple(row) for row in saved], [historical])

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
                    self.assertIn(expected, telegram.splitlines())
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
            self.assertEqual(self.telegram(processor, "/llm_costs", chat=tg_group)[0], private_hint)
            self.vk(processor, "/admin /llm_costs", peer=vk_group)
            with connect_database(self.path) as connection:
                modes = SQLiteChatModeRepository(connection)
                self.assertEqual(modes.get(vk_key).capability_id, "ask")
                # Проверяем защиту при состоянии, сохранённом старой сборкой.
                modes.save(ChatMode(tg_key, "admin"))
                modes.save(ChatMode(vk_key, "admin"))
                dispatcher = IncomingMessageDispatcher(
                    registry=processor._registry,
                    user_identity_service=create_user_identity_service(connection),
                    chat_mode_repository=modes,
                    capability_preference_repository=SQLiteUserCapabilityPreferenceRepository(connection),
                )
                for event in (IncomingEvent("telegram", str(tg_group), "1", "/help", "101"),
                              IncomingEvent("vk", str(vk_group), "1", "/help", "202")):
                    menu, complete = dispatcher.command_menu_snapshot(event)
                    self.assertTrue(complete)
                    self.assertEqual(tuple(item.command for item in menu.items), ("/settings", "/help"))
            tg_help, tg_keyboard = self.telegram(processor, "/help", chat=tg_group)
            vk_help = self.vk(processor, "/help", peer=vk_group)
            for text in (tg_help, vk_help.messages[-1][1]):
                self.assertIn("/settings", text)
                self.assertNotIn("/admin", text)
                self.assertNotIn("/llm_costs", text)
            self.assertEqual({button.text for row in tg_keyboard.inline_keyboard for button in row}, {"Настройки", "Помощь"})
            self.assertEqual(set(_vk_buttons(vk_help.keyboards[-1], inline=True)), {"Настройки", "Помощь"})
            self.assertEqual(self.telegram(processor, "/llm_costs Invalid", chat=tg_group), (private_hint, None))
            reply = self.vk(processor, "/llm_costs Europe/Moscow", peer=vk_group)
            self.assertEqual(reply.messages[-1], (vk_group, private_hint))
            with connect_database(self.path) as connection:
                modes = SQLiteChatModeRepository(connection)
                self.assertIsNone(modes.get(tg_key))
                self.assertIsNone(modes.get(vk_key))
            self.assertIn("нет доступа", self.telegram(processor, "/llm_costs", user=303, chat=303)[0])
            self.assertIn("нет доступа", self.telegram(processor, "/llm_costs", user=None)[0])
            self.assertIn("пока нет профиля", self.vk(processor, "/admin", user=405, peer=405).messages[-1][1])
            self.assertNotIn("/admin", self.telegram(processor, "/help", user=303, chat=303)[0])
            self.assertNotIn("/admin", self.telegram(processor, "/help", user=None)[0])
            execute.assert_not_called()

    def test_main_menus_and_help_offer_only_available_entries(self) -> None:
        """Главная в обоих каналах показывает доверенному профилю входы."""
        self.configure(str(self.owner_id))
        with create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.assert_channel_menus(processor, {"Администрирование", "Настройки", "Помощь"})
            for text in (self.telegram(processor, "/help")[0], self.vk(processor, "/help").messages[-1][1]):
                self.assertIn("/admin", text)
                self.assertIn("/settings", text)
                for command in ("/exit", "/register", "/name", "/capabilities", "/link", "/llm_costs", "/cancel", "/confirm"):
                    self.assertNotIn(command, text)

    def test_restart_revokes_existing_button_and_channel_menus(self) -> None:
        """Новая конфигурация блокирует сохранённый режим сразу после перезапуска."""
        self.configure(str(self.owner_id))
        cost_report = GatewayCostReport(
            "Asia/Omsk", "RUB", (CostReportRow(ReportPeriod.DAY, CostReportStatus.PERIOD_UNAVAILABLE),),
        )
        with patch.object(GetGatewayCostReport, "execute", return_value=cost_report), \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as processor:
            self.telegram(processor, "/admin")
            vk_client = FakeVkClient()
            publish = _create_vk_menu_update_handler(vk_client, logger=self.logger)
            self.vk(processor, "/admin", client=vk_client, publish=publish)
            lower = _vk_buttons(vk_client.keyboards[-1], inline=False)
            self.vk(processor, "/llm_costs DAY", client=vk_client, publish=publish)
            periods = _vk_buttons(vk_client.keyboards[-1], inline=True)
            payload = periods["Текущий месяц"]["payload"]
            self.assert_channel_menus(processor, {"Показать расходы ИИ", "Выйти из режима"}, vk_client=vk_client)
            # Активность в группе не должна скрывать ранее сохранённый личный чат
            # от фоновой сверки меню нового процесса.
            self.telegram(processor, "/admin", chat=-404)
            self.vk(processor, "/admin", peer=2_000_000_007)

        self.configure("")
        with patch.object(GetGatewayCostReport, "execute", autospec=True) as execute, \
             create_channel_message_processor(database_path=self.path, enabled_capabilities=("admin",)) as restarted:
            self.assert_channel_menus(restarted, {"Настройки", "Помощь"})
            self.assertIn("больше недоступна", self.select_vk(restarted, vk_client, payload))
            rejected = self.vk(restarted, "Показать расходы ИИ", payload=lower["Показать расходы ИИ"]["payload"])
            self.assertIn("кнопка меню устарела", rejected.messages[-1][1])
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

    def assert_channel_menus(
        self, processor: ChannelMessageProcessor, vk_expected: set[str],
        *, vk_client: FakeVkClient | None = None,
    ) -> None:
        """Проверяет меню, доставленные настоящими обработчиками обоих каналов."""
        async def check_telegram() -> None:
            bot = MenuTelegramBot()
            handler = _create_telegram_menu_update_handler(bot, loop=asyncio.get_running_loop(),
                                                          aiogram=aiogram, logger=self.logger)

            await asyncio.to_thread(
                processor.reconcile_menus,
                lambda update: handler(update) if update.address.channel == "telegram" else False,
            )
            owner_commands = next(commands for commands, scope in bot.menu_calls if scope.chat_id == 101)
            commands = {"/" + command.command for command in owner_commands}
            self.assertEqual(commands, {"/help"})

        asyncio.run(check_telegram())
        if vk_client is None:
            vk_client = FakeVkClient()
            self.vk(processor, "/help", client=vk_client,
                    publish=_create_vk_menu_update_handler(vk_client, logger=self.logger))
        keyboard = next(item for item in reversed(vk_client.keyboards) if item and not json.loads(item)["inline"])
        self.assertEqual(set(_vk_buttons(keyboard, inline=False)), vk_expected)
        self.assertFalse(processor.menu_delivery_failures)


def _vk_buttons(keyboard: str, *, inline: bool) -> dict[str, dict]:
    """Возвращает кнопки с проверкой места их публикации."""
    decoded = json.loads(keyboard)
    if decoded["inline"] != inline:
        raise AssertionError("Действия пришли с клавиатурой другого назначения")
    return {button["action"]["label"]: button["action"] for row in decoded["buttons"] for button in row}


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
