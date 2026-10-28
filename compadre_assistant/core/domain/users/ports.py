from __future__ import annotations

from datetime import datetime
from typing import Protocol

from compadre_assistant.core.domain.users.entities import AppUser, ExternalIdentity, IncomingIdentity, IdentityRebindConfirmation


class AppUserRepository(Protocol):
    """Описывает хранилище внутренних пользователей приложения."""

    def create(self, *, display_name: str | None = None) -> AppUser:
        """Создает нового пользователя приложения."""

    def get_by_id(self, app_user_id: int) -> AppUser | None:
        """Возвращает пользователя по ID или `None`."""

    def update_display_name(
        self,
        *,
        app_user_id: int,
        display_name: str,
    ) -> AppUser:
        """Обновляет пользовательское имя профиля."""

    def delete(self, app_user_id: int) -> None:
        """Удаляет пользователя приложения по ID."""


class ExternalIdentityRepository(Protocol):
    """Описывает хранилище связей внутренних пользователей с внешними каналами."""

    def find(self, identity: IncomingIdentity) -> ExternalIdentity | None:
        """Ищет связь по каналу и внешнему ID пользователя."""

    def list_for_user(self, app_user_id: int) -> tuple[ExternalIdentity, ...]:
        """Возвращает известные личные адреса профиля без пустых и неоднозначных чатов.

        Адреса поступают только от входов личных диалогов. Перечисление не
        обновляет время посещения и не восстанавливает удалённые связи.
        """

    def list_all_private_chats(self) -> tuple[ExternalIdentity, ...]:
        """Возвращает однозначные личные адреса всех существующих профилей."""

    def find_private_chat(self, channel: str, chat_id: str) -> ExternalIdentity | None:
        """Возвращает одну текущую личную identity адреса либо None при неоднозначности."""

    def has_chat_address(self, channel: str, chat_id: str) -> bool:
        """Сообщает, занят ли адрес хотя бы одной сохранённой identity."""

    def create(
        self,
        *,
        app_user_id: int,
        identity: IncomingIdentity,
    ) -> ExternalIdentity:
        """Создает связь пользователя приложения с внешней личностью."""

    def reassign(
        self,
        *,
        app_user_id: int,
        identity: IncomingIdentity,
    ) -> ExternalIdentity:
        """Перепривязывает существующую внешнюю личность к другому пользователю."""

    def count_for_user(self, app_user_id: int) -> int:
        """Возвращает количество внешних личностей пользователя."""

    def touch(self, identity: IncomingIdentity) -> None:
        """Обновляет служебные данные уже известной внешней личности."""


class IdentityLinkTokenRepository(Protocol):
    """Описывает хранилище одноразовых кодов привязки каналов."""

    def create(
        self,
        *,
        app_user_id: int,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        """Сохраняет одноразовый код привязки."""

    def find_active(self, *, token_hash: str, now: datetime) -> int | None:
        """Возвращает ID пользователя для действующего кода без погашения."""


class IdentityLifecycleRepository(Protocol):
    """Выполняет неделимые изменения профиля, identity и link-кода."""

    def register_with_name(self, identity: IncomingIdentity, *, display_name: str) -> AppUser:
        """Возвращает владельца identity либо атомарно создаёт профиль с проверенным именем и identity."""

    def link(self, *, identity: IncomingIdentity, token_hash: str, now: datetime) -> AppUser | None:
        """Погашает действующий код вместе с созданием identity.

        Args:
            identity: Новая внешняя личность.
            token_hash: SHA-256 хеш одноразового кода.
            now: Текущее время для проверки срока действия.

        Returns:
            Целевой профиль либо `None`, если код недействителен.

        Raises:
            IdentityAlreadyBoundError: Внешняя личность уже привязана.
        """


class IdentityRebindConfirmationRepository(Protocol):
    """Описывает хранилище ожидающих подтверждений перепривязки каналов."""

    def save(
        self,
        *,
        identity: IncomingIdentity,
        token_hash: str,
        source_app_user_id: int,
        target_app_user_id: int,
        expires_at: datetime,
        now: datetime
    ) -> IdentityRebindConfirmation:
        """Создаёт новый устойчивый ID ожидания в исходном чате."""

    def find(self, *, identity: IncomingIdentity, now: datetime) -> IdentityRebindConfirmation | None:
        """Читает только ожидание текущего исходного чата identity."""

    def decide(
        self,
        *,
        identity: IncomingIdentity,
        confirmation_id: int,
        version: int,
        approve: bool,
        now: datetime
    ) -> int | None:
        """Атомарно проверяет адрес/владельца/TTL/версию, погашает код и переносит identity."""
