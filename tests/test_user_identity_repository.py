"""Тесты SQLite-хранилища identity и сценария привязки каналов."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import sqlite3
import unittest

from compadre_assistant.core.domain.users.identity import (
    IdentityAlreadyBoundError, InvalidLinkCodeError, UserIdentityService,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.data.sqlite.user_identity_repository import (
    SQLiteAppUserRepository,
    SQLiteExternalIdentityRepository,
    SQLiteIdentityRebindConfirmationRepository,
    SQLiteIdentityLinkTokenRepository,
    SQLiteIdentityLifecycleRepository,
)
from compadre_assistant.core.domain.users.entities import IncomingIdentity


class SQLiteUserIdentityRepositoryTest(unittest.TestCase):
    """Проверяет регистрацию пользователей и привязку каналов."""

    def test_register_creates_user_and_resolves_same_identity(self) -> None:
        """Регистрация создает внутреннего пользователя и связь с каналом."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
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
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
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
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
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

    def test_register_rolls_back_profile_when_identity_insert_violates_unique(self) -> None:
        """Ошибка UNIQUE на второй вставке не оставляет профиль без identity."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                _install_identity_unique_conflict(connection)
                with self.assertRaises(sqlite3.IntegrityError):
                    _service(connection).register(_identity("vk", "vk-1", "chat-1"))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0], 0)

    def test_link_rolls_back_code_when_identity_insert_violates_unique(self) -> None:
        """Ошибка UNIQUE после погашения возвращает код в активное состояние."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                service = _service(connection)
                user = service.register(_identity("telegram", "tg-1", "chat-1"))
                code = service.create_link_code(user.id)
                _install_identity_unique_conflict(connection)
                with self.assertRaises(sqlite3.IntegrityError):
                    service.link(code=code.code, identity=_identity("vk", "vk-1", "chat-2"))
                self.assertIsNone(connection.execute(
                    "SELECT used_at FROM identity_link_tokens"
                ).fetchone()[0])
                connection.execute("DROP TRIGGER force_identity_unique_conflict")
                self.assertEqual(service.link(
                    code=code.code, identity=_identity("vk", "vk-1", "chat-2")
                ), user)

    def test_concurrent_register_returns_existing_profile_without_orphan(self) -> None:
        """Второе SQLite-соединение видит завершённую регистрацию первого."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as first:
                apply_migrations(first)
                started = Event()
                first.execute("BEGIN IMMEDIATE")
                user_id = first.execute("INSERT INTO app_users DEFAULT VALUES").lastrowid
                first.execute(
                    """INSERT INTO external_identities
                    (app_user_id, channel, external_user_id, external_chat_id)
                    VALUES (?, 'telegram', 'tg-1', 'chat-1')""",
                    (user_id,),
                )
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_register_on_connection, path, started)
                    self.assertTrue(started.wait(2))
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0.05)
                    first.commit()
                    returned = future.result(timeout=5)
                self.assertEqual(returned.id, user_id)
                self.assertEqual(first.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 1)
                self.assertEqual(first.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0], 1)

    def test_concurrent_identity_claim_preserves_unconsumed_code(self) -> None:
        """Параллельная привязка identity не погашает код второго соединения."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as first:
                apply_migrations(first)
                service = _service(first)
                target = service.register(_identity("telegram", "tg-1", "chat-1"))
                owner = service.register(_identity("vk", "vk-owner", "chat-2"))
                code = service.create_link_code(target.id)
                started = Event()
                first.execute("BEGIN IMMEDIATE")
                first.execute(
                    """INSERT INTO external_identities
                    (app_user_id, channel, external_user_id, external_chat_id)
                    VALUES (?, 'vk', 'vk-1', 'chat-3')""",
                    (owner.id,),
                )
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_link_on_connection, path, code.code, started)
                    self.assertTrue(started.wait(2))
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0.05)
                    first.commit()
                    with self.assertRaises(IdentityAlreadyBoundError):
                        future.result(timeout=5)
                self.assertIsNone(first.execute(
                    "SELECT used_at FROM identity_link_tokens"
                ).fetchone()[0])

    def test_two_connections_cannot_consume_same_code(self) -> None:
        """Два соединения дают одну новую связь на один одноразовый код."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as first:
                apply_migrations(first)
                service = _service(first)
                target = service.register(_identity("telegram", "tg-1", "chat-1"))
                code = service.create_link_code(target.id)
                started = Event()
                first.execute("BEGIN IMMEDIATE")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(
                        _link_on_connection, path, code.code, started, f"vk-{index}"
                    ) for index in (1, 2)]
                    self.assertTrue(started.wait(2))
                    for future in futures:
                        with self.assertRaises(TimeoutError):
                            future.result(timeout=0.05)
                    first.commit()
                    outcomes = []
                    for future in futures:
                        try:
                            outcomes.append(future.result(timeout=5))
                        except InvalidLinkCodeError:
                            outcomes.append(None)
                self.assertEqual(sum(result is not None for result in outcomes), 1)
                self.assertEqual(first.execute(
                    "SELECT COUNT(*) FROM external_identities WHERE app_user_id = ?", (target.id,)
                ).fetchone()[0], 2)


def _service(connection) -> UserIdentityService:
    """Создает identity service с предсказуемыми часами и кодом."""
    return UserIdentityService(
        app_user_repository=SQLiteAppUserRepository(connection),
        external_identity_repository=SQLiteExternalIdentityRepository(connection),
        link_token_repository=SQLiteIdentityLinkTokenRepository(connection),
        rebind_confirmation_repository=SQLiteIdentityRebindConfirmationRepository(
            connection
        ),
        lifecycle_repository=SQLiteIdentityLifecycleRepository(connection),
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


def _install_identity_unique_conflict(connection: sqlite3.Connection) -> None:
    """Заставляет вторую вставку одной identity нарушить реальный UNIQUE индекс."""
    connection.execute(
        """CREATE TRIGGER force_identity_unique_conflict BEFORE INSERT ON external_identities
        WHEN NEW.channel = 'vk' BEGIN
            INSERT INTO external_identities
            (app_user_id, channel, external_user_id, external_chat_id)
            VALUES (NEW.app_user_id, NEW.channel, NEW.external_user_id, NEW.external_chat_id);
        END"""
    )


def _register_on_connection(path: Path, started: Event):
    """Регистрирует пользователя в отдельном SQLite-соединении тестового worker."""
    with connect_database(path) as connection:
        started.set()
        return _service(connection).register(_identity("telegram", "tg-1", "chat-1"))


def _link_on_connection(path: Path, code: str, started: Event, user_id: str = "vk-1"):
    """Привязывает канал в отдельном SQLite-соединении тестового worker."""
    with connect_database(path) as connection:
        started.set()
        return _service(connection).link(
            code=code, identity=_identity("vk", user_id, "chat-3")
        )


if __name__ == "__main__":
    unittest.main()
