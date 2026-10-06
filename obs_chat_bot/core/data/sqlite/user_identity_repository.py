from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from obs_chat_bot.core.domain.users.ports import (
    AppUserRepository,
    ExternalIdentityRepository,
    IdentityRebindConfirmationRepository,
    IdentityLinkTokenRepository,
)
from obs_chat_bot.core.domain.users.entities import AppUser, ExternalIdentity, IncomingIdentity
from obs_chat_bot.core.data.sqlite.user_identity_dtos import (
    AppUserDto, ExternalIdentityDto, IdentityLinkTokenDto, IdentityRebindConfirmationDto,
)
from obs_chat_bot.core.data.sqlite.user_identity_mappers import (
    app_user_from_dto, external_identity_from_dto, incoming_identity_to_dto,
    link_token_owner_from_dto, rebind_confirmation_from_dto,
)


class SQLiteAppUserRepository(AppUserRepository):
    """Хранит внутренних пользователей приложения в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(self, *, display_name: str | None = None) -> AppUser:
        """Создает нового пользователя приложения."""
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO app_users (display_name)
                VALUES (?)
                """,
                (display_name,),
            )

        user_id = cursor.lastrowid
        if user_id is None:
            raise RuntimeError("SQLite did not return a new app user id")

        user = self.get_by_id(user_id)
        if user is None:
            raise RuntimeError(f"Created app user could not be read: {user_id}")
        return user

    def get_by_id(self, app_user_id: int) -> AppUser | None:
        """Возвращает пользователя приложения по ID."""
        row = self._connection.execute(
            """
            SELECT id, display_name, created_at
            FROM app_users
            WHERE id = ?
            """,
            (app_user_id,),
        ).fetchone()
        if row is None:
            return None
        return app_user_from_dto(AppUserDto(**dict(row)))

    def update_display_name(
        self,
        *,
        app_user_id: int,
        display_name: str,
    ) -> AppUser:
        """Обновляет пользовательское имя профиля."""
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE app_users
                SET display_name = ?
                WHERE id = ?
                """,
                (display_name, app_user_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("App user does not exist")
        user = self.get_by_id(app_user_id)
        if user is None:
            raise RuntimeError("Updated app user could not be read")
        return user

    def delete(self, app_user_id: int) -> None:
        """Удаляет пользователя приложения по ID."""
        with self._connection:
            self._connection.execute(
                "DELETE FROM app_users WHERE id = ?",
                (app_user_id,),
            )


class SQLiteExternalIdentityRepository(ExternalIdentityRepository):
    """Хранит связи пользователей приложения с внешними каналами."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find(self, identity: IncomingIdentity) -> ExternalIdentity | None:
        """Ищет связь по каналу и внешнему ID пользователя."""
        dto = incoming_identity_to_dto(identity)
        row = self._connection.execute(
            """
            SELECT
                id,
                app_user_id,
                channel,
                external_user_id,
                external_chat_id,
                username,
                display_name
            FROM external_identities
            WHERE channel = ? AND external_user_id = ?
            """,
            (dto.channel, dto.external_user_id),
        ).fetchone()
        return external_identity_from_dto(ExternalIdentityDto(**dict(row))) if row is not None else None

    def create(
        self,
        *,
        app_user_id: int,
        identity: IncomingIdentity,
    ) -> ExternalIdentity:
        """Создает связь внутреннего пользователя с внешней личностью."""
        dto = incoming_identity_to_dto(identity)
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO external_identities (
                    app_user_id,
                    channel,
                    external_user_id,
                    external_chat_id,
                    username,
                    display_name
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    app_user_id,
                    dto.channel,
                    dto.external_user_id,
                    dto.external_chat_id,
                    dto.username,
                    dto.display_name,
                ),
            )

        identity_id = cursor.lastrowid
        if identity_id is None:
            raise RuntimeError("SQLite did not return a new external identity id")

        created = self.find(identity)
        if created is None:
            raise RuntimeError(
                f"Created external identity could not be read: {identity_id}"
            )
        return created

    def reassign(
        self,
        *,
        app_user_id: int,
        identity: IncomingIdentity,
    ) -> ExternalIdentity:
        """Перепривязывает существующую внешнюю личность к другому пользователю."""
        dto = incoming_identity_to_dto(identity)
        with self._connection:
            self._connection.execute(
                """
                UPDATE external_identities
                SET
                    app_user_id = ?,
                    external_chat_id = ?,
                    username = ?,
                    display_name = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE channel = ? AND external_user_id = ?
                """,
                (
                    app_user_id,
                    dto.external_chat_id,
                    dto.username,
                    dto.display_name,
                    dto.channel,
                    dto.external_user_id,
                ),
            )

        updated = self.find(identity)
        if updated is None:
            raise RuntimeError("Reassigned external identity could not be read")
        return updated

    def count_for_user(self, app_user_id: int) -> int:
        """Возвращает количество внешних личностей пользователя."""
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS identity_count
            FROM external_identities
            WHERE app_user_id = ?
            """,
            (app_user_id,),
        ).fetchone()
        return int(row["identity_count"])

    def touch(self, identity: IncomingIdentity) -> None:
        """Обновляет чат, имя и время последнего появления внешней личности."""
        dto = incoming_identity_to_dto(identity)
        with self._connection:
            self._connection.execute(
                """
                UPDATE external_identities
                SET
                    external_chat_id = ?,
                    username = ?,
                    display_name = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE channel = ? AND external_user_id = ?
                """,
                (
                    dto.external_chat_id,
                    dto.username,
                    dto.display_name,
                    dto.channel,
                    dto.external_user_id,
                ),
            )


class SQLiteIdentityLinkTokenRepository(IdentityLinkTokenRepository):
    """Хранит одноразовые коды привязки каналов в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(
        self,
        *,
        app_user_id: int,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        """Сохраняет одноразовый код привязки."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO identity_link_tokens (
                    app_user_id,
                    token_hash,
                    expires_at
                )
                VALUES (?, ?, ?)
                """,
                (app_user_id, token_hash, _format_timestamp(expires_at)),
            )

    def consume(self, *, token_hash: str, now: datetime) -> int | None:
        """Погашает действующий код и возвращает ID пользователя."""
        with self._connection:
            row = self._find_active_dto(token_hash=token_hash, now=now)
            if row is None:
                return None

            cursor = self._connection.execute(
                """
                UPDATE identity_link_tokens
                SET used_at = CURRENT_TIMESTAMP
                WHERE id = ? AND used_at IS NULL
                """,
                (row.id,),
            )
            if cursor.rowcount != 1:
                return None
            return link_token_owner_from_dto(row)

    def find_active(self, *, token_hash: str, now: datetime) -> int | None:
        """Возвращает ID пользователя для действующего кода без погашения."""
        row = self._find_active_dto(token_hash=token_hash, now=now)
        return link_token_owner_from_dto(row) if row is not None else None

    def _find_active_dto(
        self,
        *,
        token_hash: str,
        now: datetime,
    ) -> IdentityLinkTokenDto | None:
        row = self._connection.execute(
            """
            SELECT id, app_user_id
            FROM identity_link_tokens
            WHERE token_hash = ?
                AND used_at IS NULL
                AND expires_at > ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (token_hash, _format_timestamp(now)),
        ).fetchone()
        return IdentityLinkTokenDto(**dict(row)) if row is not None else None


class SQLiteIdentityRebindConfirmationRepository(IdentityRebindConfirmationRepository):
    """Хранит ожидающие подтверждения перепривязки каналов в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(
        self,
        *,
        identity: IncomingIdentity,
        token_hash: str,
        target_app_user_id: int,
        expires_at: datetime,
    ) -> None:
        """Сохраняет ожидающее подтверждение перепривязки канала."""
        dto = incoming_identity_to_dto(identity)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO identity_rebind_confirmations (
                    channel,
                    external_user_id,
                    token_hash,
                    target_app_user_id,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel, external_user_id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    target_app_user_id = excluded.target_app_user_id,
                    expires_at = excluded.expires_at,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    dto.channel,
                    dto.external_user_id,
                    token_hash,
                    target_app_user_id,
                    _format_timestamp(expires_at),
                ),
            )

    def find(
        self,
        *,
        identity: IncomingIdentity,
        now: datetime,
    ) -> tuple[str, int] | None:
        """Возвращает активное подтверждение перепривязки канала."""
        dto = incoming_identity_to_dto(identity)
        row = self._connection.execute(
            """
            SELECT token_hash, target_app_user_id
            FROM identity_rebind_confirmations
            WHERE channel = ?
                AND external_user_id = ?
                AND expires_at > ?
            """,
            (
                dto.channel,
                dto.external_user_id,
                _format_timestamp(now),
            ),
        ).fetchone()
        if row is None:
            return None
        return rebind_confirmation_from_dto(IdentityRebindConfirmationDto(**dict(row)))

    def delete(self, *, identity: IncomingIdentity) -> None:
        """Удаляет ожидающее подтверждение перепривязки канала."""
        dto = incoming_identity_to_dto(identity)
        with self._connection:
            self._connection.execute(
                """
                DELETE FROM identity_rebind_confirmations
                WHERE channel = ? AND external_user_id = ?
                """,
                (dto.channel, dto.external_user_id),
            )


def _format_timestamp(value: datetime) -> str:
    """Форматирует timestamp для SQLite в UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
