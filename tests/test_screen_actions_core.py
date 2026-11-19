"""Проверяет общий контракт через ядро с временной БД и настоящим допуском."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionLayoutLimits, ActionPurpose, ActionScope, ScreenDestination,
)
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityHelp, CapabilityResult, HelpSection, IncomingResponse,
    ReplyAction, ResponseView, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.message_content import text_content
from tests.response_assertions import response_text
from tests.test_incoming_boundary import FakeCapability


class ActionCapability(FakeCapability, SectionActionsQueryPort):
    """Описывает нейтральный экран и раздел, не зная канала доставки."""

    def __init__(self):
        """Объявляет один предметный вход и наблюдения за запросами."""
        super().__init__()
        self._descriptor = replace(self.descriptor, help=CapabilityHelp((HelpSection(
            "Действия", (CapabilityCommand("/choose", (), "выбрать"),),
        ),)))
        self.available = (ReplyAction("Выбрать", "/choose first", scope=ActionScope.SECTION),)
        self.reads = []
        self.read_error = None
        self.on_read = None
        self.layout = None

    def section_actions(self, app_user, address, mode_revision, pending):
        """Возвращает текущие действия, сохраняя прочитанный контекст."""
        self.reads.append((app_user.id, address, mode_revision, pending))
        if self.read_error is not None:
            raise self.read_error
        if self.on_read is not None:
            self.on_read()
        return self.available

    def handle(self, context, event):
        """Сохраняет переданные ядром ограничения и штатную команду."""
        self.layout = context.action_layout
        super().handle(context, event)
        return CapabilityResult(IncomingResponse(text_content("Действия. /choose"),
                                                 suggested_actions=self.available))


class ScreenActionsCoreTest(unittest.TestCase):
    """Проверяет чтение меню, привязку и отказ без сетевых SDK."""

    def setUp(self):
        """Создаёт изолированную актуальную БД и два личных чата."""
        root = Path(self.enterContext(TemporaryDirectory(prefix="screen-actions-core-")))
        self.path = root / "app.db"
        with connect_database(self.path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            for channel in ("telegram", "vk"):
                service.register_with_name(IncomingIdentity(channel, "1001", "1001"), display_name="Тест")
        self.owner = ActionCapability()
        self.processor = ChannelMessageProcessor(database_path=self.path, capabilities=(self.owner,))
        self.event = IncomingEvent("telegram", "1001", "request", "/selected", "1001")
        self.processor(self.event)
        self.processor(replace(self.event, channel="vk"))

    def issue(self, response, *, event=None):
        """Передаёт подготовленный владельцем экран обычной привязке ядра."""
        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            return self.processor(event or self.event)

    def test_query_is_read_only_and_uses_the_same_description_in_both_channels(self):
        """Текст запроса /exit не выполняется, а действия не дублируются."""
        before = (len(self.owner.entries), len(self.owner.calls))
        for channel in ("telegram", "vk"):
            event = replace(self.event, channel=channel, text="/exit")
            self.assertEqual(self.processor.section_actions(event), self.owner.available)
            response = self.processor(replace(event, text="/choose"))
            self.assertEqual(response.suggested_actions, self.owner.available)
            self.assertEqual(response.bound_actions[0].scope, ActionScope.SECTION)
        self.assertEqual(len(self.owner.entries), before[0])
        self.assertEqual(len(self.owner.calls), before[1] + 2)
        self.assertEqual({read[1].channel for read in self.owner.reads}, {"telegram", "vk"})

    def test_empty_snapshot_is_distinct_from_unsupported_and_failure(self):
        """Пустой раздел не возвращает прежний каталог, сбой не маскируется."""
        self.owner.available = ()
        self.assertEqual(self.processor.section_actions(self.event), ())
        self.owner.read_error = RuntimeError("source unavailable")
        with self.assertRaisesRegex(RuntimeError, "source unavailable"):
            self.processor.section_actions(self.event)
        self.owner.read_error = None
        self.processor(replace(self.event, text="/exit"))
        self.assertIsNone(self.processor.section_actions(self.event))

    def test_unknown_foreign_and_revoked_context_do_not_query_owner(self):
        """Нижнее меню не раскрывается чужому автору или недоступному режиму."""
        for event in (replace(self.event, external_user_id="other"),
                      replace(self.event, chat_id="2002", external_user_id="2002")):
            self.assertIsNone(self.processor.section_actions(event))
        self.assertEqual(self.owner.reads, [])
        self.processor(replace(self.event, text="/settings"))
        self.processor(replace(self.event, text="/disable selected"))
        self.owner.reads.clear()
        self.assertIsNone(self.processor.section_actions(self.event))
        self.assertEqual(self.owner.reads, [])

    def test_query_filters_unknown_owner_and_direct_confirmation_and_rejects_screen_scope(self):
        """Новая проекция не обходит проверку владельца и адресного ожидания."""
        self.owner.available = (
            ReplyAction("Чужое", "/name Новый", scope=ActionScope.SECTION),
            ReplyAction("Неизвестное", "/unregistered", scope=ActionScope.SECTION),
            ReplyAction("Подтвердить", "/confirm selected task 1 1 yes", scope=ActionScope.SECTION),
            *self.owner.available,
        )
        self.assertEqual(tuple(action.command for action in self.processor.section_actions(self.event)),
                         ("/choose first",))
        self.owner.available = (ReplyAction("Выбор", "/choose first"),)
        with self.assertRaisesRegex(ValueError, "belonging to a screen"):
            self.processor.section_actions(self.event)

    def test_menu_label_executes_current_command_and_rejects_removed_action(self):
        """Подпись не исполняется как текст; исчезнувшее действие не запускается."""
        event = replace(self.event, channel="vk", text="Выбрать", menu_choice=True)
        self.processor(event)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")
        before = len(self.owner.calls)
        self.owner.available = ()
        self.processor(event)
        self.assertEqual(len(self.owner.calls), before)

    def test_layout_and_navigation_metadata_reach_owner_and_bound_action(self):
        """Ядро не подменяет страницу перехода и ограничения адаптера."""
        limits = ActionLayoutLimits(30, 6, 5, 40)
        self.owner.available = (ReplyAction("Назад", "/choose page 2", scope=ActionScope.SECTION,
                                           purpose=ActionPurpose.NAVIGATION,
                                           destination=ScreenDestination("list", 2)),)
        response = self.processor(replace(self.event, text="/choose", action_layout=limits))
        self.assertIs(self.owner.layout, limits)
        self.assertEqual(response.bound_actions[0].destination, ScreenDestination("list", 2))
        self.assertEqual(response.bound_actions[0].purpose, ActionPurpose.NAVIGATION)

    def test_selection_requires_confirmed_message_and_preserves_stable_target(self):
        """Адресный выбор доступен лишь из доставленного поколения и один раз."""
        description = ReplyAction("11. Объект", "/choose stable-id", purpose=ActionPurpose.SELECTION)
        response = IncomingResponse(text_content("Выбери 11."), suggested_actions=(description,))
        self.assertEqual(self.issue(response).bound_actions, ())
        bound = self.issue(replace(response, view=ResponseView("selection")))
        button = bound.bound_actions[0]
        event = replace(self.event, text="/choose forged", callback_message_id="42")
        self.assertIn("больше недоступна", response_text(self.processor.select_action(event, button.token)))
        self.assertTrue(bound.view_delivery.start())
        bound.view_delivery.finish("42")
        self.processor.select_action(event, button.token)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose stable-id")
        self.assertIn("больше недоступна", response_text(self.processor.select_action(event, button.token)))

    def test_url_is_only_exposed_as_validated_bound_action(self):
        """Ссылка не регистрирует команду и отсутствует при отказе привязки."""
        response = IncomingResponse(text_content("Открой диалог."), suggested_actions=(
            ReplyAction("Бот", url="https://example.com/bot"),
        ))
        bound = self.issue(response)
        self.assertEqual(bound.bound_actions[0].url, "https://example.com/bot")
        self.assertEqual(bound.bound_actions[0].token, "")
        self.assertEqual(bound.suggested_actions, ())
        rejected = self.issue(response, event=replace(self.event, external_user_id="other"))
        self.assertEqual(rejected.bound_actions, ())
        self.assertEqual(rejected.suggested_actions, ())
        stale = self.issue(replace(response, view=ResponseView(
            "missing", update=True, unavailable=text_content("Открой заново."),
        )))
        self.assertEqual(stale.bound_actions, ())
        self.assertEqual(stale.suggested_actions, ())

    def test_background_url_is_removed_after_context_change(self):
        """Поздний ответ не раскрывает URL-кнопку из завершённого режима."""
        delivered = []
        self.processor(replace(self.event, text="/choose"), delivered.append)
        delivery = self.owner.delivery
        response = IncomingResponse(text_content("Диалог."), suggested_actions=(
            ReplyAction("Бот", url="https://example.com/bot"),
        ))
        delivery.send(self.event.reply_address, response)
        self.assertEqual(delivered[-1].response.bound_actions[0].url, "https://example.com/bot")
        self.processor(replace(self.event, text="/exit"))
        delivery.send(self.event.reply_address, response)
        self.assertEqual(delivered[-1].response.bound_actions, ())
        self.assertEqual(delivered[-1].response.suggested_actions, ())

    def test_section_source_failure_preserves_content_and_does_not_publish_empty_menu(self):
        """Недоступный источник меню не блокирует содержательный ответ."""
        self.owner.read_error = RuntimeError("menu source unavailable")
        updates = []
        with self.assertLogs(level="ERROR"):
            response = self.processor(replace(self.event, text="/choose"),
                                      menu_update_handler=updates.append, defer_origin_menu=True)
        self.assertIn("Действия", response_text(response))
        deliveries = []

        def send(update):
            """Отмечает фактическую доставку ответа с необязательным меню."""
            deliveries.append((response_text(response), update))
            return True, None

        with self.assertLogs(level="ERROR"):
            self.assertTrue(self.processor.deliver_response_menu(self.event, updates.append, send))
        self.assertEqual(deliveries, [(response_text(response), None)])
        self.assertEqual(updates, [])
        self.assertIn("menu source unavailable", self.processor.menu_delivery_failures[self.event.reply_address])

    def test_old_owner_does_not_trigger_additional_action_context_reads(self):
        """Не перенесённый режим сохраняет прежний путь построения меню."""
        processor = ChannelMessageProcessor(database_path=self.path, capabilities=(FakeCapability(),))
        with patch.object(IncomingMessageDispatcher, "action_context",
                          side_effect=AssertionError("unexpected extra context read")):
            self.assertIsNone(processor.section_actions(self.event))

    def test_context_change_during_query_rejects_the_whole_snapshot(self):
        """Снимок старого раздела не публикуется после смены владельца режима."""
        def leave():
            """Завершает режим во время чтения действий."""
            self.owner.on_read = None
            self.processor(replace(self.event, text="/exit"))

        self.owner.on_read = leave
        with self.assertRaisesRegex(RuntimeError, "context changed"):
            self.processor.section_actions(self.event)

    def test_url_is_rejected_if_mode_revision_changes_between_context_snapshots(self):
        """Ссылка не переживает повторный вход между выполнением и привязкой."""
        original = IncomingMessageDispatcher.action_context
        reads = 0

        def changed(dispatcher, event):
            """Моделирует смену ревизии сразу после исходного снимка."""
            nonlocal reads
            context = original(dispatcher, event)
            reads += 1
            return context if reads == 1 else replace(context, mode_revision=context.mode_revision + 1)

        response = IncomingResponse(text_content("Диалог."), suggested_actions=(
            ReplyAction("Бот", url="https://example.com/bot"),
        ))
        with patch.object(IncomingMessageDispatcher, "action_context", changed):
            rejected = self.issue(response)
        self.assertEqual(rejected.bound_actions, ())
        self.assertEqual(rejected.suggested_actions, ())

    def test_failed_replacement_keeps_previous_selection_and_never_activates_new_page(self):
        """Неудачная отправка нового экрана сохраняет привязку доставленной страницы."""
        def screen(target):
            """Описывает поколение адресного выбора со стабильной целью."""
            return IncomingResponse(text_content("Выбери номер или /cancel."), suggested_actions=(
                ReplyAction("1. Объект", f"/choose {target}", purpose=ActionPurpose.SELECTION),
            ), view=ResponseView("choice", cleanup_previous=True))

        first = self.issue(screen("first"))
        self.assertTrue(first.view_delivery.start())
        first.view_delivery.finish("42")
        replacement = self.issue(screen("second"))
        self.assertTrue(replacement.view_delivery.start())
        replacement.view_delivery.finish(None)
        event = replace(self.event, callback_message_id="42")
        self.processor.select_action(event, replacement.bound_actions[0].token)
        self.assertEqual(self.owner.calls, [])
        self.processor.select_action(event, first.bound_actions[0].token)
        self.assertEqual(self.owner.calls[-1][1].text, "/choose first")


if __name__ == "__main__":
    unittest.main()
