"""Тесты SQLite-хранилища identity и сценария привязки каналов."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import sqlite3
import unittest

from compadre_assistant.core.domain.users.identity import (
    IdentityAlreadyBoundError, InvalidDisplayNameError, InvalidLinkCodeError, UserIdentityService,
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


class SQLiteExternalIdentityRepositoryTest(unittest.TestCase):
    """Проверяет запись identity и проекции личных адресов на актуальной схеме."""

    def setUp(self) -> None:
        """Открывает мигрированную временную БД с двумя профилями."""
        directory = self.enterContext(TemporaryDirectory(prefix="external-identity-"))
        self.path = Path(directory) / "app.db"
        self.connection = self.enterContext(connect_database(self.path))
        apply_migrations(self.connection)
        users = SQLiteAppUserRepository(self.connection)
        self.owner = users.create(display_name="Первый")
        self.other = users.create(display_name="Другой")
        self.identities = SQLiteExternalIdentityRepository(self.connection)

    def test_touch_keeps_known_private_chat_after_group_and_detects_first_private_chat(self) -> None:
        """Для обоих каналов только настоящая личная активность создаёт личный адрес."""
        for channel, group_id in (("telegram", "-293"), ("vk", "2000000293")):
            with self.subTest(channel=channel):
                group = _identity(channel, "293", group_id)
                private = replace(group, external_chat_id=group.external_user_id)
                created = self.identities.create(app_user_id=self.owner.id, identity=group)
                self.assertEqual(self.identities.find(private), created)
                self.identities.touch(group)
                self.assertIsNone(self.identities.find_private_chat(channel, group_id))
                self.assertIsNone(self.identities.find_private_chat(channel, private.external_chat_id))
                self.assertTrue(self.identities.has_chat_address(channel, group_id))
                self.assertFalse(self.identities.has_chat_address(channel, private.external_chat_id))

                self.identities.touch(private)
                self.identities.touch(replace(group, username="latest", display_name="Групповое имя"))
                known = self.identities.find_private_chat(channel, private.external_chat_id)
                self.assertEqual(known.app_user_id, self.owner.id)
                self.assertEqual(known.username, "latest")
                self.assertEqual(known.display_name, "Групповое имя")
                self.assertEqual(known, self.identities.find(group))
                self.assertFalse(self.identities.has_chat_address(channel, group_id))
                self.assertIn(known, self.identities.list_for_user(self.owner.id))
                self.assertNotIn(known, self.identities.list_for_user(self.other.id))
                self.assertIn(known, self.identities.list_all_private_chats())
        self.assertEqual(self.identities.count_for_user(self.owner.id), 2)
        self.assertEqual(self.identities.count_for_user(self.other.id), 0)

    def test_create_and_reassign_constraints_preserve_original_identity(self) -> None:
        """Конфликты и ошибки FK откатываются, успешный перенос меняет владельца."""
        event = _identity("telegram", "293", "293")
        original = self.identities.create(app_user_id=self.owner.id, identity=event)
        with self.assertRaises(sqlite3.IntegrityError):
            self.identities.create(app_user_id=self.other.id, identity=event)
        with self.assertRaises(sqlite3.IntegrityError):
            self.identities.reassign(app_user_id=10**6, identity=event)
        self.assertEqual(self.identities.find(event), original)
        self.assertEqual(self.identities.count_for_user(self.other.id), 0)

        moved = self.identities.reassign(app_user_id=self.other.id, identity=event)
        self.assertEqual(moved.app_user_id, self.other.id)
        self.assertEqual(self.identities.find_private_chat("telegram", "293"), moved)
        self.assertEqual(self.identities.list_for_user(self.owner.id), ())
        self.assertEqual(self.identities.list_for_user(self.other.id), (moved,))

    def test_ambiguous_address_is_excluded_from_private_projections(self) -> None:
        """Наличие второй identity в том же адресе блокирует адресную доставку."""
        event = _identity("telegram", "293", "293")
        self.identities.create(app_user_id=self.owner.id, identity=event)
        self.identities.create(app_user_id=self.other.id, identity=replace(event, external_user_id="294"))
        self.assertTrue(self.identities.has_chat_address("telegram", "293"))
        self.assertIsNone(self.identities.find_private_chat("telegram", "293"))
        self.assertEqual(self.identities.list_for_user(self.owner.id), ())
        self.assertEqual(self.identities.list_all_private_chats(), ())

    def test_waiting_group_writes_keep_newly_committed_private_chat(self) -> None:
        """Групповое обновление и повторная регистрация не затирают свежую личку."""
        private = _identity("telegram", "293", "293")
        group = replace(private, external_chat_id="-293", username="late-group")
        self.identities.create(app_user_id=self.owner.id, identity=group)
        began = (Event(), Event())

        def group_operation(index: int) -> int | None:
            """Запускает независимую запись до фиксации личного адреса."""
            with connect_database(self.path) as connection:
                began[index].set()
                if index == 0:
                    SQLiteExternalIdentityRepository(connection).touch(group)
                    return None
                return SQLiteIdentityLifecycleRepository(connection).register_with_name(group, display_name="Группа").id

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "UPDATE external_identities SET external_chat_id = ? WHERE channel = ? AND external_user_id = ?",
                (private.external_chat_id, private.channel, private.external_user_id),
            )
            waiting = (pool.submit(group_operation, 0), pool.submit(group_operation, 1))
            try:
                self.assertTrue(all(start.wait(2) for start in began))
                for future in waiting:
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0.05)
            finally:
                self.connection.commit()
            self.assertIsNone(waiting[0].result(timeout=5))
            self.assertEqual(waiting[1].result(timeout=5), self.owner.id)
        known = self.identities.find_private_chat("telegram", "293")
        self.assertEqual(known.username, "late-group")
        self.assertEqual(known.app_user_id, self.owner.id)


class SQLiteUserIdentityRepositoryTest(unittest.TestCase):
    """Проверяет регистрацию пользователей и привязку каналов."""

    def test_register_creates_user_and_resolves_same_identity(self) -> None:
        """Регистрация создает внутреннего пользователя и связь с каналом."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                service = _service(connection)

                user = service.register_with_name(_identity("telegram", "tg-1", "chat-1"), display_name="Анатолий")
                named_user = service.update_display_name(
                    app_user_id=user.id,
                    display_name="  Влад   Ерофеев  ",
                )
                resolved = service.resolve(_identity("telegram", "tg-1", "chat-1"))

        self.assertEqual(user.display_name, "Анатолий")
        self.assertEqual(named_user.display_name, "Влад Ерофеев")
        self.assertEqual(resolved, named_user)
        self.assertEqual(user.id, 1)

    def test_invalid_registration_name_creates_no_records(self) -> None:
        """Недопустимые имена не создают ни профиль, ни внешнюю identity."""
        with TemporaryDirectory() as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                for name in (" ", "я" * 81, "/name", "https://example.com"):
                    with self.subTest(name=name), self.assertRaises(InvalidDisplayNameError):
                        _service(connection).register_with_name(
                            _identity("telegram", "tg-1", "chat-1"), display_name=name,
                        )
                    for table in ("app_users", "external_identities"):
                        self.assertEqual(connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0], 0)

    def test_failure_after_identity_insert_rolls_back_both_records(self) -> None:
        """Ошибка после обеих вставок откатывает всю транзакцию регистрации."""
        with TemporaryDirectory() as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                connection.execute("""CREATE TRIGGER reject_identity AFTER INSERT
                    ON external_identities BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
                with self.assertRaises(sqlite3.IntegrityError):
                    _service(connection).register_with_name(
                        _identity("telegram", "tg-1", "chat-1"), display_name="Имя",
                    )
                for table in ("app_users", "external_identities"):
                    self.assertEqual(connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0], 0)

    def test_link_code_binds_second_channel_to_same_user(self) -> None:
        """Одноразовый код привязывает новый канал к существующему пользователю."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                service = _service(connection)

                user = service.register_with_name(_identity("telegram", "tg-1", "chat-1"), display_name="Анатолий")
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

                old_user = service.register_with_name(_identity("telegram", "tg-1", "chat-1"), display_name="Анатолий")
                new_user = service.register_with_name(_identity("vk", "vk-1", "chat-2"), display_name="Анатолий")
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
                    _service(connection).register_with_name(_identity("vk", "vk-1", "chat-1"), display_name="Анатолий")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0], 0)

    def test_link_rolls_back_code_when_identity_insert_violates_unique(self) -> None:
        """Ошибка UNIQUE после погашения возвращает код в активное состояние."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            with connect_database(Path(directory) / "test.db") as connection:
                apply_migrations(connection)
                service = _service(connection)
                user = service.register_with_name(_identity("telegram", "tg-1", "chat-1"), display_name="Анатолий")
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
                user_id = first.execute("INSERT INTO app_users (display_name) VALUES ('Исходное имя')").lastrowid
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
                self.assertEqual(returned.display_name, "Исходное имя")
                self.assertEqual(first.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 1)
                self.assertEqual(first.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0], 1)

    def test_concurrent_identity_claim_preserves_unconsumed_code(self) -> None:
        """Параллельная привязка identity не погашает код второго соединения."""
        with TemporaryDirectory(prefix="compadre-assistant-identity-") as directory:
            path = Path(directory) / "test.db"
            with connect_database(path) as first:
                apply_migrations(first)
                service = _service(first)
                target = service.register_with_name(_identity("telegram", "tg-1", "chat-1"), display_name="Анатолий")
                owner = service.register_with_name(_identity("vk", "vk-owner", "chat-2"), display_name="Анатолий")
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
                target = service.register_with_name(_identity("telegram", "tg-1", "chat-1"), display_name="Анатолий")
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
        return _service(connection).register_with_name(_identity("telegram", "tg-1", "chat-1"), display_name="Анатолий")


def _link_on_connection(path: Path, code: str, started: Event, user_id: str = "vk-1"):
    """Привязывает канал в отдельном SQLite-соединении тестового worker."""
    with connect_database(path) as connection:
        started.set()
        return _service(connection).link(
            code=code, identity=_identity("vk", user_id, "chat-3")
        )


if __name__ == "__main__":
    unittest.main()
