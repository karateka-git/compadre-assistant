"""SQLite-адаптер управления семьёй и её участниками."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from compadre_assistant.capabilities.family.data.invitation_dtos import (
    InvitationPeriodDto, invitation_period_from_dto,
)
from compadre_assistant.capabilities.family.data.membership_dtos import (
    FamilyDto, FamilyMemberDto, family_from_dto, family_member_from_dto,
)
from compadre_assistant.capabilities.family.data.shopping_dtos import ShoppingItemDto, shopping_item_from_dto
from compadre_assistant.capabilities.family.domain.invitations import (
    InvalidInvitation, InvitationPeriod, InvitationRepository,
)
from compadre_assistant.capabilities.family.domain.membership import (
    AlreadyInFamily, Family, FamilyAccessEnded, FamilyHasOtherMembers, FamilyMember, OtherMemberRequired,
)
from compadre_assistant.capabilities.family.domain.ports import FamilyRepository
from compadre_assistant.capabilities.family.domain.shopping import (
    MAX_ITEM_NUMBER, ItemChange, ItemChangeStatus, ItemNumbersExhausted, ShoppingChangeResult,
    ShoppingItem, ShoppingItemStatus, ShoppingRepository,
)


class SQLiteFamilyRepository(FamilyRepository, InvitationRepository, ShoppingRepository):
    """Хранит семейные данные и проверяет актуальный состав перед изменениями."""

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
        stamp = _format_timestamp(now)
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

    def leave(self, app_user_id: int, family_id: int, *, actor_joined_at: datetime) -> None:
        """Удаляет членство по решению участника, если он не владелец."""
        with self._write():
            family = self._require_membership(app_user_id, family_id, actor_joined_at)
            family.require_may_leave(app_user_id, has_other_members=self._has_other_members(family_id, app_user_id))
            self._delete_member(family_id, app_user_id)

    def remove_member(
        self, app_user_id: int, family_id: int, member_id: int, *,
        actor_joined_at: datetime, target_joined_at: datetime,
    ) -> None:
        """Удаляет членство другого профиля по решению текущего владельца."""
        with self._write():
            self._require_other_member(app_user_id, family_id, member_id, actor_joined_at, target_joined_at)
            self._delete_member(family_id, member_id)

    def transfer_ownership(
        self, app_user_id: int, family_id: int, member_id: int, *,
        actor_joined_at: datetime, target_joined_at: datetime,
    ) -> None:
        """Меняет владельца на другого действующего участника указанной семьи."""
        with self._write():
            self._require_other_member(app_user_id, family_id, member_id, actor_joined_at, target_joined_at)
            self._connection.execute(
                "UPDATE families SET owner_app_user_id = ? WHERE id = ?", (member_id, family_id),
            )

    def close(self, app_user_id: int, family_id: int, *, actor_joined_at: datetime) -> None:
        """Удаляет семью вместе с её данными по решению единственного владельца."""
        with self._write():
            family = self._require_membership(app_user_id, family_id, actor_joined_at)
            family.require_owner(app_user_id)
            if self._has_other_members(family_id, app_user_id):
                raise FamilyHasOtherMembers("Закрыть семью может единственный участник")
            self._connection.execute("DELETE FROM families WHERE id = ?", (family_id,))

    def issue_invitation(
        self, app_user_id: int, family_id: int, code_digest: str, period: InvitationPeriod,
    ) -> Family | None:
        """Сохраняет проверочное значение от текущего участника семьи.

        Args:
            app_user_id: Профиль приглашавшего.
            family_id: Семья, для которой запрашивали код.
            code_digest: Проверочное значение случайного кода.
            period: Срок действия приглашения в UTC.

        Returns:
            Семья после записи или None при коллизии кода.

        Raises:
            FamilyAccessEnded: Профиль больше не состоит в указанной семье.
        """
        with self._write():
            family = self._require_membership(app_user_id, family_id)
            row = self._connection.execute(
                """INSERT INTO family_invites (family_id, created_by_app_user_id, code_digest, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (code_digest) DO NOTHING RETURNING id""",
                (family_id, app_user_id, code_digest, period.created_at.isoformat(), period.expires_at.isoformat()),
            ).fetchone()
            return family if row is not None else None

    def join_by_invitation(self, app_user_id: int, code_digest: str, now: datetime) -> Family:
        """Добавляет участника и погашает приглашение атомарно.

        Args:
            app_user_id: Профиль нового участника.
            code_digest: Проверочное значение введённого кода.
            now: Время проверки и вступления в UTC.

        Returns:
            Семья, в которую принят профиль.

        Raises:
            InvalidInvitation: Код не связан с действующей семьёй.
            InvitationUsed: Код уже погашен.
            InvitationExpired: Истёк срок действия кода.
            AlreadyInFamily: Профиль уже состоит в семье.
        """
        with self._write():
            row = self._connection.execute(
                """SELECT invite.id AS invite_id, invite.created_at AS issued_at, invite.expires_at, invite.used_at,
                          family.id, family.name, family.owner_app_user_id, family.created_at
                   FROM family_invites AS invite JOIN families AS family ON family.id = invite.family_id
                   WHERE invite.code_digest = ?""",
                (code_digest,),
            ).fetchone()
            if row is None:
                raise InvalidInvitation("Семейное приглашение недействительно")
            period = invitation_period_from_dto(InvitationPeriodDto(row["issued_at"], row["expires_at"], row["used_at"]))
            period.require_usable(now)
            if self.find_for_member(app_user_id) is not None:
                raise AlreadyInFamily("Профиль уже состоит в семье")
            self._connection.execute("UPDATE family_invites SET used_at = ? WHERE id = ?", (now.isoformat(), row["invite_id"]))
            self._connection.execute(
                "INSERT INTO family_members (family_id, app_user_id, joined_at) VALUES (?, ?, ?)",
                (row["id"], app_user_id, now.isoformat()),
            )
            return family_from_dto(FamilyDto(row["id"], row["name"], row["owner_app_user_id"], row["created_at"]))

    def list_items(self, app_user_id: int, family_id: int) -> tuple[ShoppingItem, ...]:
        """Читает список и проверяет членство одним запросом.

        Args:
            app_user_id: Профиль, который открывает список.
            family_id: Ожидаемая семья.

        Returns:
            Список покупок по возрастанию номеров, включая неизвестные состояния.

        Raises:
            FamilyAccessEnded: Профиль не состоит в ожидаемой семье.
        """
        rows = self._connection.execute(
            """SELECT item.number, item.name, item.status, item.created_by_app_user_id, item.created_at,
                      item.bought_by_app_user_id, item.bought_at
               FROM family_members AS actor
               LEFT JOIN family_shopping_items AS item ON item.family_id = actor.family_id
               WHERE actor.app_user_id = ? AND actor.family_id = ?
               ORDER BY item.number""",
            (app_user_id, family_id),
        ).fetchall()
        if not rows:
            raise FamilyAccessEnded("Доступ к указанной семье завершён")
        return tuple(shopping_item_from_dto(ShoppingItemDto(**dict(row))) for row in rows if row["number"] is not None)

    def add_items(
        self, app_user_id: int, family_id: int, names: tuple[str, ...], now: datetime,
    ) -> tuple[ShoppingItem, ...]:
        """Резервирует последовательность номеров и пишет все покупки одной транзакцией.

        Args:
            app_user_id: Профиль участника, добавляющего покупки.
            family_id: Ожидаемая семья.
            names: Проверенные названия покупок в порядке ввода.
            now: Время добавления покупок.

        Returns:
            Обновлённый список.

        Raises:
            FamilyAccessEnded: Профиль не состоит в ожидаемой семье.
            ItemNumbersExhausted: В списке не осталось номеров для пакета.
        """
        with self._write():
            self._require_membership(app_user_id, family_id)
            counter = self._connection.execute(
                """UPDATE families SET next_item_number = next_item_number + ?
                   WHERE id = ? AND next_item_number <= ?
                   RETURNING next_item_number""",
                (len(names), family_id, MAX_ITEM_NUMBER + 1 - len(names)),
            ).fetchone()
            if counter is None:
                raise ItemNumbersExhausted("Номера покупок закончились")
            first_number = counter[0] - len(names)
            stamp = _format_timestamp(now)
            self._connection.executemany(
                """INSERT INTO family_shopping_items (family_id, number, name, status, created_by_app_user_id, created_at)
                   VALUES (?, ?, ?, 'needed', ?, ?)""",
                ((family_id, number, name, app_user_id, stamp) for number, name in enumerate(names, first_number)),
            )
            return self.list_items(app_user_id, family_id)

    def buy_items(
        self, app_user_id: int, family_id: int, numbers: tuple[int, ...], now: datetime,
    ) -> ShoppingChangeResult:
        """Помечает нужные покупки купленными; повтор сохраняет первую отметку."""
        return self._change_items(app_user_id, family_id, numbers, bought_at=_format_timestamp(now))

    def delete_items(self, app_user_id: int, family_id: int, numbers: tuple[int, ...]) -> ShoppingChangeResult:
        """Удаляет указанные покупки с известным состоянием."""
        return self._change_items(app_user_id, family_id, numbers, bought_at=None)

    def clear_bought(self, app_user_id: int, family_id: int) -> tuple[ShoppingItem, ...]:
        """Удаляет купленные позиции, проверяя текущее членство и семью."""
        with self._write():
            self._require_membership(app_user_id, family_id)
            self._connection.execute(
                "DELETE FROM family_shopping_items WHERE family_id = ? AND status = 'bought'", (family_id,),
            )
            return self.list_items(app_user_id, family_id)

    def _change_items(
        self, app_user_id: int, family_id: int, numbers: tuple[int, ...], *, bought_at: str | None,
    ) -> ShoppingChangeResult:
        with self._write():
            items = {item.number: item for item in self.list_items(app_user_id, family_id)}
            changes = []
            changed_numbers = []
            for number in dict.fromkeys(numbers):
                item = items.get(number)
                if item is None:
                    status = ItemChangeStatus.NOT_FOUND
                elif item.status is ShoppingItemStatus.UNKNOWN:
                    status = ItemChangeStatus.UNKNOWN
                elif bought_at is not None and item.status is ShoppingItemStatus.BOUGHT:
                    status = ItemChangeStatus.ALREADY_BOUGHT
                else:
                    status = ItemChangeStatus.CHANGED
                    changed_numbers.append(number)
                changes.append(ItemChange(number, status))

            if bought_at is not None:
                self._connection.executemany(
                    """UPDATE family_shopping_items SET status = 'bought', bought_by_app_user_id = ?, bought_at = ?
                       WHERE family_id = ? AND number = ?""",
                    ((app_user_id, bought_at, family_id, number) for number in changed_numbers),
                )
            else:
                self._connection.executemany(
                    "DELETE FROM family_shopping_items WHERE family_id = ? AND number = ?",
                    ((family_id, number) for number in changed_numbers),
                )
            return ShoppingChangeResult(tuple(changes), self.list_items(app_user_id, family_id))

    @contextmanager
    def _write(self) -> Iterator[None]:
        # Блокировка записи перед чтением исключает смену состава между проверкой и изменением.
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            yield

    def _require_membership(
        self, app_user_id: int, family_id: int, expected_joined_at: datetime | None = None,
    ) -> Family:
        family = self.find_for_member(app_user_id)
        if family is None or family.id != family_id:
            raise FamilyAccessEnded("Доступ к указанной семье завершён")
        if expected_joined_at is not None and not self._has_membership(app_user_id, family_id, expected_joined_at):
            raise FamilyAccessEnded("Членство исполнителя сменилось")
        return family

    def _require_other_member(
        self, app_user_id: int, family_id: int, member_id: int, actor_joined_at: datetime, target_joined_at: datetime,
    ) -> None:
        self._require_membership(app_user_id, family_id, actor_joined_at).require_owner(app_user_id)
        if member_id == app_user_id or not self._has_membership(member_id, family_id, target_joined_at):
            raise OtherMemberRequired("Действие требует другого участника указанной семьи")

    def _has_membership(self, app_user_id: int, family_id: int, expected_joined_at: datetime) -> bool:
        row = self._connection.execute(
            "SELECT joined_at FROM family_members WHERE family_id = ? AND app_user_id = ?", (family_id, app_user_id),
        ).fetchone()
        return row is not None and datetime.fromisoformat(row["joined_at"]) == expected_joined_at

    def _has_other_members(self, family_id: int, app_user_id: int) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM family_members WHERE family_id = ? AND app_user_id <> ? LIMIT 1",
            (family_id, app_user_id),
        ).fetchone() is not None

    def _delete_member(self, family_id: int, member_id: int) -> None:
        self._connection.execute(
            "DELETE FROM family_members WHERE family_id = ? AND app_user_id = ?", (family_id, member_id),
        )


def _format_timestamp(value: datetime) -> str:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
