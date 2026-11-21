"""Проверяет публичные адреса установки, выдачу кодов и обновление прежней БД."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_user_identity_service
from compadre_assistant.core.data.config import load_messenger_dialogs
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteIdentityLinkTokenRepository
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.domain.users.identity import IdentityAlreadyBoundError, InvalidLinkCodeError
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import CommandMenu, CommandMenuUpdate
from compadre_assistant.presentation.telegram.bot import _load_aiogram, _serialize_telegram_inline_keyboard
from compadre_assistant.presentation.vk.bot import _serialize_vk_command_menu
from tests.response_assertions import response_text


class ProfileMessengersTest(unittest.TestCase):
    """Проверяет адресата в сохранённом коде и актуальные действия двух каналов."""

    def setUp(self):
        """Выделяет отдельную БД для каждого сценария."""
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="profile-messengers-")))
        self.path = self.root / "test.db"
        self.db = self.enterContext(connect_database(self.path))

    def test_public_addresses_follow_installation_selection(self):
        """Второй сервис виден при включении, некорректные адреса скрыты."""
        with patch.dict(os.environ, {"ENABLED_CHANNELS": "telegram,vk", "VK_GROUP_ID": "731",
                                     "TELEGRAM_BOT_USERNAME": "@example_chat_bot"}, clear=True):
            self.assertEqual(load_messenger_dialogs(), {"telegram": "https://t.me/example_chat_bot",
                                                       "vk": "https://vk.com/write-731"})
            os.environ["ENABLED_CHANNELS"] = "telegram"
            os.environ["TELEGRAM_BOT_USERNAME"] = "test.invalid/other"
            self.assertEqual(load_messenger_dialogs(), {"telegram": None})

    def test_code_screen_and_current_menu_follow_link_state(self):
        """Настройки показывают результат привязки и реальный диалог с ботом."""
        apply_migrations(self.db)
        service = create_user_identity_service(self.db)
        service.register_with_name(IncomingIdentity("telegram", "11", "11"), display_name="Оля")
        dialogs = {"telegram": None, "vk": "https://vk.com/write-731"}
        processor = ChannelMessageProcessor(database_path=self.path, capabilities=(), messenger_dialogs=dialogs)
        event = IncomingEvent("telegram", "11", "1", "/settings", "11")
        settings = processor(event)
        self.assertIn("Мессенджеры", [action.label for action in settings.bound_actions])
        menu = processor(replace(event, text="/connect"))
        self.assertIn("VK — не привязан", response_text(menu))
        self.assertIn("Привязать VK", [action.label for action in processor.section_actions(event)])
        self.assertIn("недоступен", response_text(processor(replace(event, text="/connect other"))))
        issued = processor(replace(event, text="/connect vk"))
        self.assertIn(dialogs["vk"], response_text(issued))
        self.assertIn(dialogs["vk"], [action.url for action in issued.bound_actions])
        code = re.search(r"Код привязки: ([\w-]+)", response_text(issued)).group(1)
        self.assertIn("привязал", response_text(processor(IncomingEvent("vk", "22", "2", f"/link {code}", "22"))))
        self.assertIn(("Открыть бота", dialogs["vk"]), [(a.label, a.url) for a in processor.section_actions(event)])
        again = processor(replace(event, text="/connect vk"))
        self.assertIn("уже привязан", response_text(again))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM identity_link_tokens").fetchone()[0], 1)

    def test_choose_notification_channel_from_both_private_messengers(self):
        """Оба канала показывают первый выбор, актуальную настройку и кнопку смены."""
        apply_migrations(self.db)
        service = create_user_identity_service(self.db)
        telegram = IncomingIdentity("telegram", "11", "11")
        vk = IncomingIdentity("vk", "22", "22")
        owner = service.register_with_name(telegram, display_name="Оля")
        processor = ChannelMessageProcessor(database_path=self.path, capabilities=(), messenger_dialogs={
            "telegram": "https://t.me/example_bot", "vk": "https://vk.com/write-731",
        })
        event = IncomingEvent("telegram", "11", "1", "/settings", "11")
        processor(event)
        single = processor(replace(event, text="/connect"))
        self.assertIn("Уведомления приходят в Telegram", response_text(single))
        self.assertFalse(any(item.label.startswith("Получать в") for item in single.bound_actions))
        self.assertIn("не привязан", response_text(processor(replace(event, text="/notifications vk"))))
        service.link(code=service.create_link_code(owner.id, target_channel="vk").code, identity=vk)
        with self.db:
            self.db.execute("UPDATE app_users SET notification_channel = NULL WHERE id = ?", (owner.id,))
        first = processor(replace(event, text="/connect"))
        self.assertIn("Выбери мессенджер", response_text(first))
        options = {item.label: item for item in first.bound_actions}
        self.assertIn("Получать в Telegram", options)
        self.assertIn("Получать в VK", options)
        keyboard = _serialize_telegram_inline_keyboard(first, _load_aiogram())
        self.assertEqual({button.text for row in keyboard.inline_keyboard for button in row}, set(options))
        vk_event = IncomingEvent("vk", "22", "1", "/settings", "22")
        processor(vk_event)
        processor(replace(vk_event, text="/connect"))
        menu = CommandMenu(section_actions=processor.section_actions(vk_event))
        vk_keyboard = json.loads(_serialize_vk_command_menu(CommandMenuUpdate(vk_event.reply_address, menu)))
        labels = {button["action"]["label"] for row in vk_keyboard["buttons"] for button in row}
        self.assertTrue({"Получать в Telegram", "Получать в VK"} <= labels)
        processor(replace(vk_event, text="/exit"))
        self.assertIn("/notifications", response_text(processor(replace(event, text="/help"))))
        first = processor(replace(event, text="/connect"))
        choose_vk = next(item.token for item in first.bound_actions if item.label == "Получать в VK")
        result = processor.select_action(replace(event, message_id="2"), choose_vk)
        self.assertIn("Уведомления будут приходить в VK", response_text(result))
        self.assertIn("Получать в Telegram", [item.label for item in result.bound_actions])
        self.assertNotIn("Получать в VK", [item.label for item in result.bound_actions])
        second = IncomingEvent("vk", "22", "3", "/notifications telegram", "22")
        processor(second)
        self.assertEqual(service.resolve_without_touch(vk).notification_channel, "vk")
        processor(replace(second, text="/settings"))
        processor(replace(second, text="/connect"))
        result = processor(replace(second, text="Получать в Telegram", menu_choice=True))
        self.assertIn("Уведомления будут приходить в Telegram", response_text(result))
        group = replace(event, chat_id="-100")
        processor(group)
        self.assertIn("личный чат", response_text(processor(replace(group, text="/notifications vk"))))
        self.assertEqual(service.resolve_without_touch(vk).notification_channel, "telegram")

    def test_direct_and_confirmed_links_compete_for_one_target_channel(self):
        """После конкурирующей привязки подтверждение сохраняет владельца и второй код."""
        apply_migrations(self.db)
        service = create_user_identity_service(self.db)
        target = service.register_with_name(IncomingIdentity("telegram", "1", "1"), display_name="Цель")
        source = IncomingIdentity("vk", "2", "2")
        owner = service.register_with_name(source, display_name="Источник")
        first = service.create_link_code(target.id, target_channel="vk")
        second = service.create_link_code(target.id, target_channel="vk")
        with self.assertRaises(InvalidLinkCodeError):
            service.link(code=first.code, identity=IncomingIdentity("telegram", "9", "9"))
        pending = service.request_rebind_confirmation(code=first.code, identity=source)
        with connect_database(self.path) as other:
            create_user_identity_service(other).link(code=second.code, identity=IncomingIdentity("vk", "3", "3"))
        with self.assertRaises(InvalidLinkCodeError):
            service.confirm_rebind(source, confirmation_id=pending.id, version=pending.version)
        with self.assertRaises(IdentityAlreadyBoundError):
            service.create_link_code(target.id, target_channel="vk")
        self.assertEqual(service.resolve_without_touch(source), owner)
        self.assertEqual(service.linked_channels(target.id), {"telegram", "vk"})
        self.assertEqual(service.linked_channels(owner.id), {"vk"})
        self.assertIsNone(self.db.execute("SELECT used_at FROM identity_link_tokens WHERE token_hash = ?",
                                          ("vk:" + sha256(first.code.encode()).hexdigest(),)).fetchone()[0])
        # Прежний бинарник погашает код по bare digest из сохранённого подтверждения.
        self.assertEqual(self.db.execute("UPDATE identity_link_tokens SET used_at = CURRENT_TIMESTAMP "
                                         "WHERE token_hash = ? AND used_at IS NULL", (pending.token_hash,)).rowcount, 0)

    def test_code_expires_at_deadline_and_cannot_be_used_by_wrong_channel(self):
        """Проверяет обе операции token repository и отсутствие погашения при отказе."""
        apply_migrations(self.db)
        service = create_user_identity_service(self.db)
        owner = service.register_with_name(IncomingIdentity("telegram", "11", "-88"), display_name="Оля")
        tokens = SQLiteIdentityLinkTokenRepository(self.db)
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=10)
        tokens.create(app_user_id=owner.id, target_channel="vk", token_hash="test-hash", expires_at=deadline)
        self.assertEqual(tokens.find_active(token_hash="test-hash", channel="vk", now=now), owner.id)
        self.assertIsNone(tokens.find_active(token_hash="test-hash", channel="telegram", now=now))
        self.assertIsNone(tokens.find_active(token_hash="test-hash", channel="vk", now=deadline))
        self.assertEqual(service.linked_channels(owner.id), {"telegram"})
        self.assertIsNone(self.db.execute("SELECT used_at FROM identity_link_tokens").fetchone()[0])

    def test_registration_or_transfer_buttons_preserve_manual_recovery_in_both_directions(self):
        """Проводит ввод и адресное решение через настоящие публичные команды и токены."""
        apply_migrations(self.db)
        processor = ChannelMessageProcessor(database_path=self.path, capabilities=())
        service = create_user_identity_service(self.db)
        for index, (source, target) in enumerate((("telegram", "vk"), ("vk", "telegram")), 1):
            with self.subTest(target=target):
                source_id, target_id = str(10 + index), str(20 + index)
                origin = IncomingEvent(source, source_id, "1", "/settings", source_id)
                client = IncomingEvent(target, target_id, "2", "/start", target_id)
                owner = service.register_with_name(IncomingIdentity(source, source_id, source_id), display_name="Цель")
                processor(origin)
                issued = processor(replace(origin, text=f"/connect {target}"))
                code = re.search(r"Код привязки: ([\w-]+)", response_text(issued)).group(1)
                welcome = processor(client)
                submit = next(action for action in welcome.bound_actions if action.label == "У меня есть код")
                self.assertIn("следующим сообщением", response_text(processor.select_action(client, submit.token)))
                wrong = processor(replace(client, text="ошибка"))
                self.assertIn("Код привязки не принят", response_text(wrong))
                cancel = next(action for action in wrong.bound_actions if action.label == "Отмена")
                processor.select_action(client, cancel.token)
                self.assertEqual([a.label for a in processor.section_actions(client)], ["Создать профиль", "У меня есть код", "Помощь"])
                identity = IncomingIdentity(target, target_id, target_id)
                previous = service.register_with_name(identity, display_name="Источник")
                processor(replace(client, text="/settings"))
                screen = processor(replace(client, text="/connect"))
                name = "VK" if target == "vk" else "Telegram"
                transfer = next(a for a in screen.bound_actions if a.label == f"Привязать {name} к другому профилю")
                processor.select_action(client, transfer.token)
                request = processor(replace(client, text=code))
                self.assertEqual([a.label for a in request.actions], ["Перенести аккаунт", "Отменить перенос"])
                processor(replace(client, chat_id="-77", text=request.actions[0].command))
                processor(replace(client, text="да"))
                self.assertEqual(service.resolve_without_touch(identity), previous)
                processor(replace(client, text=request.actions[1].command))
                self.assertEqual(service.resolve_without_touch(identity), previous)
                second = processor(replace(client, text=f"/link {code}"))
                approve = next(a for a in second.bound_actions if a.label == "Перенести аккаунт")
                self.assertIn("перепривязал", response_text(processor.select_action(client, approve.token)))
                self.assertEqual(service.resolve_without_touch(identity), owner)
                self.assertIn("недоступна", response_text(processor.select_action(client, approve.token)))

    def test_upgrade_keeps_old_token_and_pending_confirmation(self):
        """Проверяет после обновления прежнюю выдачу и сохранённое перед ним подтверждение."""
        apply_migrations(self.db)
        expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        self.db.executemany("INSERT INTO app_users (id, display_name) VALUES (?, ?)", [(1, "Оля"), (2, "Ира")])
        self.db.executemany("INSERT INTO external_identities (app_user_id, channel, external_user_id, external_chat_id)"
                            " VALUES (?, 'telegram', ?, ?)", [(1, "1", "1"), (2, "2", "2")])
        for code in ("previous", "pending"):
            self.db.execute("INSERT INTO identity_link_tokens (app_user_id, token_hash, expires_at) VALUES (1, ?, ?)",
                            (sha256(code.encode()).hexdigest(), expires))
        self.db.execute("INSERT INTO identity_rebind_confirmations (channel, external_user_id, origin_chat_id, "
                        "source_app_user_id, token_hash, target_app_user_id, expires_at) "
                        "VALUES ('telegram', '2', '2', 2, ?, 1, ?)", (sha256(b"pending").hexdigest(), expires))
        self.db.commit()
        self.assertEqual(apply_migrations(self.db), [])
        service = create_user_identity_service(self.db)
        self.assertEqual(service.link(code="previous", identity=IncomingIdentity("vk", "3", "3")).id, 1)
        identity = IncomingIdentity("telegram", "2", "2")
        pending = service.get_pending_rebind(identity)
        self.assertEqual(service.confirm_rebind(identity, confirmation_id=pending.id, version=pending.version).id, 1)
        self.assertEqual(apply_migrations(self.db), [])
