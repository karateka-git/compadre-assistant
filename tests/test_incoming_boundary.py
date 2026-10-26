from compadre_assistant.core.presentation.message_content import text_content
from tests.response_assertions import content_text, response_text
"""Проверки общих контрактов и реестра без предметных зависимостей."""

from dataclasses import fields
import unittest
from tests.capability_preference_helpers import MemoryCapabilityPreferences

from compadre_assistant.bootstrap import create_incoming_message_dispatcher
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey, ChatModeRepository, ChatModeState
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand,
    CapabilityDescriptor, CapabilityMode, CapabilityResult, HandlerContext,
    IncomingCapabilityPort, IncomingResponse,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry


class MemoryChatModes(ChatModeRepository):
    """Сохраняет полные ключи режимов в памяти для автономных тестов."""

    def __init__(self) -> None:
        self.modes: dict[ChatModeKey, ChatMode] = {}
        self.revisions: dict[ChatModeKey, int] = {}

    def get(self, key: ChatModeKey) -> ChatMode | None:
        """Возвращает режим указанного чата."""
        return self.modes.get(key)

    def get_state(self, key: ChatModeKey) -> ChatModeState:
        """Возвращает состояние, включая ревизию очищенного выбора."""
        mode = self.modes.get(key)
        return ChatModeState(key, mode.capability_id if mode else None, self.revisions.get(key, 0))

    def replace_if_revision(
        self, key: ChatModeKey, expected_revision: int, capability_id: str | None
    ) -> bool:
        """Меняет выбор только при совпадении сохранённой ревизии."""
        if self.revisions.get(key, 0) != expected_revision:
            return False
        if capability_id is None:
            self.modes.pop(key, None)
        else:
            self.modes[key] = ChatMode(key, capability_id)
        self.revisions[key] = expected_revision + 1
        return True

    def save(self, mode: ChatMode) -> None:
        """Меняет режим указанного чата."""
        self.modes[mode.key] = mode
        self.revisions[mode.key] = self.revisions.get(mode.key, 0) + 1

    def activate_if_absent(self, mode: ChatMode) -> ChatMode:
        """Атомарно сохраняет первый выбор в тестовом хранилище."""
        if mode.key not in self.modes:
            self.save(mode)
        return self.modes[mode.key]

    def clear(self, key: ChatModeKey) -> None:
        """Очищает только адресованный режим и увеличивает его ревизию."""
        self.modes.pop(key, None)
        self.revisions[key] = self.revisions.get(key, 0) + 1


class FakeCapability(IncomingCapabilityPort):
    """Регистрируется без изменений core и адаптеров каналов."""

    def __init__(self, identifier: str = "selected", command: str = "/selected") -> None:
        self._descriptor = CapabilityDescriptor(identifier, CapabilityCommand(command, (), "тестовая возможность"), enabled_by_default=True)
        self.calls: list[tuple[AppUser, IncomingEvent]] = []
        self.entries: list[tuple[AppUser, IncomingEvent]] = []
        self.entry_mode = CapabilityMode.ACTIVE
        self.handle_mode = CapabilityMode.ACTIVE
        self.entry_error: Exception | None = None
        self.entry_exception: Exception | None = None
        self.handle_error: Exception | None = None
        self.handle_exception: Exception | None = None
        self.background_reply: IncomingResponse | None = None
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
        return CapabilityResult(IncomingResponse(text_content(self.pending), self.entry_error), self.entry_mode)

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Фиксирует исходное событие и независимую доставку."""
        self.calls.append((context.app_user, event))
        self.delivery = context.delivery
        if self.background_reply is not None and context.delivery is not None:
            context.delivery.send(event.reply_address, self.background_reply)
        if self.handle_exception is not None:
            raise self.handle_exception
        return CapabilityResult(
            IncomingResponse(text_content("Тестовый ответ"), self.handle_error, actions=self.actions),
            self.handle_mode,
        )


class FakeIdentity:
    """Предоставляет идентифицированного пользователя и ожидающий rebind."""

    def __init__(self) -> None:
        self.user = AppUser(id=23, display_name="Тест")
        self.pending = False

    def resolve(self, _identity: object) -> AppUser:
        """Возвращает идентифицированного пользователя."""
        return self.user


    def confirm_rebind(self, _identity: object, **_decision) -> AppUser:
        """Завершает подтверждённую перепривязку."""
        self.pending = False
        return self.user

    def cancel_rebind(self, _identity: object, **_decision) -> None:
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
        self.preferences = MemoryCapabilityPreferences()
        self.dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability, self.other),
            user_identity_service=self.identity,
            chat_mode_repository=self.repository,
            capability_preference_repository=self.preferences,
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
            self.assertIn(command, response_text(reply))
        self.assertFalse(self.capability.calls)
        self.assertFalse(self.capability.entries)

    def test_unregistered_help_has_only_common_commands(self) -> None:
        """Неизвестная identity не получает персональные предметные разделы."""
        self.identity.user = None
        reply = self.dispatcher.execute(_event("/help"))
        self.assertIn("/register", response_text(reply))
        self.assertNotIn("/selected", response_text(reply))
        self.assertEqual(self.preferences.reads, 0)

    def test_capabilities_catalog_handles_default_override_and_incomplete_profile(self) -> None:
        """Каталог доступен до имени и показывает личный и серверный статусы."""
        self.identity.user = AppUser(id=23, display_name=None)
        disabled = CapabilityDescriptor("offline", CapabilityCommand("/offline", (), "офлайн"), enabled_by_default=True)
        dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability,),
            disabled_capabilities=(disabled,),
            user_identity_service=self.identity,
            chat_mode_repository=self.repository,
            capability_preference_repository=self.preferences,
        )
        first = response_text(dispatcher.execute(_event("/capabilities")))
        self.assertIn("selected", first)
        self.assertIn("Личный выбор: включена; сервер: доступна", first)
        self.assertIn("offline", first)
        self.assertIn("Личный выбор: включена; сервер: недоступна", first)
        self.assertIsNone(self.identity.user.display_name)
        self.preferences.rows[(23, "selected")] = False
        self.assertIn("Личный выбор: выключена", response_text(dispatcher.execute(_event("/capabilities"))))
        self.assertEqual(self.preferences.reads, 2)

    def test_capabilities_catalog_validates_arguments_and_registration(self) -> None:
        """Лишний аргумент не читает настройки; незнакомый канал просит регистрацию."""
        before = self.preferences.reads
        self.assertIn("без аргументов", response_text(self.dispatcher.execute(_event("/capabilities extra"))))
        self.assertEqual(self.preferences.reads, before)
        self.identity.user = None
        self.assertIn("/register", response_text(self.dispatcher.execute(_event("/capabilities"))))
        self.assertEqual(self.preferences.reads, before)

    def test_empty_capabilities_catalog_keeps_common_commands(self) -> None:
        """Пустой серверный каталог возвращает понятный список без записи имени."""
        dispatcher = create_incoming_message_dispatcher(
            capabilities=(),
            user_identity_service=self.identity,
            chat_mode_repository=self.repository,
            capability_preference_repository=self.preferences,
        )
        reply = dispatcher.execute(_event("/capabilities"))
        self.assertIn("нет известных возможностей", response_text(reply))
        self.assertIn("/enable <id>", response_text(reply))

    def test_enable_validates_exact_id_and_server_availability(self) -> None:
        """Некорректный ID и отключённый сервером вход не создают настроек."""
        disabled = CapabilityDescriptor("offline", CapabilityCommand("/offline", (), "офлайн"))
        dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability,), disabled_capabilities=(disabled,),
            user_identity_service=self.identity, chat_mode_repository=self.repository,
            capability_preference_repository=self.preferences,
        )
        for text in ("/enable", "/enable Selected", "/enable selected extra", "/enable unknown"):
            reply = dispatcher.execute(_event(text))
            self.assertIn("/capabilities", response_text(reply))
            self.assertEqual(self.preferences.rows, {})
        self.assertIn("недоступна на сервере", response_text(dispatcher.execute(_event("/enable offline"))))
        self.assertEqual(self.preferences.rows, {})
        self.identity.user = None
        self.assertIn("/register", response_text(dispatcher.execute(_event("/enable selected"))))

    def test_enable_is_idempotent_and_reports_write_failure(self) -> None:
        """Повторный выбор сообщает «уже», а сбой записи не подтверждает включение."""
        self.identity.user = AppUser(id=23, display_name=None)
        first = self.dispatcher.execute(_event("/enable selected"))
        self.assertIn("уже включена", response_text(first))
        self.assertTrue(self.preferences.rows[(23, "selected")])
        self.preferences.rows[(23, "selected")] = False
        self.assertIn("включена", response_text(self.dispatcher.execute(_event("/enable selected"))))
        self.assertIn("уже включена", response_text(self.dispatcher.execute(_event("/enable selected"))))

        class FailingPreferences(MemoryCapabilityPreferences):
            """Имитирует отказ хранилища при записи."""

            def upsert(self, preference):
                """Сообщает о невозможности зафиксировать изменение."""
                raise OSError("storage failed")

        dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability,), user_identity_service=self.identity,
            chat_mode_repository=self.repository,
            capability_preference_repository=FailingPreferences(),
        )
        with self.assertLogs(level="ERROR"):
            failed = dispatcher.execute(_event("/enable selected"))
        self.assertIn("Не удалось сохранить", response_text(failed))
        self.assertNotIn("Возможность selected включена", response_text(failed))

    def test_disable_preserves_mode_data_and_background_delivery(self) -> None:
        """Выключение блокирует новые события, сохраняя маршрут и фоновый ответ."""
        self.dispatcher.execute(_event("/selected"))
        delivered = []
        event = _event("начать")
        self.dispatcher.execute(event, delivered.append)
        delivery = self.capability.delivery
        before = self.repository.get_state(self.key)
        handled = len(self.capability.calls)
        reply = self.dispatcher.execute(_event("/disable selected"))
        self.assertIn("выключена", response_text(reply))
        self.assertEqual(self.repository.get_state(self.key), before)
        self.assertIn("/enable selected", response_text(self.dispatcher.execute(_event("/status"))))
        self.assertNotIn("`/selected` —", response_text(self.dispatcher.execute(_event("/help"))))
        self.assertIn("/enable selected", response_text(self.dispatcher.execute(_event("текст"))))
        self.assertIn("/enable selected", response_text(self.dispatcher.execute(
            _event("/confirm selected action 1 1 yes")
        )))
        self.assertEqual(len(self.capability.calls), handled)
        delivery.send(event.reply_address, IncomingResponse(text_content("Готово")))
        self.assertEqual(response_text(delivered[-1].response), "Готово")
        self.assertIn("уже выключена", response_text(self.dispatcher.execute(_event("/disable selected"))))
        self.dispatcher.execute(_event("/disable other"))
        self.assertIn("нет включённых", response_text(self.dispatcher.execute(_event("/start"))))
        self.assertIn("/capabilities", response_text(self.dispatcher.execute(_event("/exit"))))
        self.assertIsNone(self.repository.get(self.key))
        self.assertIn("включена", response_text(self.dispatcher.execute(_event("/enable selected"))))
        self.assertEqual(response_text(self.dispatcher.execute(_event("/selected"))), self.capability.pending)

    def test_disable_validates_id_and_accepts_server_disabled_entry(self) -> None:
        """Глобальное отключение не мешает личному выключению известного ID."""
        self.identity.user = AppUser(id=23, display_name=None)
        disabled = CapabilityDescriptor("offline", CapabilityCommand("/offline", (), "офлайн"))
        dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability,), disabled_capabilities=(disabled,),
            user_identity_service=self.identity, chat_mode_repository=self.repository,
            capability_preference_repository=self.preferences,
        )
        for text in ("/disable", "/disable Offline", "/disable offline extra", "/disable unknown"):
            self.assertIn("/capabilities", response_text(dispatcher.execute(_event(text))))
            self.assertEqual(self.preferences.rows, {})
        self.preferences.rows[(23, "offline")] = True
        self.assertIn("выключена", response_text(dispatcher.execute(_event("/disable offline"))))
        self.assertFalse(self.preferences.rows[(23, "offline")])
        self.assertIn("уже выключена", response_text(dispatcher.execute(_event("/disable offline"))))
        self.identity.user = None
        self.assertIn("/register", response_text(dispatcher.execute(_event("/disable selected"))))

    def test_plain_text_and_url_without_mode_only_show_hint(self) -> None:
        """Содержание сообщения никогда не выбирает возможность автоматически."""
        for text in ("обычный текст", "https://example.com/article", "да", "/unknown"):
            reply = self.dispatcher.execute(_event(text))
            self.assertIn("Выбери возможность", response_text(reply))
        self.assertFalse(self.capability.calls)
        self.assertIsNone(self.repository.get(self.key))

    def test_personal_guard_reads_once_and_blocks_direct_entry(self) -> None:
        """Выключенная fake-возможность не получает вход и не меняет режим."""
        self.preferences.rows[(23, "selected")] = False
        reply = self.dispatcher.execute(_event("/selected аргумент"))
        self.assertIn("/enable selected", response_text(reply))
        self.assertEqual(self.preferences.reads, 1)
        self.assertEqual(self.capability.entries, [])
        self.assertIsNone(self.repository.get(self.key))

    def test_saved_mode_and_subject_confirmation_share_one_guard(self) -> None:
        """Текст и адресный confirm не входят в лично выключенный режим."""
        self.repository.save(ChatMode(self.key, "selected"))
        revision = self.repository.get_state(self.key).revision
        self.preferences.rows[(23, "selected")] = False
        for text in ("обычный текст", "/confirm selected action 1 1 yes"):
            before = self.preferences.reads
            reply = self.dispatcher.execute(_event(text))
            self.assertIn("/enable selected", response_text(reply))
            self.assertEqual(self.preferences.reads, before + 1)
        self.assertEqual(self.capability.calls, [])
        self.assertEqual(self.repository.get_state(self.key).revision, revision)
        self.preferences.rows[(23, "selected")] = True
        self.dispatcher.execute(_event("обычный текст"))
        self.assertEqual(len(self.capability.calls), 1)

    def test_general_responses_use_one_personal_snapshot(self) -> None:
        """Справка, старт, статус и выход не предлагают лично выключенные входы."""
        self.preferences.rows[(23, "selected")] = False
        self.repository.save(ChatMode(self.key, "selected"))
        for text in ("/help", "/start", "/status", "/exit", "обычный текст"):
            before = self.preferences.reads
            reply = self.dispatcher.execute(_event(text))
            self.assertEqual(self.preferences.reads, before + 1)
            self.assertNotIn("`/selected` —", response_text(reply))
            if text == "/status":
                self.assertIn("Режим этого чата: selected", response_text(reply))
                self.assertIn("/enable selected", response_text(reply))
            if text == "/exit":
                self.assertIsNone(self.repository.get(self.key))
        self.assertIn("/other", response_text(reply))

    def test_entry_forwards_original_event_and_resolved_user(self) -> None:
        """Вход получает исходное событие с аргументом без анализа ядром."""
        event = _event("/selected произвольный аргумент")
        reply = self.dispatcher.execute(event)
        self.assertEqual(response_text(reply), self.capability.pending)
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
        self.assertEqual(response_text(self.dispatcher.execute(_event("/selected"))), self.capability.pending)

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

    def test_failed_complete_preserves_mode(self) -> None:
        """Ошибка результата handle не завершает выбранный режим."""
        self.dispatcher.execute(_event("/selected"))
        self.capability.handle_mode = CapabilityMode.COMPLETE
        self.capability.handle_error = RuntimeError("Сбой")
        response = self.dispatcher.execute(_event("завершить"))
        self.assertIsInstance(response.error, RuntimeError)
        self.assertEqual(self.repository.get(self.key).capability_id, "selected")

    def test_handle_exception_preserves_mode_and_does_not_repeat_effect(self) -> None:
        """Сбой после фоновой доставки оставляет маршрут и не повторяет действие."""
        self.dispatcher.execute(_event("/selected"))
        before = self.repository.get_state(self.key)
        failure = RuntimeError("секретная причина")
        self.capability.handle_mode = CapabilityMode.COMPLETE
        self.capability.handle_exception = failure
        self.capability.background_reply = IncomingResponse(text_content("Действие принято"))
        delivered = []

        with self.assertLogs("compadre_assistant.core.presentation.incoming.dispatcher", level="ERROR") as logs:
            response = self.dispatcher.execute(_event("подтвердить"), delivered.append)

        self.assertEqual(response.error, failure)
        self.assertIn("Проверь состояние действия", response_text(response))
        self.assertNotIn("секретная причина", response_text(response))
        self.assertEqual(response.actions, ())
        self.assertEqual(self.repository.get_state(self.key), before)
        self.assertEqual(len(self.capability.calls), 1)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(response_text(delivered[0].response), "Действие принято")
        self.assertIn("capability_id=selected", "\n".join(logs.output))

    def test_handle_exception_has_same_safe_reply_in_both_adapters(self) -> None:
        """TG и VK показывают одинаковый безопасный ответ подменной возможности."""
        import asyncio
        import logging
        from compadre_assistant.presentation.telegram.bot import _register_handlers
        from compadre_assistant.presentation.vk.bot import _handle_update
        from tests.test_telegram_bot import FakeAiogram, TelegramMessage
        from tests.test_vk_bot import FakeVkClient

        self.capability.handle_mode = CapabilityMode.COMPLETE
        self.capability.handle_exception = RuntimeError("секретная причина")
        aiogram = FakeAiogram()
        message = TelegramMessage()
        message.message_id = 1
        message.from_user = None
        _register_handlers(
            aiogram.dispatcher, aiogram,
            incoming_message_processor=self.dispatcher.execute,
            logger=logging.getLogger("test.fake.telegram"),
        )
        with self.assertLogs(level="ERROR"):
            for text in ("/selected", "внешнее действие"):
                message.text = text
                asyncio.run(aiogram.dispatcher.included_router.handler(message))

        client = FakeVkClient()
        with self.assertLogs(level="ERROR"):
            for text in ("/selected", "внешнее действие"):
                _handle_update(
                    {"type": "message_new", "object": {"message": {
                        "id": 1, "peer_id": 42, "from_id": 1, "text": text,
                    }}},
                    incoming_message_processor=self.dispatcher.execute,
                    client=client,
                    logger=logging.getLogger("test.fake.vk"),
                )

        telegram_reply = message.answers[-1]
        vk_reply = client.messages[-1][1]
        self.assertEqual(telegram_reply, vk_reply)
        self.assertIn("Проверь состояние действия", telegram_reply)
        self.assertNotIn("секретная причина", telegram_reply)
        self.assertEqual(len(self.capability.calls), 2)
        for channel in ("telegram", "vk"):
            key = ChatModeKey(23, channel, "42")
            self.assertEqual(self.repository.get(key).capability_id, "selected")
            self.assertEqual(self.repository.get_state(key).revision, 1)

    def test_general_commands_and_entry_precede_pending_rebind(self) -> None:
        """Общие команды доступны во время прежнего текстового rebind."""
        self.identity.pending = True
        self.assertIn("Профиль", response_text(self.dispatcher.execute(_event("/status"))))
        self.dispatcher.execute(_event("/selected"))
        self.assertTrue(self.capability.entries)
        self.dispatcher.execute(_event("/exit"))
        self.assertIsNone(self.repository.get(self.key))
        self.assertTrue(self.identity.pending)
        self.dispatcher.execute(_event("да"))
        self.assertTrue(self.identity.pending)
        self.dispatcher.execute(_event("/confirm core identity 1 1 yes"))
        self.assertFalse(self.identity.pending)
        self.assertFalse(self.capability.calls)

    def test_disabled_id_is_preserved_until_explicit_exit_or_switch(self) -> None:
        """Отключённый сохранённый маршрут не заменяется по обычному сообщению."""
        mode = ChatMode(self.key, "disabled")
        self.repository.save(mode)
        reply = self.dispatcher.execute(_event("https://example.com/article"))
        self.assertIn("отключена", response_text(reply))
        self.assertIn("/exit", response_text(reply))
        self.assertIn("/other", response_text(reply))
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
        delivery.send(event.reply_address, IncomingResponse(text_content("Готово")))
        self.assertEqual(delivered[0].address, event.reply_address)
        self.assertEqual(response_text(delivered[0].response), "Готово")
        self.assertIsNone(self.repository.get(self.key))

    def test_fake_entry_and_actions_work_in_both_channel_adapters(self) -> None:
        """Новая возможность проходит через настоящие адаптеры с общими контрактами."""
        import asyncio
        import logging
        from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction
        from compadre_assistant.presentation.telegram.bot import _register_handlers
        from compadre_assistant.presentation.vk.bot import _handle_update
        from tests.test_telegram_bot import FakeAiogram, TelegramMessage
        from tests.test_vk_bot import FakeVkClient

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
        disabled = CapabilityDescriptor("unavailable", CapabilityCommand("/unavailable", (), "отключённая возможность"))
        dispatcher = create_incoming_message_dispatcher(
            capabilities=(self.capability,), disabled_capabilities=(disabled,),
            user_identity_service=self.identity, chat_mode_repository=self.repository,
            capability_preference_repository=self.preferences,
        )
        dispatcher.execute(_event("/selected"))
        reply = dispatcher.execute(_event("/unavailable https://example.com/article"))
        self.assertIn("unavailable недоступна на сервере", response_text(reply))
        self.assertEqual(self.repository.get(self.key).capability_id, "selected")
        self.assertFalse(self.capability.calls)
        self.assertNotIn("/unavailable", response_text(dispatcher.execute(_event("/help"))))

    def test_registry_checks_disabled_metadata_conflicts(self) -> None:
        """Отключённые входы подчиняются общим ограничениям реестра."""
        for descriptor in (
            CapabilityDescriptor("selected", CapabilityCommand("/different", (), "описание")),
            CapabilityDescriptor("different", CapabilityCommand("/selected", (), "описание")),
            CapabilityDescriptor("different", CapabilityCommand("/help", (), "описание")),
            CapabilityDescriptor("different", CapabilityCommand("invalid", (), "описание")),
            CapabilityDescriptor(" ", CapabilityCommand("/different", (), "описание")),
            CapabilityDescriptor("different", CapabilityCommand("/different", (), " ")),
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
        dispatcher = create_incoming_message_dispatcher(capabilities=(), user_identity_service=self.identity, chat_mode_repository=self.repository, capability_preference_repository=self.preferences)
        self.assertIn("нет включённых", response_text(dispatcher.execute(_event("текст"))))
        self.assertIn("/exit", response_text(dispatcher.execute(_event("/help"))))


if __name__ == "__main__":
    unittest.main()
