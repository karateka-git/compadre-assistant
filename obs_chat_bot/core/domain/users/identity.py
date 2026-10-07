from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Callable

from obs_chat_bot.core.domain.users.ports import (
    AppUserRepository,
    ExternalIdentityRepository,
    IdentityRebindConfirmationRepository,
    IdentityLinkTokenRepository,
)
from obs_chat_bot.core.domain.users.entities import (
    MAX_DISPLAY_NAME_LENGTH,
    AppUser,
    IdentityRebindConfirmation,
    IncomingIdentity,
)


class UserIdentityError(RuntimeError):
    """Базовая ошибка сценариев регистрации и привязки каналов."""


class IdentityAlreadyBoundError(UserIdentityError):
    """Ошибка попытки привязать канал, который уже связан с пользователем."""


class InvalidLinkCodeError(UserIdentityError):
    """Ошибка использования несуществующего, просроченного или погашенного кода."""


class InvalidDisplayNameError(UserIdentityError):
    """Ошибка недопустимого имени внутреннего профиля пользователя."""


@dataclass(frozen=True, slots=True)
class CreatedLinkCode:
    """Одноразовый код привязки и срок его действия."""

    code: str
    expires_at: datetime


class UserIdentityService:
    """Координирует регистрацию пользователей и привязку внешних каналов."""

    def __init__(
        self,
        *,
        app_user_repository: AppUserRepository,
        external_identity_repository: ExternalIdentityRepository,
        link_token_repository: IdentityLinkTokenRepository,
        rebind_confirmation_repository: IdentityRebindConfirmationRepository,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        token_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self._app_user_repository = app_user_repository
        self._external_identity_repository = external_identity_repository
        self._link_token_repository = link_token_repository
        self._rebind_confirmation_repository = rebind_confirmation_repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: token_urlsafe(6))
        self._token_ttl = token_ttl

    def resolve(self, identity: IncomingIdentity) -> AppUser | None:
        """Возвращает внутреннего пользователя для внешней личности."""
        external_identity = self._external_identity_repository.find(identity)
        if external_identity is None:
            return None

        self._external_identity_repository.touch(identity)
        return self._app_user_repository.get_by_id(external_identity.app_user_id)

    def register(self, identity: IncomingIdentity) -> AppUser:
        """Создает нового пользователя и привязывает к нему текущий канал."""
        existing = self.resolve(identity)
        if existing is not None:
            return existing

        app_user = self._app_user_repository.create()
        self._external_identity_repository.create(
            app_user_id=app_user.id,
            identity=identity,
        )
        return app_user

    def update_display_name(
        self,
        *,
        app_user_id: int,
        display_name: str,
    ) -> AppUser:
        """Проверяет и сохраняет имя внутреннего профиля пользователя."""
        normalized = " ".join(display_name.split())
        if not normalized:
            raise InvalidDisplayNameError("Display name must not be empty")
        if len(normalized) > MAX_DISPLAY_NAME_LENGTH:
            raise InvalidDisplayNameError(
                f"Display name must not exceed {MAX_DISPLAY_NAME_LENGTH} characters"
            )
        if normalized.startswith("/") or "://" in normalized:
            raise InvalidDisplayNameError("Display name looks like a command or URL")
        return self._app_user_repository.update_display_name(
            app_user_id=app_user_id,
            display_name=normalized,
        )

    def create_link_code(self, app_user_id: int) -> CreatedLinkCode:
        """Создает одноразовый код для привязки нового канала к пользователю."""
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")

        code = self._token_factory()
        expires_at = self._clock() + self._token_ttl
        self._link_token_repository.create(
            app_user_id=app_user_id,
            token_hash=_hash_code(code),
            expires_at=expires_at,
        )
        return CreatedLinkCode(code=code, expires_at=expires_at)

    def link(self, *, code: str, identity: IncomingIdentity) -> AppUser:
        """Привязывает текущий канал к пользователю по одноразовому коду."""
        if self.resolve(identity) is not None:
            raise IdentityAlreadyBoundError("External identity is already bound")

        app_user_id = self._link_token_repository.consume(
            token_hash=_hash_code(code.strip()),
            now=self._clock(),
        )
        if app_user_id is None:
            raise InvalidLinkCodeError("Link code is invalid or expired")

        self._external_identity_repository.create(
            app_user_id=app_user_id,
            identity=identity,
        )
        app_user = self._app_user_repository.get_by_id(app_user_id)
        if app_user is None:
            raise InvalidLinkCodeError("Link code points to missing user")
        return app_user

    def request_rebind_confirmation(
        self,
        *,
        code: str,
        identity: IncomingIdentity,
    ) -> IdentityRebindConfirmation:
        """Запрашивает адресное подтверждение перепривязки уже известного канала.

        Args:
            code: Одноразовый код целевого профиля.
            identity: Отправитель и исходный чат запроса.

        Returns:
            Ожидание с устойчивым ID и версией для общей команды решения.

        Raises:
            InvalidLinkCodeError: Код истёк, отсутствует или владелец identity изменился.
            IdentityAlreadyBoundError: Identity уже у цели либо её перенос занят другим чатом.
        """
        current_user = self.resolve(identity)
        if current_user is None:
            raise InvalidLinkCodeError("External identity is not bound")

        token_hash = _hash_code(code.strip())
        target_app_user_id = self._link_token_repository.find_active(
            token_hash=token_hash,
            now=self._clock(),
        )
        if target_app_user_id is None:
            raise InvalidLinkCodeError("Link code is invalid or expired")
        if target_app_user_id == current_user.id:
            raise IdentityAlreadyBoundError("External identity is already bound")

        target_user = self._app_user_repository.get_by_id(target_app_user_id)
        if target_user is None:
            raise InvalidLinkCodeError("Link code points to missing user")

        confirmation = self._rebind_confirmation_repository.save(
            identity=identity,
            token_hash=token_hash,
            source_app_user_id=current_user.id,
            target_app_user_id=target_app_user_id,
            now=self._clock(),
            expires_at=self._clock() + self._token_ttl,
        )
        return confirmation

    def confirm_rebind(
        self, identity: IncomingIdentity, *, confirmation_id: int, version: int
    ) -> AppUser:
        """Переносит identity по конкретному актуальному адресу операции ядра.

        Args:
            identity: Отправитель и исходный чат текущего события.
            confirmation_id: ID из показанной команды решения.
            version: Версия этого решения.

        Returns:
            Целевой профиль после атомарного переноса.

        Raises:
            InvalidLinkCodeError: Решение недоступно, устарело или истекло.
        """
        user_id = self._rebind_confirmation_repository.decide(
            identity=identity,
            confirmation_id=confirmation_id,
            version=version,
            approve=True,
            now=self._clock(),
        )
        user = self._app_user_repository.get_by_id(user_id) if user_id else None
        if user is None:
            raise InvalidLinkCodeError("Rebind confirmation is unavailable, stale or expired")
        return user

    def cancel_rebind(
        self, identity: IncomingIdentity, *, confirmation_id: int, version: int
    ) -> None:
        """Отменяет только выбранное актуальное ожидание исходного чата.

        Args:
            identity: Отправитель и исходный чат текущего события.
            confirmation_id: Устойчивый ID операции из команды решения.
            version: Версия, которую пользователь отклоняет.

        Raises:
            InvalidLinkCodeError: Решение недоступно, устарело или истекло.
        """
        if (
            self._rebind_confirmation_repository.decide(
                identity=identity,
                confirmation_id=confirmation_id,
                version=version,
                approve=False,
                now=self._clock(),
            )
            is None
        ):
            raise InvalidLinkCodeError("Rebind confirmation is unavailable, stale or expired")

    def get_pending_rebind(self, identity: IncomingIdentity) -> IdentityRebindConfirmation | None:
        """Читает адресное ожидание только в исходном чате identity."""
        return self._rebind_confirmation_repository.find(identity=identity, now=self._clock())


def _hash_code(code: str) -> str:
    """Возвращает SHA-256 хеш одноразового кода привязки."""
    return sha256(code.encode("utf-8")).hexdigest()
