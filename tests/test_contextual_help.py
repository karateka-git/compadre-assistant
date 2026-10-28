"""Матрица контекстной справки через публичный вход диспетчера."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
import unittest

from compadre_assistant.capabilities.obsidian.presentation.commands import obsidian_descriptor
from compadre_assistant.capabilities.ask.presentation.handler import AskCapability
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.domain.users.entities import AppUser, IdentityRebindConfirmation
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.presentation.incoming.ports import ConfirmationQueryResult
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from tests.capability_preference_helpers import MemoryCapabilityPreferences
from tests.test_confirmation_snapshot import QueryCapability
from tests.test_pending_input import InputCapability
from tests.test_incoming_boundary import FakeCapability, MemoryChatModes, _event
from tests.response_assertions import response_text


class HelpCapability(QueryCapability, InputCapability):
    """Объявляет настоящий каталог Obsidian и наблюдаемые нейтральные ports."""


class ContextualHelpTest(unittest.TestCase):
    """Проверяет контекст, права, TTL и независимость просмотра от исполнения."""

    def setUp(self):
        """Собирает профиль с Obsidian, Ask и дополнительным эффективным входом."""
        self.now = datetime(2026, 10, 27, tzinfo=UTC)
        self.store = PendingInputStore(lambda: self.now)
        self.identity = Mock()
        self.identity.resolve.return_value = AppUser(23, "Имя")
        self.identity.get_pending_rebind.return_value = None
        self.identity.list_known_private_chats.return_value = ()
        self.obsidian = HelpCapability()
        self.obsidian._descriptor = replace(obsidian_descriptor(), enabled_by_default=True)
        self.ask = AskCapability(Mock())
        self.other = FakeCapability("other", "/other")
        self.preferences = MemoryCapabilityPreferences()
        self.modes = MemoryChatModes()
        self.key = ChatModeKey(23, "telegram", "chat-1")
        self.registry = CapabilityRegistry((self.obsidian, self.ask, self.other))
        self.dispatcher = self.build()

    def build(self):
        """Внедряет общий каталог, не открывая предметные repositories."""
        return IncomingMessageDispatcher(registry=self.registry, user_identity_service=self.identity,
            chat_mode_repository=self.modes, capability_preference_repository=self.preferences,
            pending_inputs=self.store)

    def send(self, command="/help", chat="chat-1", channel="telegram"):
        """Возвращает текст результата настоящего маршрута."""
        return response_text(self.dispatcher.execute(_event(command, chat, channel)))

    def rebind(self):
        """Задаёт актуальное адресное действие ядра в исходном чате."""
        self.identity.get_pending_rebind.return_value = IdentityRebindConfirmation(
            4, 23, 24, "telegram", "chat-1", "chat-1", "hash", self.now + timedelta(minutes=10))

    def test_unknown_and_registration_overlay(self):
        """До профиля доступны два действия, а регистрация заменяет их вводом имени."""
        self.identity.resolve.return_value = None
        text = self.send()
        for command in ("/register", "/link", "/link <код>"):
            self.assertIn(command, text)
        for command in ("/status", "/name", "/ask", "/obsidian", "/cancel", "/confirm"):
            self.assertNotIn(command, text)
        self.send("/register")
        pending = self.store.get(_event("x").reply_address)
        text = self.send()
        self.assertIn("Регистрация не завершена", text)
        self.assertIn("/cancel", text)
        self.assertNotIn("/link", text)
        self.assertEqual(self.store.get(pending.chat_key), pending)
        self.identity.register_with_name.assert_not_called()
        self.send("/cancel")
        self.assertNotIn("/cancel", self.send())

    def test_main_lists_entries_without_subject_commands(self):
        """Основной режим показывает входы, но не внутренние команды возможностей."""
        text = self.send()
        for command in ("/name", "/capabilities", "/link_code", "/obsidian", "/ask", "/other"):
            self.assertIn(command, text)
        for command in ("/github_sync", "/reanalyze", "/cancel", "/confirm"):
            self.assertNotIn(command, text)
        self.assertIn("одноразовый", text)
        self.assertFalse(self.other.entries)

    def test_active_mode_hides_other_entries_but_preserves_manual_access(self):
        """Активный каталог не расширяет общую справку; скрытый вход остаётся рабочим."""
        self.modes.save(ChatMode(self.key, "obsidian"))
        text = self.send()
        for command in ("/github_sync", "/reanalyze", "/exit", "/status", "/capabilities", "/link_code", "/help", "/ask"):
            self.assertIn(command, text)
        for command in ("/name", "/register", "/enable", "/other", "/confirm", "/cancel"):
            self.assertNotIn(command, text)
        self.assertIn("/other", self.send(chat="other"))
        self.assertEqual(self.modes.get(self.key).capability_id, "obsidian")
        self.send("/other")
        self.assertTrue(self.other.entries)

    def test_unavailable_modes_and_effective_alternatives(self):
        """Выключенный, серверно недоступный и неизвестный режимы имеют отдельную ветку."""
        for reason in ("personal", "server", "unknown"):
            with self.subTest(reason=reason):
                self.modes.save(ChatMode(self.key, "missing" if reason == "unknown" else "obsidian"))
                self.preferences.rows[(23, "obsidian")] = reason != "personal"
                if reason == "server":
                    self.registry = CapabilityRegistry((self.ask, self.other), (self.obsidian.descriptor,))
                    self.dispatcher = self.build()
                text = self.send()
                self.assertIn("недоступен", text)
                self.assertIn("лично выключена" if reason == "personal" else "недоступна на сервере", text)
                self.assertIn("/exit", text)
                self.assertIn("/ask", text)
                self.assertNotIn("/github_sync", text)
                self.assertNotIn("/obsidian", text)
                self.assertIn("/name", text)

    def test_pending_is_preserved_until_expired_disabled_or_context_changed(self):
        """Просмотр сохраняет ID и срок, но удаляет ввод, который уже нельзя исполнить."""
        self.modes.save(ChatMode(self.key, "obsidian"))
        for invalidation in ("ttl", "disabled", "mode", "profile"):
            with self.subTest(invalidation=invalidation):
                self.identity.resolve.return_value = AppUser(23, "Имя")
                self.preferences.rows[(23, "obsidian")] = True
                self.modes.save(ChatMode(self.key, "obsidian"))
                pending = self.store.begin(_event("x").reply_address, "obsidian", "article", app_user_id=23,
                                           required_mode="obsidian", value_label="ID статьи Obsidian")
                text = self.send()
                self.assertIn("Ожидается ID статьи Obsidian", text)
                self.assertIn("/cancel", text)
                self.assertEqual(self.store.get(pending.chat_key), pending)
                if invalidation == "ttl": self.now += timedelta(minutes=10)
                elif invalidation == "disabled": self.preferences.rows[(23, "obsidian")] = False
                elif invalidation == "mode": self.modes.clear(self.key)
                else: self.identity.resolve.return_value = AppUser(24, "Другой")
                self.assertNotIn("/cancel", self.send())
                self.assertIsNone(self.store.get(pending.chat_key))
        self.assertFalse(self.obsidian.values)

    def test_ask_overlay_and_unknown_link_keep_base_context(self):
        """Ask поверх режима и код до регистрации показывают конкретное ожидаемое значение."""
        self.modes.save(ChatMode(self.key, "obsidian"))
        self.send("/ask")
        text = self.send()
        self.assertIn("Ожидается вопрос для Ask", text)
        self.assertIn("/github_sync", text)
        self.assertIn("/cancel", text)
        self.assertEqual(self.modes.get(self.key).capability_id, "obsidian")
        self.identity.resolve.return_value = None
        self.send("/link")
        text = self.send()
        self.assertIn("Ожидается код привязки", text)
        self.assertIn("/register", text)
        self.assertIn("/cancel", text)

    def test_confirmations_complete_incomplete_and_pre_profile(self):
        """Адресные команды отделены; голая confirm требует профиля и полного снимка."""
        self.rebind()
        self.modes.save(ChatMode(self.key, "obsidian"))
        for registered, complete in ((True, True), (True, False), (False, True)):
            with self.subTest(registered=registered, complete=complete):
                self.identity.resolve.return_value = AppUser(23, "Имя") if registered else None
                self.obsidian.query.return_value = ConfirmationQueryResult("complete" if complete else "unavailable")
                text = self.send()
                self.assertIn("/confirm core identity 4 1 yes", text)
                self.assertIn("/confirm core identity 4 1 no", text)
                self.assertEqual("/confirm —" in text, registered and complete)
                self.assertEqual("неполный" in text, not complete)
        self.identity.confirm_rebind.assert_not_called()
        self.identity.cancel_rebind.assert_not_called()

    def test_help_does_not_grant_access_after_preferences_change(self):
        """Показанный вход повторно проверяет доступ при следующем вызове."""
        self.assertIn("/other", self.send())
        self.preferences.rows[(23, "other")] = False
        self.assertIn("лично выключена", self.send("/other"))
        self.assertFalse(self.other.entries)

    def test_active_ask_disabled_and_no_available_entries(self):
        """Личный запрет скрывает одноразовый Ask, пустой каталог даёт явный следующий шаг."""
        self.modes.save(ChatMode(self.key, "obsidian"))
        self.preferences.rows[(23, "ask")] = False
        self.assertNotIn("/ask", self.send())
        self.modes.clear(self.key)
        self.preferences.rows[(23, "obsidian")] = False
        self.preferences.rows[(23, "other")] = False
        text = self.send()
        self.assertIn("Сейчас нет доступных возможностей", text)
        self.assertNotIn("/obsidian", text)
        self.assertIn("/capabilities", text)

    def test_unavailable_pending_owner_and_adjacent_chats(self):
        """Серверное отключение убирает ожидание только проверяемого чата."""
        pending = self.store.begin(_event("x").reply_address, "obsidian", "article", app_user_id=23)
        adjacent = self.store.begin(_event("x", "adjacent").reply_address, "core", "profile_name", app_user_id=23)
        self.registry = CapabilityRegistry((self.ask,), (self.obsidian.descriptor,))
        self.dispatcher = self.build()
        self.assertNotIn("/cancel", self.send())
        self.assertIsNone(self.store.get(pending.chat_key))
        self.assertEqual(self.store.get(adjacent.chat_key), adjacent)
        self.assertIn("/cancel", self.send(chat="adjacent"))
        self.assertNotIn("/cancel", self.send(channel="vk"))

    def test_empty_incomplete_snapshot_does_not_offer_bare_confirmation(self):
        """Неудачный опрос без известных действий остаётся явной неполнотой."""
        self.modes.save(ChatMode(self.key, "obsidian"))
        self.obsidian.query.return_value = ConfirmationQueryResult("unavailable")
        text = self.send()
        self.assertIn("Не удалось получить полный список", text)
        self.assertNotIn("/confirm", text)

    def test_saved_one_shot_is_not_an_active_mode(self):
        """Старый режим Ask даёт выход и рабочие входы без ложной ошибки источника."""
        self.modes.save(ChatMode(self.key, "ask"))
        text = self.send()
        self.assertIn("Сохранённый режим ask устарел", text)
        self.assertIn("/exit", text)
        self.assertIn("/ask", text)
        self.assertIn("/obsidian", text)
        self.assertIn("/name", text)
        self.assertNotIn("Команды режима ask", text)
        self.assertNotIn("Не удалось получить", text)
        self.assertNotIn("/confirm", text)
        self.assertEqual(self.modes.get(self.key).capability_id, "ask")
