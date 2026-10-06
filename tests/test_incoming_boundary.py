"""Проверки границы общего входа и подключаемой возможности."""

from dataclasses import fields
import unittest

from obs_chat_bot.application.incoming.events import IncomingEvent
from obs_chat_bot.application.incoming.ports import CapabilityHelp, IncomingCapabilityPort, OnboardingReplyKind, OnboardingState
from obs_chat_bot.application.incoming.processing import (
    IncomingMessageResultType,
    ProcessIncomingMessageResult,
    ProcessIncomingMessageUseCase,
)
from obs_chat_bot.domain.users.entities import AppUser


class FakeCapability(IncomingCapabilityPort):
    """Фиксирует предметные вызовы без моделей и repositories Obsidian."""

    def __init__(self) -> None:
        self.calls: list[tuple[AppUser, IncomingEvent]] = []

    def onboarding_state(self, _app_user: AppUser) -> OnboardingState:
        """Возвращает готовность тестовой возможности."""
        return OnboardingState(ready=True, summary="Готово")

    def onboarding_reply(self, _kind: OnboardingReplyKind, _app_user: AppUser) -> str:
        """Возвращает нейтральное тестовое описание готовности."""
        return "Готово"

    def help(self) -> CapabilityHelp:
        """Возвращает пустое расширение общей справки."""
        return CapabilityHelp()

    def confirmation_missing_reply(self) -> str:
        """Возвращает нейтральную подсказку при пустом подтверждении."""
        return "Нет действия."

    def status(self, app_user: AppUser) -> ProcessIncomingMessageResult:
        """Возвращает состояние тестовой возможности."""
        return ProcessIncomingMessageResult(
            type=IncomingMessageResultType.ONBOARDING_REQUIRED, app_user=app_user
        )

    def handle(
        self,
        app_user: AppUser,
        event: IncomingEvent,
        _completion_handler: object | None = None,
    ) -> ProcessIncomingMessageResult:
        """Запоминает передачу уже идентифицированного события."""
        self.calls.append((app_user, event))
        return ProcessIncomingMessageResult(
            type=IncomingMessageResultType.ONBOARDING_REQUIRED,
            app_user=app_user,
        )


class FakeIdentity:
    """Предоставляет общего пользователя и ожидающий rebind для теста."""

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


def _event(text: str) -> IncomingEvent:
    """Создаёт нейтральное событие Telegram для проверок."""
    return IncomingEvent(
        channel="telegram", chat_id="chat-1", message_id="msg-1", text=text
    )


class IncomingBoundaryTest(unittest.TestCase):
    """Проверяет маршрутизацию до входа конкретной возможности."""

    def test_event_has_no_user_or_article_storage_fields(self) -> None:
        """Адрес канала не превращается в запись article journal."""
        field_names = {field.name for field in fields(IncomingEvent)}
        self.assertNotIn("app_user_id", field_names)
        self.assertNotIn("article_id", field_names)

    def test_general_help_stays_before_capability(self) -> None:
        """Общая справка не вызывает предметный обработчик."""
        capability = FakeCapability()
        use_case = ProcessIncomingMessageUseCase(
            incoming_capability=capability, user_identity_service=FakeIdentity()
        )

        result = use_case.execute(_event("/help"))

        self.assertEqual(result.type, IncomingMessageResultType.HELP)
        self.assertEqual(capability.calls, [])

    def test_rebind_confirmation_precedes_capability_confirmation(self) -> None:
        """Ответ «да» сначала подтверждает общую перепривязку."""
        capability = FakeCapability()
        identity = FakeIdentity()
        identity.pending = True
        use_case = ProcessIncomingMessageUseCase(
            incoming_capability=capability, user_identity_service=identity
        )

        result = use_case.execute(_event("да"))

        self.assertEqual(result.type, IncomingMessageResultType.LINK_REBOUND)
        self.assertFalse(identity.pending)
        self.assertEqual(capability.calls, [])

    def test_plain_text_reaches_capability_with_resolved_user(self) -> None:
        """Возможность получает пользователя и неизменённое событие канала."""
        capability = FakeCapability()
        identity = FakeIdentity()
        use_case = ProcessIncomingMessageUseCase(
            incoming_capability=capability, user_identity_service=identity
        )
        event = _event("произвольный текст")

        use_case.execute(event)

        self.assertEqual(capability.calls, [(identity.user, event)])


if __name__ == "__main__":
    unittest.main()
