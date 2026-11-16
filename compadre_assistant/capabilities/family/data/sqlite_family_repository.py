"""SQLite-адаптер управления семьёй и её участниками."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from compadre_assistant.capabilities.family.data.membership_dtos import (
    FamilyDto, FamilyMemberDto, family_from_dto, family_member_from_dto,
)
from compadre_assistant.capabilities.family.domain.membership import (
    AlreadyInFamily, Family, FamilyAccessEnded, FamilyHasOtherMembers, FamilyMember, OtherMemberRequired,
)
from compadre_assistant.capabilities.family.domain.ports import FamilyRepository


class SQLiteFamilyRepository(FamilyRepository):
    """Хранит семью и проверяет актуальный состав перед каждым изменением."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Принимает настроенное соединение без открытой транзакции."""
        self._connection = connection

    def create(self, app_user_id: int, name: str, now: datetime) -> Family:
        """Создаёт семью и членство владельца в одной транзакции.

        Args:
            app_user_id: Профиль создателя.
            name: Проверенное название семьи.
            now: Используемое время создания.

        Returns:
            Созданная семья с идентификатором.

        Raises:
            AlreadyInFamily: Профиль уже состоит в семье.
        """
        stamp = (now if now.tzinfo is not None else now.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
        with self._write():
            if self._connection.execute(
                "SELECT 1 FROM family_members WHERE app_user_id = ?", (app_user_id,),
            ).fetchone() is not None:
                raise AlreadyInFamily("Профиль уже состоит в семье")
            row = self._connection.execute(
                """INSERT INTO families (name, owner_app_user_id, created_at) VALUES (?, ?, ?)
                   RETURNING id, name, owner_app_user_id, created_at""", (name, app_user_id, stamp),
            ).fetchone()
            self._connection.execute(
                "INSERT INTO family_members (family_id, app_user_id, joined_at) VALUES (?, ?, ?)",
                (row["id"], app_user_id, stamp),
            )
            return family_from_dto(FamilyDto(**dict(row)))

    def find_for_member(self, app_user_id: int) -> Family | None:
        """Читает семью текущего участника или возвращает None."""
        row = self._connection.execute(
            """SELECT family.id, family.name, family.owner_app_user_id, family.created_at
               FROM family_members AS member JOIN families AS family ON family.id = member.family_id
               WHERE member.app_user_id = ?""", (app_user_id,),
        ).fetchone()
        return family_from_dto(FamilyDto(**dict(row))) if row is not None else None

    def list_members(self, app_user_id: int, family_id: int) -> tuple[FamilyMember, ...]:
        """Читает состав указанной семьи в том же снимке, что и доступ профиля.

        Args:
            app_user_id: Профиль, запрашивающий состав.
            family_id: Идентификатор выбранной семьи.

        Returns:
            Участники в порядке вступления, с владельцем в начале.

        Raises:
            FamilyAccessEnded: Запрашивающий профиль не состоит в выбранной семье.
        """
        rows = self._connection.execute(
            """SELECT member.app_user_id, member.joined_at, family.owner_app_user_id
               FROM family_members AS actor JOIN families AS family ON family.id = actor.family_id
               JOIN family_members AS member ON member.family_id = family.id
               WHERE actor.app_user_id = ? AND family.id = ?
               ORDER BY (member.app_user_id = family.owner_app_user_id) DESC, member.joined_at, member.app_user_id""",
            (app_user_id, family_id),
        ).fetchall()
        if not rows:
            raise FamilyAccessEnded("Доступ к указанной семье завершён")
        return tuple(family_member_from_dto(FamilyMemberDto(**dict(row))) for row in rows)

    def leave(self, app_user_id: int, family_id: int) -> None:
        """Удаляет членство по решению участника, если он не владелец."""
        with self._write():
            family = self._require_membership(app_user_id, family_id)
            family.require_may_leave(app_user_id, has_other_members=self._has_other_members(family_id, app_user_id))
            self._delete_member(family_id, app_user_id)

    def remove_member(self, app_user_id: int, family_id: int, member_id: int) -> None:
        """Удаляет членство другого профиля по решению текущего владельца."""
        with self._write():
            self._require_other_member(app_user_id, family_id, member_id)
            self._delete_member(family_id, member_id)

    def transfer_ownership(self, app_user_id: int, family_id: int, member_id: int) -> None:
        """Меняет владельца на другого действующего участника указанной семьи."""
        with self._write():
            self._require_other_member(app_user_id, family_id, member_id)
            self._connection.execute(
                "UPDATE families SET owner_app_user_id = ? WHERE id = ?", (member_id, family_id),
            )

    def close(self, app_user_id: int, family_id: int) -> None:
        """Удаляет семью вместе с её данными по решению единственного владельца."""
        with self._write():
            family = self._require_membership(app_user_id, family_id)
            family.require_owner(app_user_id)
            if self._has_other_members(family_id, app_user_id):
                raise FamilyHasOtherMembers("Закрыть семью может единственный участник")
            self._connection.execute("DELETE FROM families WHERE id = ?", (family_id,))

    @contextmanager
    def _write(self) -> Iterator[None]:
        # Блокировка записи перед чтением исключает смену состава между проверкой и изменением.
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            yield

    def _require_membership(self, app_user_id: int, family_id: int) -> Family:
        family = self.find_for_member(app_user_id)
        if family is None or family.id != family_id:
            raise FamilyAccessEnded("Доступ к указанной семье завершён")
        return family

    def _require_other_member(self, app_user_id: int, family_id: int, member_id: int) -> None:
        self._require_membership(app_user_id, family_id).require_owner(app_user_id)
        if member_id == app_user_id or self._connection.execute(
            "SELECT 1 FROM family_members WHERE family_id = ? AND app_user_id = ?", (family_id, member_id),
        ).fetchone() is None:
            raise OtherMemberRequired("Действие требует другого участника указанной семьи")

    def _has_other_members(self, family_id: int, app_user_id: int) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM family_members WHERE family_id = ? AND app_user_id <> ? LIMIT 1",
            (family_id, app_user_id),
        ).fetchone() is not None

    def _delete_member(self, family_id: int, member_id: int) -> None:
        self._connection.execute(
            "DELETE FROM family_members WHERE family_id = ? AND app_user_id = ?", (family_id, member_id),
        )
