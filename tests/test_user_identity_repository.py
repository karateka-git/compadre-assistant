"""Тесты SQLite-хранилища identity и сценария привязки каналов."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from obs_chat_bot.core.domain.users.identity import UserIdentityService
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from obs_chat_bot.core.data.sqlite.user_identity_repository import (
    SQLiteAppUserRepository,
    SQLiteExternalIdentityRepository,
    SQLiteIdentityRebindConfirmationRepository,
    SQLiteIdentityLinkTokenRepository,
)
from obs_chat_bot.core.domain.users.entities import IncomingIdentity


class SQLiteUserIdentityRepositoryTest(unittest.TestCase):
    """Проверяет регистрацию пользователей и привязку каналов."""

    def test_register_creates_user_and_resolves_same_identity(self) -> None:
        """Регистрация создает внутреннего пользователя и связь с каналом."""
        with TemporaryDirectory(prefix="obs-chat-bot-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                service = _service(connection)

                user = service.register(_identity("telegram", "tg-1", "chat-1"))
                named_user = service.update_display_name(
                    app_user_id=user.id,
                    display_name="  Влад   Ерофеев  ",
                )
                resolved = service.resolve(_identity("telegram", "tg-1", "chat-1"))

        self.assertIsNone(user.display_name)
        self.assertEqual(named_user.display_name, "Влад Ерофеев")
        self.assertEqual(resolved, named_user)
        self.assertEqual(user.id, 1)

    def test_link_code_binds_second_channel_to_same_user(self) -> None:
        """Одноразовый код привязывает новый канал к существующему пользователю."""
        with TemporaryDirectory(prefix="obs-chat-bot-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                service = _service(connection)

                user = service.register(_identity("telegram", "tg-1", "chat-1"))
                link_code = service.create_link_code(user.id)
                linked = service.link(
                    code=link_code.code,
                    identity=_identity("vk", "vk-1", "chat-2"),
                )
                resolved = service.resolve(_identity("vk", "vk-1", "chat-2"))

        self.assertEqual(linked, user)
        self.assertEqual(resolved, user)

    def test_confirm_rebind_moves_channel_and_deletes_empty_previous_user(self) -> None:
        """Подтверждение перепривязки переносит канал и удаляет пустого старого пользователя."""
        with TemporaryDirectory(prefix="obs-chat-bot-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                service = _service(connection)

                old_user = service.register(_identity("telegram", "tg-1", "chat-1"))
                new_user = service.register(_identity("vk", "vk-1", "chat-2"))
                link_code = service.create_link_code(new_user.id)
                target_user = service.request_rebind_confirmation(
                    code=link_code.code,
                    identity=_identity("telegram", "tg-1", "chat-1"),
                )
                rebound_user = service.confirm_rebind(
                    _identity("telegram", "tg-1", "chat-1"),
                    confirmation_id=target_user.id,
                    version=target_user.version,
                )
                resolved = service.resolve(_identity("telegram", "tg-1", "chat-1"))
                old_user_row = SQLiteAppUserRepository(connection).get_by_id(old_user.id)

        self.assertEqual(target_user.target_app_user_id, new_user.id)
        self.assertEqual(rebound_user, new_user)
        self.assertEqual(resolved, new_user)
        self.assertIsNone(old_user_row)


def _service(connection) -> UserIdentityService:
    """Создает identity service с предсказуемыми часами и кодом."""
    return UserIdentityService(
        app_user_repository=SQLiteAppUserRepository(connection),
        external_identity_repository=SQLiteExternalIdentityRepository(connection),
        link_token_repository=SQLiteIdentityLinkTokenRepository(connection),
        rebind_confirmation_repository=SQLiteIdentityRebindConfirmationRepository(
            connection
        ),
        clock=lambda: datetime(2026, 7, 17, tzinfo=UTC),
        token_factory=lambda: "ABC123",
        token_ttl=timedelta(minutes=10),
    )


def _identity(channel: str, user_id: str, chat_id: str) -> IncomingIdentity:
    """Создает внешнюю identity для тестов."""
    return IncomingIdentity(
        channel=channel,
        external_user_id=user_id,
        external_chat_id=chat_id,
        username=f"{channel}_{user_id}",
        display_name="Test User",
    )


if __name__ == "__main__":
    unittest.main()
