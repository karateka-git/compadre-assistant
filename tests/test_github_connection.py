"""Тесты фонового application-сценария GitHub Device Flow."""
from obs_chat_bot.capabilities.obsidian.domain.pending import PendingOrigin

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
import unittest

from obs_chat_bot.capabilities.obsidian.domain.vaults.github_connection import (
    GitHubConnectionCoordinator,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import (
    GitHubAuthenticatedAccount,
    GitHubConnectionCompletion,
    GitHubConnectionCompletionStatus,
    GitHubConnectionStartStatus,
    GitHubDeviceAuthorization,
    GitHubDevicePollResult,
    GitHubDevicePollStatus,
    GitHubUserAccessToken,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_connection_state_store import (
    SQLiteGitHubConnectionStateStore,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_installation_repository import (
    SQLiteGitHubInstallationRepository,
)
from obs_chat_bot.capabilities.obsidian.data.sqlite.github_installation_writer import (
    SQLiteGitHubAccountAccessWriter,
)
from obs_chat_bot.core.data.sqlite.connection import connect_database
from obs_chat_bot.core.data.sqlite.migration_runner import apply_migrations
from tests.sqlite_helpers import ensure_app_user


class FakeClock:
    """Управляет временем и ожиданием background worker без реального sleep."""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        """Продвигает тестовые часы на заданный interval."""
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakeGateway:
    """Возвращает предсказуемую последовательность Device Flow статусов."""

    def __init__(
        self,
        statuses: list[GitHubDevicePollStatus],
        *,
        installation_ids: set[int] | None = None,
    ) -> None:
        self.statuses = statuses
        self.installation_ids = (
            installation_ids if installation_ids is not None else {101, 102}
        )
        self.device_codes: list[str] = []
        self.tokens: list[GitHubUserAccessToken] = []
        self.polled = Event()

    def get_authenticated_account(
        self,
        access_token: GitHubUserAccessToken,
    ) -> GitHubAuthenticatedAccount:
        """Возвращает публичные данные тестового GitHub-аккаунта."""
        self.tokens.append(access_token)
        return GitHubAuthenticatedAccount(github_user_id=777, login="octocat")

    def request_device_authorization(self) -> GitHubDeviceAuthorization:
        """Возвращает тестовый challenge."""
        return GitHubDeviceAuthorization(
            device_code="device-secret",
            user_code="ABCD-EFGH",
            verification_uri="https://github.com/login/device",
            expires_in=900,
            interval=5,
        )

    def poll_device_token(self, device_code: str) -> GitHubDevicePollResult:
        """Возвращает следующий статус и запоминает device code."""
        self.device_codes.append(device_code)
        status = self.statuses.pop(0)
        self.polled.set()
        token = (
            GitHubUserAccessToken("ghu-secret")
            if status is GitHubDevicePollStatus.AUTHORIZED
            else None
        )
        return GitHubDevicePollResult(status=status, access_token=token)

    def list_installation_ids(
        self,
        access_token: GitHubUserAccessToken,
    ) -> set[int]:
        """Возвращает installations и запоминает временный user token."""
        self.tokens.append(access_token)
        return self.installation_ids


class FakeWriter:
    """Запоминает сохранённые installation IDs и сигнализирует о завершении."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, set[int]]] = []
        self.completed = Event()

    def replace_for_user(
        self,
        *,
        app_user_id: int,
        github_user_id: int,
        login: str,
        installation_ids: set[int],
    ) -> None:
        """Сохраняет только IDs без временного token."""
        self.calls.append((app_user_id, github_user_id, login, installation_ids))
        self.completed.set()


class FakeStateStore:
    """Хранит безопасное состояние подключения GitHub в памяти теста."""

    def __init__(self) -> None:
        self.attempt: tuple[int, str, datetime] | None = None

    def acquire_attempt(
        self,
        *,
        app_user_id: int,
        owner: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        """Захватывает попытку, если активной попытки ещё нет."""
        if self.has_active_attempt(app_user_id=app_user_id, now=now):
            return False
        self.attempt = (app_user_id, owner, expires_at)
        return True

    def has_active_attempt(self, *, app_user_id: int, now: datetime) -> bool:
        """Проверяет наличие неистёкшей попытки пользователя."""
        return bool(
            self.attempt is not None
            and self.attempt[0] == app_user_id
            and self.attempt[2] > now
        )

    def release_attempt(self, *, app_user_id: int, owner: str) -> None:
        """Освобождает попытку только её владельцу."""
        if self.attempt is not None and self.attempt[:2] == (app_user_id, owner):
            self.attempt = None


class BlockingGateway(FakeGateway):
    """Удерживает worker, чтобы проверить повторную команду пользователя."""

    def __init__(self) -> None:
        super().__init__([GitHubDevicePollStatus.DENIED])
        self.release_poll = Event()
        self.finished = Event()

    def poll_device_token(self, device_code: str) -> GitHubDevicePollResult:
        """Ожидает разрешения теста перед возвратом отказа."""
        self.polled.set()
        self.release_poll.wait(timeout=1)
        result = super().poll_device_token(device_code)
        self.finished.set()
        return result


class GitHubConnectionCoordinatorTest(unittest.TestCase):
    """Проверяет polling, slow_down и очистку in-memory session."""

    def test_start_returns_challenge_and_background_saves_only_installations(self) -> None:
        """Команда отвечает сразу, а worker завершает Device Flow в фоне."""
        clock = FakeClock()
        gateway = FakeGateway(
            [
                GitHubDevicePollStatus.PENDING,
                GitHubDevicePollStatus.SLOW_DOWN,
                GitHubDevicePollStatus.AUTHORIZED,
            ]
        )
        writer = FakeWriter()
        completions: list[GitHubConnectionCompletion] = []
        notification_sent = Event()

        def notify(completion: GitHubConnectionCompletion) -> None:
            completions.append(completion)
            notification_sent.set()

        coordinator = GitHubConnectionCoordinator(
            gateway=gateway,
            account_writer=writer,
            state_store=FakeStateStore(),
            installation_url="https://github.com/apps/obs-chat-bot/installations/new",
            clock=clock,
            sleeper=clock.sleep,
        )

        result = coordinator.start(42, PendingOrigin("telegram", "chat"), notify)
        self.assertTrue(writer.completed.wait(timeout=1))
        self.assertTrue(notification_sent.wait(timeout=1))

        self.assertEqual(result.status, GitHubConnectionStartStatus.STARTED)
        self.assertEqual(result.authorization.user_code, "ABCD-EFGH")
        self.assertEqual(clock.sleeps, [5.0, 5.0, 10.0])
        self.assertEqual(writer.calls, [(42, 777, "octocat", {101, 102})])
        self.assertEqual(gateway.tokens[0].value, "ghu-secret")
        self.assertEqual(
            completions,
            [
                GitHubConnectionCompletion(
                    GitHubConnectionCompletionStatus.CONNECTED,
                    installation_count=2,
                    account_login="octocat",
                )
            ],
        )

    def test_background_completion_uses_connection_after_message_closes(self) -> None:
        """Фоновый worker сохраняет installations после закрытия входного соединения."""
        with TemporaryDirectory(prefix="obs-chat-bot-github-worker-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
                ensure_app_user(connection)

            clock = FakeClock()
            completed = Event()
            completions: list[GitHubConnectionCompletion] = []

            def notify(completion: GitHubConnectionCompletion) -> None:
                completions.append(completion)
                completed.set()

            coordinator = GitHubConnectionCoordinator(
                gateway=FakeGateway([GitHubDevicePollStatus.AUTHORIZED]),
                account_writer=SQLiteGitHubAccountAccessWriter(database_path),
                state_store=SQLiteGitHubConnectionStateStore(database_path),
                installation_url="https://github.com/apps/obs-chat-bot/installations/new",
                clock=clock,
                sleeper=clock.sleep,
            )
            self.assertEqual(
                coordinator.start(1, PendingOrigin("telegram", "chat"), notify).status,
                GitHubConnectionStartStatus.STARTED,
            )
            self.assertTrue(completed.wait(timeout=1))
            with connect_database(database_path) as connection:
                installations = SQLiteGitHubInstallationRepository(connection).list_for_user(1)

            self.assertEqual(completions[0].status, GitHubConnectionCompletionStatus.CONNECTED)
            self.assertEqual(
                [item.installation_id for item in installations],
                [101, 102],
            )

    def test_denied_authorization_does_not_save_installations(self) -> None:
        """Отказ пользователя очищает session без записи в SQLite writer."""
        clock = FakeClock()
        writer = FakeWriter()
        gateway = FakeGateway([GitHubDevicePollStatus.DENIED])
        completions: list[GitHubConnectionCompletion] = []
        notification_sent = Event()

        def notify(completion: GitHubConnectionCompletion) -> None:
            completions.append(completion)
            notification_sent.set()

        coordinator = GitHubConnectionCoordinator(
            gateway=gateway,
            account_writer=writer,
            state_store=FakeStateStore(),
            installation_url="https://github.com/apps/obs-chat-bot/installations/new",
            clock=clock,
            sleeper=clock.sleep,
        )

        first = coordinator.start(42, PendingOrigin("telegram", "chat"), notify)
        self.assertTrue(notification_sent.wait(timeout=1))

        self.assertEqual(first.status, GitHubConnectionStartStatus.STARTED)
        self.assertEqual(writer.calls, [])
        self.assertEqual(
            completions,
            [GitHubConnectionCompletion(GitHubConnectionCompletionStatus.DENIED)],
        )

    def test_authorized_without_installations_notifies_user(self) -> None:
        """Успешный OAuth без installation получает отдельный понятный итог."""
        clock = FakeClock()
        gateway = FakeGateway(
            [GitHubDevicePollStatus.AUTHORIZED],
            installation_ids=set(),
        )
        writer = FakeWriter()
        completions: list[GitHubConnectionCompletion] = []
        notification_sent = Event()

        def notify(completion: GitHubConnectionCompletion) -> None:
            completions.append(completion)
            notification_sent.set()

        coordinator = GitHubConnectionCoordinator(
            gateway=gateway,
            account_writer=writer,
            state_store=FakeStateStore(),
            installation_url="https://github.com/apps/obs-chat-bot/installations/new",
            clock=clock,
            sleeper=clock.sleep,
        )

        coordinator.start(42, PendingOrigin("telegram", "chat"), notify)
        self.assertTrue(notification_sent.wait(timeout=1))

        self.assertEqual(writer.calls, [(42, 777, "octocat", set())])
        self.assertEqual(
            completions,
            [GitHubConnectionCompletion(GitHubConnectionCompletionStatus.NO_INSTALLATIONS)],
        )

    def test_expired_authorization_notifies_user(self) -> None:
        """Истёкший Device Flow code получает итоговый ответ для повтора."""
        clock = FakeClock()
        gateway = FakeGateway([GitHubDevicePollStatus.EXPIRED])
        completions: list[GitHubConnectionCompletion] = []
        notification_sent = Event()

        def notify(completion: GitHubConnectionCompletion) -> None:
            completions.append(completion)
            notification_sent.set()

        coordinator = GitHubConnectionCoordinator(
            gateway=gateway,
            account_writer=FakeWriter(),
            state_store=FakeStateStore(),
            installation_url="https://github.com/apps/obs-chat-bot/installations/new",
            clock=clock,
            sleeper=clock.sleep,
        )

        coordinator.start(42, PendingOrigin("telegram", "chat"), notify)
        self.assertTrue(notification_sent.wait(timeout=1))

        self.assertEqual(
            completions,
            [GitHubConnectionCompletion(GitHubConnectionCompletionStatus.EXPIRED)],
        )

    def test_repeated_start_returns_same_pending_challenge(self) -> None:
        """Повторная команда не создаёт второй Device Flow для пользователя."""
        gateway = BlockingGateway()
        coordinator = GitHubConnectionCoordinator(
            gateway=gateway,
            account_writer=FakeWriter(),
            state_store=FakeStateStore(),
            installation_url="https://github.com/apps/obs-chat-bot/installations/new",
            sleeper=lambda _seconds: None,
        )

        self.assertIsNone(coordinator.get_pending(42, PendingOrigin("telegram", "chat")))
        first = coordinator.start(42, PendingOrigin("telegram", "chat"))
        self.assertTrue(gateway.polled.wait(timeout=1))
        observed = coordinator.get_pending(42, PendingOrigin("telegram", "chat"))
        self.assertEqual(observed.authorization, first.authorization)
        foreign = coordinator.get_pending(42, PendingOrigin("vk", "other-chat"))
        self.assertEqual(foreign.status, GitHubConnectionStartStatus.IN_PROGRESS)
        self.assertIsNone(foreign.authorization)
        other_chat = coordinator.start(42, PendingOrigin("telegram", "other-chat"))
        self.assertEqual(other_chat.status, GitHubConnectionStartStatus.IN_PROGRESS)
        self.assertIsNone(other_chat.authorization)
        repeated = coordinator.start(42, PendingOrigin("telegram", "chat"))
        gateway.release_poll.set()
        self.assertTrue(gateway.finished.wait(timeout=1))

        self.assertEqual(first.status, GitHubConnectionStartStatus.STARTED)
        self.assertEqual(
            repeated.status,
            GitHubConnectionStartStatus.ALREADY_PENDING,
        )
        self.assertEqual(repeated.authorization, first.authorization)

    def test_shared_attempt_blocks_second_coordinator(self) -> None:
        """Общий claim не позволяет Telegram и VK начать два Device Flow одновременно."""
        state_store = FakeStateStore()
        gateway = BlockingGateway()
        first = GitHubConnectionCoordinator(
            gateway=gateway,
            account_writer=FakeWriter(),
            state_store=state_store,
            installation_url="https://github.com/apps/obs-chat-bot/installations/new",
            sleeper=lambda _seconds: None,
        )
        second = GitHubConnectionCoordinator(
            gateway=FakeGateway([GitHubDevicePollStatus.DENIED]),
            account_writer=FakeWriter(),
            state_store=state_store,
            installation_url="https://github.com/apps/obs-chat-bot/installations/new",
            sleeper=lambda _seconds: None,
        )

        first.start(42, PendingOrigin("telegram", "chat"))
        self.assertTrue(gateway.polled.wait(timeout=1))
        observed = second.get_pending(42, PendingOrigin("telegram", "chat"))
        self.assertEqual(observed.status, GitHubConnectionStartStatus.IN_PROGRESS)
        blocked = second.start(42, PendingOrigin("telegram", "chat"))
        gateway.release_poll.set()
        self.assertTrue(gateway.finished.wait(timeout=1))

        self.assertEqual(blocked.status, GitHubConnectionStartStatus.IN_PROGRESS)

    def test_restarted_coordinator_starts_after_orphan_claim_expires(self) -> None:
        """После утраты процесса новый coordinator ждёт TTL и начинает flow."""
        with TemporaryDirectory(prefix="obs-chat-bot-github-retry-") as directory:
            database_path = Path(directory) / "test.db"
            with connect_database(database_path) as connection:
                apply_migrations(connection)
                ensure_app_user(connection)

            clock = FakeClock()
            state_store = SQLiteGitHubConnectionStateStore(database_path)
            self.assertTrue(
                state_store.acquire_attempt(
                    app_user_id=1,
                    owner="lost-process",
                    now=clock.now,
                    expires_at=clock.now + timedelta(minutes=20),
                )
            )
            gateway = FakeGateway([GitHubDevicePollStatus.DENIED])
            completion_sent = Event()
            restarted = GitHubConnectionCoordinator(
                gateway=gateway,
                account_writer=SQLiteGitHubAccountAccessWriter(database_path),
                state_store=SQLiteGitHubConnectionStateStore(database_path),
                installation_url="https://github.com/apps/obs-chat-bot/installations/new",
                clock=clock,
                sleeper=clock.sleep,
            )
            self.assertEqual(
                restarted.start(1, PendingOrigin("telegram", "chat")).status,
                GitHubConnectionStartStatus.IN_PROGRESS,
            )
            clock.now += timedelta(minutes=20)
            self.assertEqual(
                restarted.start(
                    1, PendingOrigin("telegram", "chat"), lambda _completion: completion_sent.set()
                ).status,
                GitHubConnectionStartStatus.STARTED,
            )
            self.assertTrue(completion_sent.wait(timeout=1))
            self.assertEqual(gateway.device_codes, ["device-secret"])


if __name__ == "__main__":
    unittest.main()
