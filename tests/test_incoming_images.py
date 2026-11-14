"""Проверяет маршрут фото и изоляцию текстового ввода."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import Mock

from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey
from compadre_assistant.core.presentation.incoming.dispatcher import IncomingMessageDispatcher
from compadre_assistant.core.presentation.incoming.events import ImageAttachment, ImageLoadError, IncomingEvent
from compadre_assistant.core.presentation.incoming.pending_input import PendingInputStore
from compadre_assistant.core.presentation.incoming.ports import (
    IncomingResponse, PendingInputContinuationPort, PendingInputOutcome, PendingInputResult,
)
from compadre_assistant.core.presentation.incoming.registry import CapabilityRegistry
from compadre_assistant.core.presentation.message_content import text_content
from tests.capability_preference_helpers import MemoryCapabilityPreferences
from tests.response_assertions import response_text
from tests.test_incoming_boundary import FakeCapability, FakeIdentity, MemoryChatModes, _event


class ImageCapability(FakeCapability, PendingInputContinuationPort):
    """Подключаемый владелец ввода, принимающий одно фото."""

    def __init__(self, store: PendingInputStore) -> None:
        """Регистрирует тестовый обработчик с поддержкой вложений."""
        super().__init__("photos", "/photos")
        self._descriptor = replace(self.descriptor, accepts_images=True)
        self.store = store

    def continue_input(self, context, pending, event):
        """Возвращает загрузку как действие после принятия ожидания."""
        if len(event.images) != 1:
            return PendingInputResult(IncomingResponse(text_content("Нужно одно фото")), PendingInputOutcome.RETRY)

        def read():
            if self.store.get(event.reply_address) is not None:
                raise AssertionError("Ввод должен завершиться до загрузки")
            return IncomingResponse(text_content(event.images[0].read_bytes().decode()))

        return PendingInputResult(IncomingResponse(text_content("")), PendingInputOutcome.COMPLETE, read)


class IncomingImageTest(unittest.TestCase):
    """Проверяет общее событие без SDK и без сетевых вызовов."""

    def setUp(self) -> None:
        """Собирает публичный вход с независимыми владельцами маршрутов."""
        self.now = datetime(2026, 11, 14, tzinfo=UTC)
        self.store = PendingInputStore(lambda: self.now)
        self.identity = FakeIdentity()
        self.identity.register_with_name = Mock()
        self.text_capability = FakeCapability("obsidian", "/obsidian")
        self.image_capability = ImageCapability(self.store)
        self.modes = MemoryChatModes()
        self.key = ChatModeKey(self.identity.user.id, "telegram", "chat-1")
        self.preferences = MemoryCapabilityPreferences()
        self.dispatcher = IncomingMessageDispatcher(
            registry=CapabilityRegistry((self.text_capability, self.image_capability)),
            user_identity_service=self.identity, chat_mode_repository=self.modes,
            capability_preference_repository=self.preferences, pending_inputs=self.store,
        )
        self.loader = Mock(return_value=b"read")
        self.image = ImageAttachment(self.loader)
        self.address = _event("текст").reply_address

    def photo(self, caption: str = "", images: tuple[ImageAttachment, ...] | None = None) -> IncomingResponse:
        """Передаёт фото через общий публичный маршрут."""
        return self.dispatcher.execute(IncomingEvent(
            "telegram", "chat-1", "photo", caption,
            images=(self.image,) if images is None else images,
        ))

    def test_ignores_caption_without_switching_mode_or_delivering_subject_text(self) -> None:
        """Подпись не становится командой, ссылкой или подтверждением."""
        self.modes.save(ChatMode(self.key, "obsidian"))
        for caption in ("/photos", "/exit", "https://example.com/article", "да"):
            self.assertIn("Пришли текстовое сообщение", response_text(self.photo(caption)))
        self.assertEqual(self.modes.get(self.key).capability_id, "obsidian")
        self.assertEqual(self.text_capability.calls, [])
        self.assertEqual(self.image_capability.entries, [])
        self.loader.assert_not_called()
        self.assertEqual(response_text(self.dispatcher.execute(_event("текст"))), "Тестовый ответ")

    def test_only_admitted_modes_receive_photos_without_loading(self) -> None:
        """Фото без ожидания видит только доступная возможность с поддержкой фото."""
        self.assertIn("Пришли текстовое сообщение", response_text(self.photo()))
        self.modes.save(ChatMode(self.key, "photos"))
        self.assertEqual(response_text(self.photo("/exit")), "Тестовый ответ")
        delivered = self.image_capability.calls[0][1]
        self.assertEqual(delivered.text, "")
        self.assertEqual(delivered.images, (self.image,))
        self.preferences.rows[(self.identity.user.id, "photos")] = False
        self.assertIn("Пришли текстовое сообщение", response_text(self.photo()))
        self.assertEqual(len(self.image_capability.calls), 1)
        self.loader.assert_not_called()

    def test_photo_preserves_text_expectation_and_ignores_caption_as_registration_name(self) -> None:
        """Вложение не погашает ожидание обычного текста."""
        for owner, kind in (("core", "register_name"), ("photos", "ask_question")):
            pending = self.store.begin(self.address, owner, kind)
            self.assertIn("Пришли текстовое сообщение", response_text(self.photo("Имя")))
            self.assertEqual(self.store.get(self.address), pending)
        self.identity.register_with_name.assert_not_called()
        self.loader.assert_not_called()

    def test_expected_images_load_only_after_admission_and_consumption(self) -> None:
        """Владелец получает количество фото и загружает только принятое."""
        self.modes.save(ChatMode(self.key, "photos"))
        pending = self.store.begin(
            self.address, "photos", "photo", required_mode="photos", accepts_images=True,
        )
        self.assertEqual(response_text(self.photo(images=(self.image, self.image))), "Нужно одно фото")
        self.assertEqual(self.store.get(self.address), pending)
        self.loader.assert_not_called()
        other = IncomingEvent("telegram", "chat-2", "photo", "", images=(self.image,))
        self.assertIn("Пришли текстовое сообщение", response_text(self.dispatcher.execute(other)))
        self.assertEqual(response_text(self.photo("/exit")), "read")
        self.assertEqual(self.modes.get(self.key).capability_id, "photos")
        self.assertIsNone(self.store.get(self.address))
        self.loader.assert_called_once_with()

    def test_photo_does_not_load_after_expiry_mode_change_or_revocation(self) -> None:
        """Повторная проверка ждёт актуальный доступ, режим и срок."""
        for change, hint in (("expire", "истёк"), ("switch", "Контекст ввода изменился"),
                             ("revoke", "выключена")):
            with self.subTest(change=change):
                self.modes.save(ChatMode(self.key, "photos"))
                self.store.begin(self.address, "photos", "photo", required_mode="photos",
                                 retry_command="/take_photo", accepts_images=True)
                if change == "expire":
                    self.now += timedelta(minutes=10)
                elif change == "switch":
                    self.modes.save(ChatMode(self.key, "obsidian"))
                else:
                    self.preferences.rows[(self.identity.user.id, "photos")] = False
                self.assertIn(hint, response_text(self.photo("/exit")))
                self.assertIsNone(self.store.get(self.address))
        self.loader.assert_not_called()

    def test_empty_text_requires_attachment_and_loader_details_are_private(self) -> None:
        """Общий контракт скрывает подпись, функцию и ошибку транспорта."""
        with self.assertRaises(ValueError):
            IncomingEvent("telegram", "chat-1", "text", "")
        self.loader.side_effect = ValueError("secret file URL")
        event = IncomingEvent("telegram", "chat-1", "photo", "secret caption", images=(self.image,))
        self.assertNotIn("secret", repr(event))
        self.assertNotIn("Mock", repr(event))
        with self.assertRaisesRegex(ImageLoadError, "Не удалось получить изображение") as raised:
            self.image.read_bytes()
        self.assertIsNone(raised.exception.__context__)
