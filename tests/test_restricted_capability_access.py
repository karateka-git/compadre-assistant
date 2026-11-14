"""Проверяет доверенный допуск через реальные маршруты профиля и режима."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.sqlite.capability_preference_repository import SQLiteUserCapabilityPreferenceRepository
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.capability_preferences import UserCapabilityPreference
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityHelp, CommandMenuUpdateHandler, ConfirmationQueryResult,
    HandlerContext, HelpSection, IncomingResponse, PendingConfirmationQueryPort,
    PendingInputContinuationPort, PendingInputResult, ReplyAction,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.message_content import text_content
from tests.response_assertions import response_text
from tests.test_incoming_boundary import FakeCapability


class RestrictedCapability(FakeCapability, PendingInputContinuationPort, PendingConfirmationQueryPort):
    """Фиксирует входы и вызовы владельца ограниченного режима."""

    def __init__(self) -> None:
        """Объявляет отдельную команду режима и доверенный доступ."""
        super().__init__("restricted", "/restricted")
        self._descriptor = replace(
            self.descriptor, requires_profile_access=True,
            help=CapabilityHelp(sections=(HelpSection(
                "Действия", (CapabilityCommand("/restricted_action", (), "проверить допуск"),),
            ),)),
        )

    def list_pending_confirmations(self, context: HandlerContext, origin: ReplyAddress) -> ConfirmationQueryResult:
        """Сообщает об отсутствии адресных подтверждений."""
        return ConfirmationQueryResult("complete")

    def continue_input(self, context: HandlerContext, pending: PendingInput, event: IncomingEvent) -> PendingInputResult:
        """Отклоняет вызов продолжения в проверках с отозванным правом."""
        raise AssertionError("Недопущенное продолжение вызвано")


class RestrictedCapabilityAccessTest(unittest.TestCase):
    """Проверяет одного доверенного AppUser, обычный и неизвестный профиль."""

    def setUp(self) -> None:
        """Создаёт связанную владельцу пару каналов и отдельного пользователя."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "app.db"
        self.owner = IncomingEvent("telegram", "1001", "1", "/start", "1001", "owner")
        self.linked = IncomingEvent("vk", "3003", "1", "/start", "3003")
        self.other = IncomingEvent("telegram", "2002", "1", "/start", "2002", "owner")
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            service = create_user_identity_service(connection)
            owner = service.register_with_name(IncomingIdentity("telegram", "1001", "1001"), display_name="Владелец")
            user = service.register_with_name(IncomingIdentity("telegram", "2002", "2002"), display_name="Другой")
            code = service.create_link_code(owner.id)
            service.link(code=code.code, identity=IncomingIdentity("vk", "3003", "3003"))
        self.owner_id = owner.id
        self.other_id = user.id
        self.trusted = {owner.id}
        self.capability = RestrictedCapability()
        self.public = FakeCapability()
        self.processor = ChannelMessageProcessor(
            database_path=self.database_path, capabilities=(self.capability, self.public),
            profile_access={"restricted": self.trusted.__contains__},
        )

    def send(self, text: str, event: IncomingEvent | None = None, *, menus: CommandMenuUpdateHandler | None = None) -> str:
        """Передаёт команду обычному процессору и возвращает простой текст."""
        return response_text(self.processor(replace(event or self.owner, text=text), menu_update_handler=menus))

    def test_only_profile_grant_allows_entry_and_mode_commands_in_each_chat(self) -> None:
        """Режим каждого чата независим, а имя, адрес чата и выбор права не дают."""
        with connect_database(self.database_path) as connection:
            SQLiteUserCapabilityPreferenceRepository(connection).upsert(
                UserCapabilityPreference(self.other_id, "restricted", True),
            )
        self.assertIn("войди в режим через /restricted", self.send("/restricted_action"))
        self.assertEqual(self.send("/restricted"), "Актуальное состояние")
        self.assertEqual(self.send("/restricted_action"), "Тестовый ответ")
        self.assertIn("войди в режим через /restricted", self.send("/restricted_action", self.linked))
        self.assertEqual(self.send("/restricted", self.linked), "Актуальное состояние")
        self.assertEqual(self.send("/restricted_action", self.linked), "Тестовый ответ")
        for event in (replace(self.other, chat_id=self.owner.chat_id), replace(self.owner, external_user_id=None)):
            self.assertIn("нет доступа", self.send("/restricted", event))
            self.assertIn("нет доступа", self.send("/restricted_action", event))
        self.assertEqual(len(self.capability.entries), 2)
        self.assertEqual(len(self.capability.calls), 2)

    def test_catalog_preferences_and_initial_menus_hide_untrusted_capability(self) -> None:
        """Неподтверждённый профиль не получает скрытое действие или способ выдать себе право."""
        self.assertIn("restricted", self.send("/capabilities"))
        unknown = replace(self.owner, chat_id="4004", external_user_id="4004")
        self.assertIn("/register", self.send("/restricted", unknown))
        for event in (self.other, unknown):
            updates = []
            for command in ("/help", "/start", "/status", "/capabilities", "/enable", "/disable"):
                self.assertNotIn("restricted", self.send(command, event, menus=updates.append))
            self.assertTrue(updates)
            for update in updates:
                self.assertFalse(any("restricted" in item.command for item in update.menu.items))

        self.assertIn("Неизвестная возможность", self.send("/enable restricted", self.other))
        self.assertIn("Неизвестная возможность", self.send("/disable restricted", self.other))
        self.send("/disable", self.other)
        self.assertIn("Неизвестная возможность", self.send("restricted", self.other))
        with connect_database(self.database_path) as connection:
            self.assertEqual(SQLiteUserCapabilityPreferenceRepository(connection).get_for_user(self.other_id), ())

    def test_revocation_hides_saved_mode_and_rejects_buttons_and_pending(self) -> None:
        """Повторное чтение допуска запрещает прямой и отложенный вызовы после отзыва."""
        admitted_menus = []
        self.send("/restricted", menus=admitted_menus.append)
        self.assertIn("/restricted_action", tuple(item.command for item in admitted_menus[-1].menu.items))
        response = IncomingResponse(text_content("Действия"), suggested_actions=(
            ReplyAction("Выполнить", "/restricted_action"),
            ReplyAction("Выключить", "/disable restricted"),
        ))
        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            buttons = self.processor(self.owner).bound_actions
        self.assertEqual(len(buttons), 2)
        self.processor._pending_inputs.begin(
            self.owner.reply_address, "restricted", "value", app_user_id=self.owner_id, required_mode="restricted",
        )
        self.trusted.clear()
        self.assertIn("нет доступа", self.send("значение"))
        self.assertIsNone(self.processor._pending_inputs.get(self.owner.reply_address))
        for button in buttons:
            self.assertIn("больше недоступна", response_text(self.processor.select_action(self.owner, button.token)))
        self.assertIn("нет доступа", self.send("/restricted_action"))
        self.assertIn("нет доступа", self.send("свободный текст"))
        self.assertNotIn("restricted", self.send("/status"))
        self.assertNotIn("restricted", self.send("/help"))
        revoked_menus = []
        self.processor.reconcile_menus(revoked_menus.append)
        self.assertTrue(revoked_menus)
        for update in revoked_menus:
            self.assertFalse(any("restricted" in item.command for item in update.menu.items))
        with patch.object(IncomingMessageDispatcher, "execute", return_value=response):
            self.assertFalse(self.processor(self.owner).bound_actions)
        self.assertFalse(self.capability.calls)
        self.trusted.add(self.owner_id)
        self.assertEqual(self.send("/restricted_action"), "Тестовый ответ")
        self.assertIn("выключена", self.send("/disable restricted"))
        self.assertIn("лично выключена", self.send("/restricted_action"))

    def test_profile_link_requires_private_verified_address(self) -> None:
        """Код и перенос профиля не выдаются в группу или по адресу без автора."""
        for event in (replace(self.owner, chat_id="-5005"), replace(self.owner, external_user_id=None)):
            for command in ("/link_code", "/link", "/link abc", "/confirm core identity 1 1 yes"):
                self.assertIn("открой личный чат", self.send(command, event))
            self.assertIsNone(self.processor._pending_inputs.get(event.reply_address))
        self.assertIn("/link ", self.send("/link_code"))

    def test_missing_policy_and_disabled_descriptors_deny_by_default(self) -> None:
        """Ограничение сохраняется в отключённом каталоге и без настроенного правила."""
        for registry in (CapabilityRegistry((self.capability,)), CapabilityRegistry((), (self.capability.descriptor,))):
            with self.subTest(registry=registry):
                access = registry.for_user((), app_user_id=self.owner_id).status("restricted")
                self.assertFalse(access.profile_allowed)
                self.assertFalse(access.effective)
                self.assertNotIn("/restricted", registry.entry_hint())
        active = CapabilityRegistry((self.capability,), profile_access={"restricted": lambda user_id: True})
        self.assertFalse(active.for_user(()).status("restricted").profile_allowed)
        disabled = CapabilityRegistry((), (self.capability.descriptor,), profile_access={"restricted": self.trusted.__contains__})
        access = disabled.for_user((), app_user_id=self.owner_id).status("restricted")
        self.assertTrue(access.profile_allowed)
        self.assertFalse(access.effective)
