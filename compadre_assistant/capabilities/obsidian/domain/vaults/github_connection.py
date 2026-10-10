from __future__ import annotations

from collections.abc import Callable

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from threading import Lock, Thread
import time
from uuid import uuid4

from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import (
    GitHubConnectionCompletion,
    GitHubConnectionCompletionStatus,
    GitHubConnectionStartResult,
    GitHubConnectionStartStatus,
    GitHubDeviceAuthorization,
    GitHubDevicePollStatus,
    GitHubGatewayError,
    GitHubUserAccessToken,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.ports import (
    GitHubConnectionCompletionHandler,
    GitHubAccountAccessWriter,
    GitHubConnectionStateStore,
    GitHubConnectionStarter,
    GitHubDeviceFlowGateway,
)


LOGGER = logging.getLogger(__name__)
CONNECTION_ATTEMPT_TTL = timedelta(minutes=20)


@dataclass(frozen=True, slots=True)
class _GitHubConnectionSession:
    """Хранит in-memory challenge и callback исходного чата."""

    authorization: GitHubDeviceAuthorization | None
    completion_handler: GitHubConnectionCompletionHandler | None
    owner: str
    origin: PendingOrigin


class GitHubConnectionCoordinator(GitHubConnectionStarter):
    """Координирует один GitHub-аккаунт и Device Flow между adapters."""

    def __init__(
        self,
        *,
        gateway: GitHubDeviceFlowGateway,
        account_writer: GitHubAccountAccessWriter,
        state_store: GitHubConnectionStateStore,
        installation_url: str,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        thread_factory: Callable[..., Thread] | None = None,
    ) -> None:
        if not installation_url.strip():
            raise ValueError("installation_url must not be empty")
        self._gateway = gateway
        self._account_writer = account_writer
        self._state_store = state_store
        self._installation_url = installation_url
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or time.sleep
        self._thread_factory = thread_factory or Thread
        self._sessions: dict[int, _GitHubConnectionSession] = {}
        self._lock = Lock()

    @property
    def installation_url(self) -> str:
        """Возвращает страницу установки и настройки GitHub App."""
        return self._installation_url

    def start(
        self,
        app_user_id: int,
        origin: PendingOrigin,
        completion_handler: GitHubConnectionCompletionHandler | None = None,
    ) -> GitHubConnectionStartResult:
        """Возвращает challenge исходному чату и запускает polling в daemon thread.

        Args:
            app_user_id: Внутренний пользователь, владеющий общим Device Flow claim.
            origin: Адрес чата, начавшего авторизацию.
            completion_handler: Доставка результата в тот же исходный чат.

        Returns:
            Challenge только исходного чата либо безопасный признак занятости.

        Raises:
            ValueError: Внутренний ID пользователя некорректен.
            GitHubGatewayError: Device Flow не удалось начать.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        pending = self.get_pending(app_user_id, origin)
        if pending is not None:
            return pending

        return self._start_device_flow(app_user_id, origin, completion_handler)

    def get_pending(
        self,
        app_user_id: int,
        origin: PendingOrigin,
    ) -> GitHubConnectionStartResult | None:
        """Читает ожидающую авторизацию текущего или соседнего процесса.

        Args:
            app_user_id: Внутренний пользователь приложения.
            origin: Чат текущего события; чужой challenge не возвращается.

        Returns:
            Текущий challenge или признак незавершённой shared attempt без запуска API.
        """
        with self._lock:
            if app_user_id in self._sessions:
                if self._sessions[app_user_id].origin != origin:
                    return GitHubConnectionStartResult(
                        GitHubConnectionStartStatus.IN_PROGRESS, self._installation_url
                    )
                authorization = self._sessions[app_user_id].authorization
                status = (
                    GitHubConnectionStartStatus.ALREADY_PENDING
                    if authorization is not None
                    else GitHubConnectionStartStatus.PREPARING
                )
                return GitHubConnectionStartResult(
                    status=status,
                    installation_url=self._installation_url,
                    authorization=authorization,
                )
        if self._state_store.has_active_attempt(app_user_id=app_user_id, now=self._clock()):
            return GitHubConnectionStartResult(
                status=GitHubConnectionStartStatus.IN_PROGRESS,
                installation_url=self._installation_url,
            )
        return None

    def _start_device_flow(
        self,
        app_user_id: int,
        origin: PendingOrigin,
        completion_handler: GitHubConnectionCompletionHandler | None,
    ) -> GitHubConnectionStartResult:
        """Захватывает shared claim и запускает Device Flow в текущем процессе."""
        owner = uuid4().hex
        now = self._clock()
        acquired = self._state_store.acquire_attempt(
            app_user_id=app_user_id,
            owner=owner,
            expires_at=now + CONNECTION_ATTEMPT_TTL,
            now=now,
        )
        if not acquired:
            return GitHubConnectionStartResult(
                status=GitHubConnectionStartStatus.IN_PROGRESS,
                installation_url=self._installation_url,
            )
        with self._lock:
            session = _GitHubConnectionSession(
                authorization=None,
                completion_handler=completion_handler,
                owner=owner,
                origin=origin,
            )
            self._sessions[app_user_id] = session

        authorization: GitHubDeviceAuthorization | None = None
        try:
            authorization = self._gateway.request_device_authorization()
            session = _GitHubConnectionSession(
                authorization=authorization,
                completion_handler=completion_handler,
                owner=owner,
                origin=origin,
            )
            with self._lock:
                self._sessions[app_user_id] = session
            worker = self._thread_factory(
                target=self._poll_until_complete,
                args=(app_user_id, session),
                name=f"github-device-flow-{app_user_id}",
                daemon=True,
            )
            worker.start()
        except Exception as error:
            self._remove_session(app_user_id, expected=session)
            self._state_store.release_attempt(app_user_id=app_user_id, owner=owner)
            if isinstance(error, GitHubGatewayError):
                raise
            raise GitHubGatewayError(
                f"GitHub Device Flow could not start: {type(error).__name__}"
            ) from error

        return GitHubConnectionStartResult(
            status=GitHubConnectionStartStatus.STARTED,
            installation_url=self._installation_url,
            authorization=authorization,
        )

    def _poll_until_complete(
        self,
        app_user_id: int,
        session: _GitHubConnectionSession,
    ) -> None:
        authorization = session.authorization
        if authorization is None:
            raise RuntimeError("Background GitHub session has no authorization")
        deadline = self._clock() + timedelta(seconds=authorization.expires_in)
        interval = float(authorization.interval)
        completion: GitHubConnectionCompletion | None = None
        try:
            while self._clock() < deadline:
                self._sleeper(interval)
                if self._clock() >= deadline:
                    break
                try:
                    result = self._gateway.poll_device_token(
                        authorization.device_code
                    )
                except GitHubGatewayError as error:
                    LOGGER.warning(
                        "GitHub Device Flow poll failed for app_user_id=%s: %s",
                        app_user_id,
                        error,
                    )
                    continue

                if result.status is GitHubDevicePollStatus.PENDING:
                    continue
                if result.status is GitHubDevicePollStatus.SLOW_DOWN:
                    interval += 5
                    continue
                if result.status is GitHubDevicePollStatus.AUTHORIZED:
                    completion = self._complete_authorization(
                        app_user_id,
                        result.access_token,
                    )
                    return
                if result.status is GitHubDevicePollStatus.DENIED:
                    LOGGER.info(
                        "GitHub Device Flow denied for app_user_id=%s",
                        app_user_id,
                    )
                    completion = GitHubConnectionCompletion(
                        GitHubConnectionCompletionStatus.DENIED
                    )
                    return
                if result.status is GitHubDevicePollStatus.EXPIRED:
                    break

            LOGGER.info(
                "GitHub Device Flow expired for app_user_id=%s",
                app_user_id,
            )
            completion = GitHubConnectionCompletion(
                GitHubConnectionCompletionStatus.EXPIRED
            )
        except Exception as error:
            LOGGER.error(
                "GitHub Device Flow failed for app_user_id=%s error_type=%s",
                app_user_id,
                type(error).__name__,
            )
            completion = GitHubConnectionCompletion(
                GitHubConnectionCompletionStatus.FAILED
            )
        finally:
            self._remove_session(app_user_id, expected=session)
            self._state_store.release_attempt(
                app_user_id=app_user_id,
                owner=session.owner,
            )
            if completion is not None:
                self._notify(session.completion_handler, completion, app_user_id)

    def _complete_authorization(
        self,
        app_user_id: int,
        access_token: GitHubUserAccessToken | None,
    ) -> GitHubConnectionCompletion:
        if access_token is None:
            raise RuntimeError("Authorized Device Flow returned no access token")
        try:
            account = self._gateway.get_authenticated_account(access_token)
            installation_ids = self._gateway.list_installation_ids(access_token)
            self._account_writer.replace_for_user(
                app_user_id=app_user_id,
                github_user_id=account.github_user_id,
                login=account.login,
                installation_ids=installation_ids,
            )
        except GitHubGatewayError as error:
            LOGGER.warning(
                "GitHub installations could not be saved for app_user_id=%s: %s",
                app_user_id,
                error,
            )
            return GitHubConnectionCompletion(
                GitHubConnectionCompletionStatus.FAILED
            )
        except Exception as error:
            LOGGER.error(
                "GitHub installation storage failed for app_user_id=%s "
                "error_type=%s",
                app_user_id,
                type(error).__name__,
            )
            return GitHubConnectionCompletion(
                GitHubConnectionCompletionStatus.FAILED
            )
        LOGGER.info(
            "GitHub Device Flow completed: app_user_id=%s installation_count=%s",
            app_user_id,
            len(installation_ids),
        )
        if not installation_ids:
            return GitHubConnectionCompletion(
                GitHubConnectionCompletionStatus.NO_INSTALLATIONS
            )
        return GitHubConnectionCompletion(
            GitHubConnectionCompletionStatus.CONNECTED,
            installation_count=len(installation_ids),
            account_login=account.login,
        )

    def _remove_session(
        self,
        app_user_id: int,
        *,
        expected: _GitHubConnectionSession,
    ) -> None:
        with self._lock:
            current = self._sessions.get(app_user_id)
            if current is expected:
                self._sessions.pop(app_user_id, None)

    @staticmethod
    def _notify(
        handler: GitHubConnectionCompletionHandler | None,
        completion: GitHubConnectionCompletion,
        app_user_id: int,
    ) -> None:
        """Вызывает callback, не позволяя ошибке канала уронить worker."""
        if handler is None:
            return
        try:
            handler(completion)
        except Exception as error:
            LOGGER.error(
                "GitHub completion notification failed for app_user_id=%s "
                "error_type=%s",
                app_user_id,
                type(error).__name__,
            )
