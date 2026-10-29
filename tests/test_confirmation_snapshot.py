"""Проверяет чтение подтверждений и повторную проверку показанных действий."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
import unittest

from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import ObsidianProposalConfirmationService
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import ObsidianProposalAction
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageHandler
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import AppUser, IdentityRebindConfirmation, IncomingIdentity
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import (
    ConfirmationAction, ConfirmationQueryResult, HandlerContext,
    PendingConfirmation, PendingConfirmationQueryPort,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from tests.capability_preference_helpers import MemoryCapabilityPreferences
from tests.response_assertions import response_text
from tests.test_incoming_boundary import FakeCapability, MemoryChatModes, _event
from tests.test_process_incoming_message import FakeArticleUrlUseCase, FakeVaultSelectionManager, _proposal as incoming_proposal
from tests.test_obsidian_proposal_repository import _database, _proposal
from tests.test_user_identity_repository import _service as identity_service
from tests.test_vault_selection import _service as vault_service, FakeGitHubRepositoryGateway
from compadre_assistant.capabilities.obsidian.data.sqlite.obsidian_proposal_repository import SQLiteObsidianProposalRepository


class QueryCapability(FakeCapability, PendingConfirmationQueryPort):
    """Возвращает управляемый снимок через публичный контракт."""

    def __init__(self):
        super().__init__()
        self.query = Mock(return_value=ConfirmationQueryResult("complete"))

    def list_pending_confirmations(self, context, origin):
        """Передаёт проверенный контекст наблюдаемому источнику."""
        return self.query(context, origin)


class ConfirmationSnapshotTest(unittest.TestCase):
    """Проверяет агрегатор ядра без доступа к внутренним данным возможности."""

    def setUp(self):
        self.user = AppUser(23, "Тест")
        self.identity = Mock()
        self.identity.resolve.return_value = self.user
        self.identity.get_pending_rebind.return_value = None
        self.capability = QueryCapability()
        self.modes = MemoryChatModes()
        self.key = ChatModeKey(23, "telegram", "chat-1")
        self.modes.save(ChatMode(self.key, "selected"))
        self.preferences = MemoryCapabilityPreferences()
        self.dispatcher = self.make_dispatcher()

    def make_dispatcher(self, registry=None):
        """Собирает настоящий dispatcher с наблюдаемыми источниками."""
        return IncomingMessageDispatcher(
            registry=registry or CapabilityRegistry((self.capability,)),
            user_identity_service=self.identity, chat_mode_repository=self.modes,
            capability_preference_repository=self.preferences,
        )

    def rebind(self):
        """Создаёт результат сервиса identity для текущего исходного чата."""
        return IdentityRebindConfirmation(
            4, 23, 24, "telegram", "chat-1", "chat-1", "hash",
            datetime.now(UTC) + timedelta(minutes=10),
        )

    def pending(self, origin=None, owner="selected"):
        """Создаёт нейтральное адресное действие тестовой возможности."""
        return PendingConfirmation(owner, "operation", 7, 2,
            origin or _event("x").reply_address, tuple(
                ConfirmationAction(f"{owner}:{decision}", decision,
                    f"/confirm {owner} operation 7 2 {decision}")
                for decision in ("yes", "no")))

    def send(self, command="/confirm", **kwargs):
        """Выполняет команду через публичный вход адаптеров."""
        return self.dispatcher.execute(_event(command, **kwargs))

    def test_complete_multiple_is_read_only_and_contains_both_choices(self):
        """Полный просмотр объединяет решения, не исполняя ни одного."""
        self.identity.get_pending_rebind.return_value = self.rebind()
        self.capability.query.return_value = ConfirmationQueryResult("complete", (self.pending(),))
        response = self.send()
        self.assertEqual(len(response.actions), 4)
        for action in response.actions:
            self.assertIn(action.command, response_text(response))
        self.assertNotIn("неполный", response_text(response))
        self.identity.confirm_rebind.assert_not_called()
        self.identity.cancel_rebind.assert_not_called()
        self.assertFalse(self.capability.calls)
        self.assertEqual(self.modes.get(self.key).capability_id, "selected")

    def test_complete_empty(self):
        """Только полный пустой снимок означает отсутствие действий."""
        self.assertEqual(response_text(self.send()), "В этом чате нет действий, которые нужно подтвердить.")

    def test_unavailable_empty_is_not_no_actions(self):
        """Сбой источника не превращается в утверждение об отсутствии действий."""
        for result in (ConfirmationQueryResult("unavailable"), RuntimeError("secret")):
            with self.subTest(result=result):
                self.capability.query.side_effect = result if isinstance(result, Exception) else None
                self.capability.query.return_value = result
                text = response_text(self.send())
                self.assertIn("Не удалось получить полный список", text)
                self.assertNotIn("secret", text)

    def test_partial_core_and_partial_capability_are_kept(self):
        """Известные команды сохраняются независимо от сбоя другого источника."""
        self.identity.get_pending_rebind.return_value = self.rebind()
        self.capability.query.return_value = ConfirmationQueryResult("unavailable", (self.pending(),))
        response = self.send()
        self.assertIn("Список действий неполный", response_text(response))
        self.assertEqual(len(response.actions), 4)
        self.identity.get_pending_rebind.side_effect = RuntimeError("identity offline")
        response = self.send()
        self.assertIn("Список действий неполный", response_text(response))
        self.assertEqual(len(response.actions), 2)

    def test_identity_before_profile_and_original_identity_filter(self):
        """Identity доступна без профиля и ограничена полным исходным адресом."""
        self.identity.resolve.return_value = None
        self.identity.get_pending_rebind.return_value = self.rebind()
        self.assertEqual(len(self.send().actions), 2)
        self.capability.query.assert_not_called()
        for change in ({"chat_id": "other"}, {"channel": "vk"}, {"external_user_id": "other"}):
            with self.subTest(change=change):
                self.identity.get_pending_rebind.return_value = replace(self.rebind(), **change)
                self.assertEqual(len(self.send().actions), 0)
        self.identity.confirm_rebind.return_value = self.user
        self.send("/confirm core identity 4 1 yes")
        self.identity.confirm_rebind.assert_called_once()

    def test_foreign_channel_chat_and_owner_are_never_shown(self):
        """Ядро повторно фильтрует адрес и владельца публичных записей."""
        records = tuple(self.pending(origin) for origin in (
            ReplyAddress("telegram", "other"), ReplyAddress("vk", "chat-1"),
        )) + (self.pending(owner="other"), self.pending())
        self.capability.query.return_value = ConfirmationQueryResult("complete", records)
        self.assertEqual(len(self.send().actions), 2)

    def test_only_effectively_accessible_active_mode_is_queried(self):
        """Неактивные, выключенные и отсутствующие режимы не опрашиваются."""
        for mode, enabled in ((None, True), ("selected", False), ("missing", True)):
            with self.subTest(mode=mode, enabled=enabled):
                self.modes.clear(self.key)
                if mode:
                    self.modes.save(ChatMode(self.key, mode))
                self.preferences.rows[(23, "selected")] = enabled
                self.assertIn("нет действий", response_text(self.send()))
        self.capability.query.assert_not_called()
        self.modes.save(ChatMode(self.key, "selected"))
        self.dispatcher = self.make_dispatcher(CapabilityRegistry((), (self.capability.descriptor,)))
        self.assertIn("нет действий", response_text(self.send()))
        self.capability.query.assert_not_called()

    def test_missing_query_is_unavailable(self):
        """Отсутствующий публичный запрос считается неизвестным источником."""
        self.dispatcher = self.make_dispatcher(CapabilityRegistry((FakeCapability(),)))
        self.assertIn("Не удалось получить полный список", response_text(self.send()))

    def test_profile_resolution_failure_preserves_core_actions(self):
        """Сбой разрешения профиля оставляет известные команды identity."""
        self.identity.get_pending_rebind.return_value = self.rebind()
        self.identity.resolve.side_effect = RuntimeError("profile offline")
        response = self.send()
        self.assertIn("Список действий неполный", response_text(response))
        self.assertEqual(len(response.actions), 2)

    def test_exact_command_rechecks_access_and_mode_after_snapshot(self):
        """Опубликованное действие не обходит последующее отключение режима."""
        self.capability.query.return_value = ConfirmationQueryResult("complete", (self.pending(),))
        command = self.send().actions[0].command
        self.preferences.rows[(23, "selected")] = False
        self.send(command)
        self.assertFalse(self.capability.calls)
        self.preferences.rows[(23, "selected")] = True
        self.modes.clear(self.key)
        self.send(command)
        self.assertFalse(self.capability.calls)

    def test_invalid_address_numbers_never_reach_identity(self):
        """Некорректные числовые адреса отклоняются до исполнения."""
        for value in ("²", "0", "9" * 5000, str(2**63)):
            self.send(f"/confirm core identity {value} 1 yes")
        self.identity.confirm_rebind.assert_not_called()


class ObsidianConfirmationSnapshotTest(unittest.TestCase):
    """Проверяет предметный query, неполноту и короткие ответы."""

    def setUp(self):
        self.user = AppUser(42, "Тест")
        self.origin = ReplyAddress("telegram", "chat-1")
        self.proposal = incoming_proposal()
        self.service = Mock()
        self.service.list_pending.return_value = (self.proposal,)
        self.manager = FakeVaultSelectionManager()
        self.handler = ObsidianMessageHandler(article_url_use_case=FakeArticleUrlUseCase(),
            obsidian_confirmation_service=self.service, vault_selection_manager=self.manager)

    def query(self):
        """Вызывает публичный предметный port без команды решения."""
        return self.handler.list_pending_confirmations(HandlerContext(self.user), self.origin)

    def test_multiple_proposals_and_vault_have_exact_actions(self):
        """Несколько предложений и операция vault сохраняют обе стороны выбора."""
        self.service.list_pending.return_value += (replace(self.proposal, id=9, article_id=8),)
        self.manager.disconnect_pending = True
        snapshot = self.query()
        self.assertEqual(snapshot.status, "complete")
        self.assertEqual(len(snapshot.confirmations), 3)
        self.assertTrue(all(len(item.actions) == 2 for item in snapshot.confirmations))
        self.service.confirm.assert_not_called()
        self.assertTrue(self.manager.disconnect_pending)

    def test_missing_or_failing_service_preserves_known_actions(self):
        """Неполный снимок сохраняет команды, но запрещает короткое решение."""
        self.manager.disconnect_pending = True
        for service in (None, Mock(list_pending=Mock(side_effect=RuntimeError("offline")))):
            self.handler._obsidian_confirmation_service = service
            snapshot = self.query()
            self.assertEqual(snapshot.status, "unavailable")
            self.assertEqual(len(snapshot.confirmations), 1)
            response = self.handler.handle(HandlerContext(self.user), _event("да")).response
            self.assertIn("не обработан", response_text(response))
            self.assertTrue(self.manager.disconnect_pending)

    def test_missing_vault_source_prevents_false_sole_proposal(self):
        """Отсутствие источника vault не доказывает единственность proposal."""
        self.handler._vault_selection_manager = None
        self.assertEqual(self.query().status, "unavailable")
        self.handler.handle(HandlerContext(self.user), _event("нет"))
        self.service.cancel.assert_not_called()

    def test_foreign_proposal_owner_and_origin_are_filtered(self):
        """В предметный снимок не попадают чужие пользователи и чаты."""
        self.service.list_pending.return_value = (
            replace(self.proposal, app_user_id=99),
            replace(self.proposal, origin=PendingOrigin("vk", "chat-1")),
            replace(self.proposal, origin=PendingOrigin("telegram", "other")),
        )
        self.assertEqual(self.query(), ConfirmationQueryResult("complete"))

    def test_multiple_pending_reject_short_answer(self):
        """Неоднозначный короткий ответ не меняет ни один источник."""
        self.manager.disconnect_pending = True
        response = self.handler.handle(HandlerContext(self.user), _event("да")).response
        self.assertIn("несколько", response_text(response))
        self.service.confirm.assert_not_called()
        self.assertTrue(self.manager.disconnect_pending)

    def test_short_answer_only_reaches_active_accessible_obsidian(self):
        """Короткое решение не доставляется вне исходного доступного режима."""
        identity = Mock()
        identity.resolve.return_value = self.user
        modes = MemoryChatModes()
        preferences = MemoryCapabilityPreferences()
        dispatcher = IncomingMessageDispatcher(registry=CapabilityRegistry((self.handler,)),
            user_identity_service=identity, chat_mode_repository=modes,
            capability_preference_repository=preferences)
        preferences.rows[(42, "obsidian")] = True
        dispatcher.execute(_event("да"))
        self.service.list_pending.assert_not_called()
        modes.save(ChatMode(ChatModeKey(42, "telegram", "chat-1"), "obsidian"))
        preferences.rows[(42, "obsidian")] = False
        dispatcher.execute(_event("да"))
        self.service.list_pending.assert_not_called()
        preferences.rows[(42, "obsidian")] = True
        dispatcher.execute(_event("да", chat="other"))
        self.service.list_pending.assert_not_called()
        self.service.confirm.assert_not_called()


class ConfirmationSnapshotSQLiteTest(unittest.TestCase):
    """Проверяет срок и состояние заново после просмотра на настоящей SQLite."""

    def test_proposal_without_ttl_and_vault_with_ttl_and_stale_choice(self):
        """Срок vault не ограничивает proposal, а старый выбор проверяется заново."""
        with _database() as connection:
            repository = SQLiteObsidianProposalRepository(connection)
            pending = repository.save_pending(_proposal(ObsidianProposalAction.SKIP))
            connection.execute("UPDATE obsidian_proposals SET created_at = '2020-01-01T00:00:00+00:00'")
            connection.commit()
            manager = vault_service(connection, FakeGitHubRepositoryGateway({}))
            origin = PendingOrigin("telegram", "chat")
            manager.request_disconnect(1, origin)
            proposal_service = ObsidianProposalConfirmationService(
                proposal_repository=repository, vault_repository=Mock(),
                lease_repository=Mock(), github_gateway=Mock(),
            )
            handler = ObsidianMessageHandler(article_url_use_case=FakeArticleUrlUseCase(),
                obsidian_confirmation_service=proposal_service, vault_selection_manager=manager)
            context = HandlerContext(AppUser(1, "Тест"))
            address = ReplyAddress("telegram", "chat")
            before = handler.list_pending_confirmations(context, address)
            self.assertEqual(len(before.confirmations), 2)
            self.assertEqual(handler.list_pending_confirmations(context,
                ReplyAddress("telegram", "other")), ConfirmationQueryResult("complete"))
            handler.handle(context, _event("да", chat="other"))
            self.assertIsNotNone(manager.get_selected(1))
            self.assertEqual(repository.get_by_id(pending.id, 1).status.value, "pending")
            command = before.confirmations[1].actions[0].command
            manager._clock = lambda: datetime(2030, 1, 1, tzinfo=UTC)
            after = handler.list_pending_confirmations(context, address)
            self.assertEqual([item.operation for item in after.confirmations], ["proposal"])
            response = handler.handle(context, _event(command, chat="chat")).response
            self.assertIn("истёк", response_text(response))
            self.assertIsNotNone(manager.get_selected(1))
            proposal_command = after.confirmations[0].actions[0].command
            repository.mark_cancelled(proposal_id=pending.id, app_user_id=1, origin=origin, version=pending.version)
            handler.handle(context, _event(proposal_command, chat="chat"))
            self.assertEqual(repository.get_by_id(pending.id, 1).status.value, "cancelled")
            proposal_service._github_gateway.commit_vault_markdown.assert_not_called()

    def test_identity_snapshot_then_expiry_or_completion_rejects_old_command(self):
        """Реальная identity повторно проверяет исходный адрес, версию и срок."""
        for completed in (False, True):
            with self.subTest(completed=completed), _database() as connection:
                service = identity_service(connection)
                source = IncomingIdentity("telegram", "sender", "chat-1")
                target = IncomingIdentity("vk", "target", "target-chat")
                service.register_with_name(source, display_name="Источник")
                target_user = service.register_with_name(target, display_name="Цель")
                code = service.create_link_code(target_user.id)
                pending = service.request_rebind_confirmation(code=code.code, identity=source)
                dispatcher = IncomingMessageDispatcher(registry=CapabilityRegistry(),
                    user_identity_service=service, chat_mode_repository=MemoryChatModes(),
                    capability_preference_repository=MemoryCapabilityPreferences())
                event = replace(_event("/confirm"), external_user_id="sender")
                for foreign in (replace(event, chat_id="other"),
                                replace(event, channel="vk"),
                                replace(event, external_user_id="other")):
                    self.assertFalse(dispatcher.execute(foreign).actions)
                response = dispatcher.execute(event)
                self.assertEqual(len(response.actions), 2)
                for foreign in (replace(event, chat_id="other"),
                                replace(event, channel="vk"),
                                replace(event, external_user_id="other")):
                    self.assertIsNotNone(dispatcher.execute(replace(foreign,
                        text=response.actions[0].command)).error)
                self.assertIsNotNone(dispatcher.execute(replace(event,
                    text=f"/confirm core identity {pending.id} 999 yes")).error)
                if completed:
                    service.cancel_rebind(source, confirmation_id=pending.id, version=pending.version)
                else:
                    service._clock = lambda: datetime(2030, 1, 1, tzinfo=UTC)
                response = dispatcher.execute(replace(event, text=response.actions[0].command))
                self.assertIn("устарело или истекло", response_text(response))
                self.assertNotEqual(service.resolve(source).id, target_user.id)


if __name__ == "__main__":
    unittest.main()
