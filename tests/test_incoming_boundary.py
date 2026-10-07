"""Проверки общих контрактов и реестра без предметных зависимостей."""

from dataclasses import fields
import unittest

from obs_chat_bot.bootstrap import create_incoming_message_dispatcher
from obs_chat_bot.core.domain.chat_modes import ChatMode, ChatModeKey, ChatModeRepository
from obs_chat_bot.core.domain.users.entities import AppUser
from obs_chat_bot.core.presentation.incoming.events import IncomingEvent
from obs_chat_bot.core.presentation.incoming.ports import (
    CapabilityDescriptor, CapabilityMode, CapabilityResult, HandlerContext,
    IncomingCapabilityPort, IncomingResponse,
)
from obs_chat_bot.core.presentation.incoming.registry import CapabilityRegistry


class MemoryChatModes(ChatModeRepository):
    """Сохраняет полные ключи режимов в памяти для автономных тестов."""

    def __init__(self) -> None:
        self.modes: dict[ChatModeKey, ChatMode] = {}

    def get(self, key: ChatModeKey) -> ChatMode | None:
        """Возвращает режим указанного чата."""
        return self.modes.get(key)

    def save(self, mode: ChatMode) -> None:
        """Меняет режим указанного чата."""
        self.modes[mode.key] = mode

    def activate_if_absent(self, mode: ChatMode) -> ChatMode:
        """Атомарно сохраняет первый выбор в тестовом хранилище."""
        return self.modes.setdefault(mode.key, mode)

    def clear(self, key: ChatModeKey) -> None:
        """Удаляет только адресованный режим."""
        self.modes.pop(key, None)


class FakeCapability(IncomingCapabilityPort):
    """Регистрируется без изменений core и адаптеров каналов."""

    def __init__(self, identifier: str = "selected", command: str = "/selected") -> None:
        self._descriptor = CapabilityDescriptor(identifier, command, "тестовая возможность")
        self.calls: list[tuple[AppUser, IncomingEvent]] = []
        self.entries: list[tuple[AppUser, IncomingEvent]] = []
        self.entry_mode = CapabilityMode.ACTIVE
        self.handle_mode = CapabilityMode.ACTIVE
        self.entry_error: Exception | None = None
        self.entry_exception: Exception | None = None
        self.pending = "Актуальное состояние"
        self.delivery = None
        self.actions = ()

    @property
    def descriptor(self) -> CapabilityDescriptor:
        """Объявляет тестовую точку входа."""
        return self._descriptor

    def enter(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> CapabilityResult:
        """Фиксирует вход и сообщает выбранную семантику режима."""
        self.entries.append((context.app_user, event))
        self.entry_arguments = arguments
        if self.entry_exception:
            raise self.entry_exception
        return CapabilityResult(IncomingResponse(self.pending, self.entry_error), self.entry_mode)

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Фиксирует исходное событие и независимую доставку."""
        self.calls.append((context.app_user, event))
        self.delivery = context.delivery
        return CapabilityResult(IncomingResponse("Тестовый ответ", actions=self.actions), self.handle_mode)


class FakeIdentity:
    """Предоставляет идентифицированного пользователя и ожидающий rebind."""

    def __init__(self) -> None:
        self.user = AppUser(id=23, display_name="Тест")
        self.pending = False

    def resolve(self, _identity: object) -> AppUser:
        """Возвращает идентифицированного пользователя."""
        return self.user

    def has_pending_rebind(self, _identity: object) -> bool:
        """Сообщает о перепривязке общего канала."""
        return self.pending

    def confirm_rebind(self, _identity: object) -> AppUser:
        """Завершает подтверждённую перепривязку."""
        self.pending = False
        return self.user

    def cancel_rebind(self, _identity: object) -> None:
        """Отменяет ожидающую перепривязку."""
        self.pending = False


def _event(text: str, chat: str = "chat-1", channel: str = "telegram") -> IncomingEvent:
    """Создаёт нейтральное событие для проверок маршрутизации."""
    return IncomingEvent(channel=channel, chat_id=chat, message_id="msg-1", text=text)


class IncomingBoundaryTest(unittest.TestCase):
    """Проверяет публичный вход и семантику режимов любого канала."""

    def setUp(self) -> None:
        self.identity = FakeIdentity()
        self.capability = FakeCapability()
        self.other = FakeCapability("other", "/other")
        self.repository = MemoryChatModes()
        self.dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability, self.other),
            user_identity_service=self.identity,
            chat_mode_repository=self.repository,
        )
        self.key = ChatModeKey(23, "telegram", "chat-1")

    def test_event_and_response_have_no_subject_storage_fields(self) -> None:
        """Общие контракты не растут вместе с предметными моделями."""
        for model in (IncomingEvent, IncomingResponse, CapabilityResult):
            names = {field.name for field in fields(model)}
            self.assertNotIn("article_id", names)
            self.assertNotIn("vault", names)
        self.assertNotIn("app_user_id", {field.name for field in fields(IncomingEvent)})

    def test_help_lists_every_entry_and_exit_without_calling_handlers(self) -> None:
        """Справка всех входов доступна до активного режима и pending rebind."""
        self.identity.pending = True
        reply = self.dispatcher.execute(_event("/help"))
        for command in ("/selected", "/other", "/exit", "/status", "/name"):
            self.assertIn(command, reply.text)
        self.assertFalse(self.capability.calls)
        self.assertFalse(self.capability.entries)

    def test_plain_text_and_url_without_mode_only_show_hint(self) -> None:
        """Содержание сообщения никогда не выбирает возможность автоматически."""
        for text in ("обычный текст", "https://example.com/article", "да", "/unknown"):
            reply = self.dispatcher.execute(_event(text))
            self.assertIn("Выбери возможность", reply.text)
        self.assertFalse(self.capability.calls)
        self.assertIsNone(self.repository.get(self.key))

    def test_entry_forwards_original_event_and_resolved_user(self) -> None:
        """Вход получает исходное событие с аргументом без анализа ядром."""
        event = _event("/selected произвольный аргумент")
        reply = self.dispatcher.execute(event)
        self.assertEqual(reply.text, self.capability.pending)
        self.assertEqual(self.capability.entries, [(self.identity.user, event)])
        self.assertEqual(self.capability.entry_arguments, "произвольный аргумент")
        self.assertEqual(self.repository.get(self.key).capability_id, "selected")
        incoming = _event("https://example.com/article")
        self.dispatcher.execute(incoming)
        self.assertEqual(self.capability.calls, [(self.identity.user, incoming)])

    def test_exit_then_text_keeps_mode_empty_and_pending_data(self) -> None:
        """Выход очищает маршрут; предметное состояние остаётся в возможности."""
        self.dispatcher.execute(_event("/selected"))
        self.dispatcher.execute(_event("/exit"))
        self.assertIsNone(self.repository.get(self.key))
        self.dispatcher.execute(_event("https://example.com/article"))
        self.assertFalse(self.capability.calls)
        self.capability.pending = "Ожидающее действие сохранено"
        self.assertEqual(self.dispatcher.execute(_event("/selected")).text, self.capability.pending)

    def test_switch_and_exit_are_isolated_by_chat_channel_and_user(self) -> None:
        """Переключение не затрагивает соседний чат, канал или пользователя."""
        self.dispatcher.execute(_event("/selected"))
        self.dispatcher.execute(_event("/selected", chat="other-chat"))
        self.dispatcher.execute(_event("/selected", channel="vk"))
        self.repository.save(ChatMode(ChatModeKey(99, "telegram", "chat-1"), "selected"))
        self.dispatcher.execute(_event("/other"))
        self.assertEqual(self.repository.get(self.key).capability_id, "other")
        self.dispatcher.execute(_event("/exit"))
        for key in (ChatModeKey(23, "telegram", "other-chat"), ChatModeKey(23, "vk", "chat-1"), ChatModeKey(99, "telegram", "chat-1")):
            self.assertEqual(self.repository.get(key).capability_id, "selected")

    def test_one_shot_and_failed_entries_preserve_previous_mode(self) -> None:
        """Завершённый, ошибочный и выбросивший исключение входы сохраняют режим."""
        self.dispatcher.execute(_event("/selected"))
        self.other.entry_mode = CapabilityMode.COMPLETE
        self.dispatcher.execute(_event("/other"))
        self.assertEqual(self.repository.get(self.key).capability_id, "selected")
        self.other.entry_mode = CapabilityMode.ACTIVE
        self.other.entry_error = ValueError("Сбой")
        self.dispatcher.execute(_event("/other"))
        self.assertEqual(self.repository.get(self.key).capability_id, "selected")
        self.other.entry_error = None
        self.other.entry_exception = RuntimeError("Сбой")
        with self.assertLogs(level="ERROR"):
            reply = self.dispatcher.execute(_event("/other"))
        self.assertIsInstance(reply.error, RuntimeError)
        self.assertEqual(self.repository.get(self.key).capability_id, "selected")
        self.dispatcher.execute(_event("/exit"))
        self.other.entry_exception = None
        self.other.entry_mode = CapabilityMode.COMPLETE
        self.dispatcher.execute(_event("/other"))
        self.assertIsNone(self.repository.get(self.key))

    def test_completed_handle_clears_only_its_mode(self) -> None:
        """Логическое завершение handle возвращает чат к выбору входа."""
        self.dispatcher.execute(_event("/selected"))
        self.capability.handle_mode = CapabilityMode.COMPLETE
        self.dispatcher.execute(_event("завершить"))
        self.assertIsNone(self.repository.get(self.key))

    def test_general_commands_and_entry_precede_pending_rebind(self) -> None:
        """Общие команды доступны во время прежнего текстового rebind."""
        self.identity.pending = True
        self.assertIn("Профиль", self.dispatcher.execute(_event("/status")).text)
        self.dispatcher.execute(_event("/selected"))
        self.assertTrue(self.capability.entries)
        self.dispatcher.execute(_event("/exit"))
        self.assertIsNone(self.repository.get(self.key))
        self.assertTrue(self.identity.pending)
        self.dispatcher.execute(_event("да"))
        self.assertFalse(self.identity.pending)
        self.assertFalse(self.capability.calls)

    def test_disabled_id_is_preserved_until_explicit_exit_or_switch(self) -> None:
        """Отключённый сохранённый маршрут не заменяется по обычному сообщению."""
        mode = ChatMode(self.key, "disabled")
        self.repository.save(mode)
        reply = self.dispatcher.execute(_event("https://example.com/article"))
        self.assertIn("отключена", reply.text)
        self.assertIn("/exit", reply.text)
        self.assertIn("/other", reply.text)
        self.assertEqual(self.repository.get(self.key), mode)
        self.assertFalse(self.capability.calls)
        self.dispatcher.execute(_event("/other"))
        self.assertEqual(self.repository.get(self.key).capability_id, "other")

    def test_background_response_keeps_address_after_exit(self) -> None:
        """Доставка сохраняет адрес и работает после выхода из режима."""
        delivered = []
        self.dispatcher.execute(_event("/selected"))
        event = _event("начать")
        self.dispatcher.execute(event, delivered.append)
        delivery = self.capability.delivery
        self.dispatcher.execute(_event("/exit"))
        delivery.send(event.reply_address, IncomingResponse("Готово"))
        self.assertEqual(delivered[0].address, event.reply_address)
        self.assertEqual(delivered[0].response.text, "Готово")
        self.assertIsNone(self.repository.get(self.key))

    def test_fake_entry_and_actions_work_in_both_channel_adapters(self) -> None:
        """Новая возможность проходит через настоящие адаптеры с общими контрактами."""
        import asyncio
        import logging
        from obs_chat_bot.core.presentation.incoming.ports import ConfirmationAction
        from obs_chat_bot.presentation.telegram.bot import _register_handlers
        from obs_chat_bot.presentation.vk.bot import _handle_update
        from test_telegram_bot import FakeAiogram, TelegramMessage
        from test_vk_bot import FakeVkClient

        self.capability.actions = (ConfirmationAction("test-action", "Выполнить", "/selected_confirm token"),)
        self.capability.handle_mode = CapabilityMode.COMPLETE
        aiogram = FakeAiogram()
        message = TelegramMessage()
        message.message_id = 1
        message.from_user = None
        _register_handlers(
            aiogram.dispatcher, aiogram,
            incoming_message_processor=self.dispatcher.execute,
            logger=logging.getLogger("test.fake.telegram"),
        )
        for text in ("/selected аргумент Telegram", "действие"):
            message.text = text
            asyncio.run(aiogram.dispatcher.included_router.handler(message))
        self.assertIn("/selected_confirm token", message.answers[-1])
        self.assertIsNone(self.repository.get(ChatModeKey(23, "telegram", "42")))
        self.assertEqual(self.capability.entry_arguments, "аргумент Telegram")
        client = FakeVkClient()
        for text in ("/selected аргумент VK", "действие"):
            _handle_update(
                {"type": "message_new", "object": {"message": {"id": 1, "peer_id": 42, "from_id": 1, "text": text}}},
                incoming_message_processor=self.dispatcher.execute,
                client=client,
                logger=logging.getLogger("test.fake.vk"),
            )
        self.assertIn("/selected_confirm token", client.messages[-1][1])
        self.assertIsNone(self.repository.get(ChatModeKey(23, "vk", "42")))
        self.assertEqual(self.capability.entry_arguments, "аргумент VK")
        self.assertEqual([event.channel for _, event in self.capability.entries], ["telegram", "vk"])
        self.assertEqual([event.channel for _, event in self.capability.calls], ["telegram", "vk"])

    def test_disabled_entry_metadata_is_generic_and_preserves_mode(self) -> None:
        """Метаданные любого отключённого входа не запускают выбранный обработчик."""
        disabled = CapabilityDescriptor("unavailable", "/unavailable", "отключённая возможность")
        dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability,), disabled_capabilities=(disabled,),
            user_identity_service=self.identity, chat_mode_repository=self.repository,
        )
        dispatcher.execute(_event("/selected"))
        reply = dispatcher.execute(_event("/unavailable https://example.com/article"))
        self.assertIn("`unavailable` отключена", reply.text)
        self.assertEqual(self.repository.get(self.key).capability_id, "selected")
        self.assertFalse(self.capability.calls)
        self.assertNotIn("/unavailable", dispatcher.execute(_event("/help")).text)

    def test_registry_checks_disabled_metadata_conflicts(self) -> None:
        """Отключённые входы подчиняются общим ограничениям реестра."""
        for descriptor in (
            CapabilityDescriptor("selected", "/different", "описание"),
            CapabilityDescriptor("different", "/selected", "описание"),
            CapabilityDescriptor("different", "/help", "описание"),
            CapabilityDescriptor("different", "invalid", "описание"),
            CapabilityDescriptor(" ", "/different", "описание"),
            CapabilityDescriptor("different", "/different", " "),
        ):
            with self.subTest(descriptor=descriptor), self.assertRaises(ValueError):
                CapabilityRegistry((self.capability,), (descriptor,))

    def test_registry_rejects_id_command_and_reserved_conflicts(self) -> None:
        """Ошибки запуска называют конфликтующий ID или команду."""
        for capabilities, expected in (
            ((FakeCapability("same", "/one"), FakeCapability("same", "/two")), "same"),
            ((FakeCapability("one", "/same"), FakeCapability("two", "/same")), "/same"),
            ((FakeCapability("one", "/help"),), "/help"),
            ((FakeCapability("one", "/exit"),), "/exit"),
            ((FakeCapability("one", "/status"),), "/status"),
            ((FakeCapability("one", "/name"),), "/name"),
            ((FakeCapability("one", "/register"),), "/register"),
            ((FakeCapability("one", "/link"),), "/link"),
        ):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                CapabilityRegistry(capabilities)

    def test_empty_registry_only_offers_general_help(self) -> None:
        """Отсутствие включённых возможностей не требует предметного обработчика."""
        dispatcher = create_incoming_message_dispatcher(capabilities=(), user_identity_service=self.identity, chat_mode_repository=self.repository)
        self.assertIn("нет включённых", dispatcher.execute(_event("текст")).text)
        self.assertIn("/exit", dispatcher.execute(_event("/help")).text)


if __name__ == "__main__":
    unittest.main()
