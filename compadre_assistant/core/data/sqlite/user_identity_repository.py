from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from compadre_assistant.core.domain.users.ports import (
    AppUserRepository,
    ExternalIdentityRepository,
    IdentityRebindConfirmationRepository,
    IdentityLinkTokenRepository,
    IdentityLifecycleRepository,
)
from compadre_assistant.core.domain.users.entities import AppUser, ExternalIdentity, IncomingIdentity, IdentityRebindConfirmation
from compadre_assistant.core.domain.users.identity import IdentityAlreadyBoundError, InvalidLinkCodeError
from compadre_assistant.core.data.sqlite.user_identity_dtos import (
    AppUserDto, ExternalIdentityDto, IncomingIdentityDto, IdentityLinkTokenDto, IdentityRebindConfirmationDto,
)
from compadre_assistant.core.data.sqlite.user_identity_mappers import (
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


class SQLiteIdentityLifecycleRepository(IdentityLifecycleRepository):
    """Сериализует регистрацию и привязку канала в коротких SQLite-транзакциях."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Сохраняет соединение, настроенное с foreign keys и busy timeout.

        Args:
            connection: Соединение SQLite, принадлежащее текущему core scope.
        """
        self._connection = connection

    def register(self, identity: IncomingIdentity) -> AppUser:
        """Создаёт профиль вместе с identity либо возвращает её владельца.

        Args:
            identity: Отправитель из внешнего канала.

        Returns:
            Существующий или созданный профиль.
        """
        dto = incoming_identity_to_dto(identity)
        creating = False
        try:
            with self._connection:
                # Блокировка писателя до чтения исключает гонку двух регистраций.
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT app_user_id FROM external_identities WHERE channel = ? AND external_user_id = ?",
                    (dto.channel, dto.external_user_id),
                ).fetchone()
                if existing is not None:
                    self._touch_identity(dto)
                    return self._require_user(existing["app_user_id"])
                creating = True
                cursor = self._connection.execute("INSERT INTO app_users DEFAULT VALUES")
                app_user_id = cursor.lastrowid
                if app_user_id is None:
                    raise RuntimeError("SQLite did not return a new app user id")
                self._insert_identity(app_user_id, dto)
                return self._require_user(app_user_id)
        except sqlite3.IntegrityError:
            # При нарушении UNIQUE обе вставки уже откатились; возвращаем победителя гонки.
            if not creating:
                raise
            existing = self._connection.execute(
                "SELECT app_user_id FROM external_identities WHERE channel = ? AND external_user_id = ?",
                (dto.channel, dto.external_user_id),
            ).fetchone()
            if existing is None:
                raise
            return self._require_user(existing["app_user_id"])

    def link(self, *, identity: IncomingIdentity, token_hash: str, now: datetime) -> AppUser | None:
        """Погашает link-код и создаёт identity одним коммитом.

        Args:
            identity: Новая внешняя личность.
            token_hash: SHA-256 хеш кода.
            now: UTC-время проверки TTL.

        Returns:
            Владелец кода либо `None` для недействительного кода.

        Raises:
            IdentityAlreadyBoundError: Внешняя личность уже привязана.
        """
        dto = incoming_identity_to_dto(identity)
        try:
            with self._connection:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT 1 FROM external_identities WHERE channel = ? AND external_user_id = ?",
                    (dto.channel, dto.external_user_id),
                ).fetchone()
                if existing is not None:
                    raise IdentityAlreadyBoundError("External identity is already bound")
                token = self._connection.execute(
                    """UPDATE identity_link_tokens SET used_at = ?
                    WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                    RETURNING app_user_id""",
                    (_format_timestamp(now), token_hash, _format_timestamp(now)),
                ).fetchone()
                if token is None:
                    return None
                app_user_id = token["app_user_id"]
                self._insert_identity(app_user_id, dto)
                return self._require_user(app_user_id)
        except sqlite3.IntegrityError:
            # Ошибка UNIQUE после погашения откатывает и used_at.
            existing = self._connection.execute(
                "SELECT 1 FROM external_identities WHERE channel = ? AND external_user_id = ?",
                (dto.channel, dto.external_user_id),
            ).fetchone()
            if existing is not None:
                raise IdentityAlreadyBoundError("External identity is already bound") from None
            raise

    def _insert_identity(self, app_user_id: int, dto: IncomingIdentityDto) -> None:
        self._connection.execute(
            """INSERT INTO external_identities
            (app_user_id, channel, external_user_id, external_chat_id, username, display_name)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (app_user_id, dto.channel, dto.external_user_id, dto.external_chat_id,
             dto.username, dto.display_name),
        )

    def _touch_identity(self, dto: IncomingIdentityDto) -> None:
        self._connection.execute(
            """UPDATE external_identities SET external_chat_id = ?, username = ?,
            display_name = ?, last_seen_at = CURRENT_TIMESTAMP
            WHERE channel = ? AND external_user_id = ?""",
            (dto.external_chat_id, dto.username, dto.display_name, dto.channel, dto.external_user_id),
        )

    def _require_user(self, app_user_id: int) -> AppUser:
        row = self._connection.execute(
            "SELECT id, display_name, created_at FROM app_users WHERE id = ?", (app_user_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Identity owner is missing")
        return app_user_from_dto(AppUserDto(**dict(row)))


class SQLiteIdentityRebindConfirmationRepository(IdentityRebindConfirmationRepository):
    """Хранит ожидающие подтверждения перепривязки каналов в SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(
        self,
        *,
        identity: IncomingIdentity,
        token_hash: str,
        source_app_user_id: int,
        target_app_user_id: int,
        expires_at: datetime,
        now: datetime,
    ) -> IdentityRebindConfirmation:
        """Создаёт новый ID, завершая прежнее ожидание только того же чата."""
        with self._connection:
            self._connection.execute(
                """UPDATE identity_rebind_confirmations SET status = 'cancelled', version = version + 1
                WHERE channel = ? AND external_user_id = ? AND status = 'pending'
                    AND (origin_chat_id = ? OR expires_at <= ?)""",
                (
                    identity.channel,
                    identity.external_user_id,
                    identity.external_chat_id,
                    _format_timestamp(now),
                ),
            )
            busy = self._connection.execute(
                """SELECT 1 FROM identity_rebind_confirmations WHERE channel = ? AND external_user_id = ?
                    AND status = 'pending' AND expires_at > ?""",
                (identity.channel, identity.external_user_id, _format_timestamp(now)),
            ).fetchone()
            if busy:
                raise IdentityAlreadyBoundError(
                    "Identity transfer is already pending in its origin chat"
                )
            current = self._connection.execute(
                "SELECT app_user_id FROM external_identities WHERE channel = ? AND external_user_id = ?",
                (identity.channel, identity.external_user_id),
            ).fetchone()
            if current is None or current["app_user_id"] != source_app_user_id:
                raise InvalidLinkCodeError("Identity owner changed before confirmation request")
            cursor = self._connection.execute(
                """INSERT INTO identity_rebind_confirmations (channel, external_user_id, origin_chat_id,
                source_app_user_id, token_hash, target_app_user_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    identity.channel,
                    identity.external_user_id,
                    identity.external_chat_id,
                    source_app_user_id,
                    token_hash,
                    target_app_user_id,
                    _format_timestamp(expires_at),
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM identity_rebind_confirmations WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return rebind_confirmation_from_dto(
            IdentityRebindConfirmationDto(
                **{name: row[name] for name in IdentityRebindConfirmationDto.__dataclass_fields__}
            )
        )

    def find(self, *, identity: IncomingIdentity, now: datetime) -> IdentityRebindConfirmation | None:
        """Возвращает активное ожидание только текущего исходного чата."""
        row = self._connection.execute(
            """SELECT * FROM identity_rebind_confirmations WHERE channel = ? AND external_user_id = ?
            AND origin_chat_id = ? AND status = 'pending' AND expires_at > ?""",
            (
                identity.channel,
                identity.external_user_id,
                identity.external_chat_id,
                _format_timestamp(now),
            ),
        ).fetchone()
        return (
            rebind_confirmation_from_dto(
                IdentityRebindConfirmationDto(
                    **{name: row[name] for name in IdentityRebindConfirmationDto.__dataclass_fields__}
                )
            )
            if row
            else None
        )

    def decide(
        self,
        *,
        identity: IncomingIdentity,
        confirmation_id: int,
        version: int,
        approve: bool,
        now: datetime
    ) -> int | None:
        """Атомарно завершает конкретный перенос вместе с погашением link token.

        Args:
            identity: Отправитель и исходный чат текущего события.
            confirmation_id: Устойчивый ID показанного ожидания.
            version: Версия адресного решения пользователя.
            approve: Перенести identity либо отменить ожидание.
            now: UTC-время для проверки TTL ожидания и link token.

        Returns:
            ID итогового профиля либо `None` при недоступном или устаревшем решении.
        """
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE identity_rebind_confirmations SET status = ?, version = version + 1
                WHERE id = ? AND version = ? AND status = 'pending' AND expires_at > ?
                    AND channel = ? AND external_user_id = ? AND origin_chat_id = ?
                    AND source_app_user_id = (SELECT app_user_id FROM external_identities WHERE channel = ? AND external_user_id = ?)""",
                (
                    "applied" if approve else "cancelled",
                    confirmation_id,
                    version,
                    _format_timestamp(now),
                    identity.channel,
                    identity.external_user_id,
                    identity.external_chat_id,
                    identity.channel,
                    identity.external_user_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = self._connection.execute(
                "SELECT * FROM identity_rebind_confirmations WHERE id = ?", (confirmation_id,)
            ).fetchone()
            confirmation = rebind_confirmation_from_dto(
                IdentityRebindConfirmationDto(
                    **{name: row[name] for name in IdentityRebindConfirmationDto.__dataclass_fields__}
                )
            )
            if not approve:
                return confirmation.source_app_user_id
            token = self._connection.execute(
                """UPDATE identity_link_tokens SET used_at = ? WHERE token_hash = ? AND app_user_id = ?
                AND used_at IS NULL AND expires_at > ?""",
                (
                    _format_timestamp(now),
                    confirmation.token_hash,
                    confirmation.target_app_user_id,
                    _format_timestamp(now),
                ),
            )
            if token.rowcount != 1:
                # Непригодный код завершает именно эту запись; новую запись он не затрагивает.
                self._connection.execute(
                    "UPDATE identity_rebind_confirmations SET status = 'cancelled' WHERE id = ?",
                    (confirmation_id,),
                )
                return None
            self._connection.execute(
                "UPDATE external_identities SET app_user_id = ?, external_chat_id = ?, username = ?, display_name = ?, last_seen_at = CURRENT_TIMESTAMP WHERE channel = ? AND external_user_id = ? AND app_user_id = ?",
                (
                    confirmation.target_app_user_id,
                    identity.external_chat_id,
                    identity.username,
                    identity.display_name,
                    identity.channel,
                    identity.external_user_id,
                    confirmation.source_app_user_id,
                ),
            )
            self._connection.execute(
                "DELETE FROM app_users WHERE id = ? AND NOT EXISTS (SELECT 1 FROM external_identities WHERE app_user_id = ?)",
                (confirmation.source_app_user_id, confirmation.source_app_user_id),
            )
            return confirmation.target_app_user_id


def _format_timestamp(value: datetime) -> str:
    """Форматирует timestamp для SQLite в UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
