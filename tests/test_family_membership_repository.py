"""Проверяет предметные правила семьи на SQLite с актуальной схемой."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.domain.manage_family import ManageFamily
from compadre_assistant.capabilities.family.domain.membership import (
    AlreadyInFamily, FamilyAccessEnded, FamilyHasOtherMembers, FamilyMember, InvalidFamilyName,
    OtherMemberRequired, OwnerCannotLeave, OwnerRequired,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations


_NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


class SQLiteFamilyRepositoryTest(unittest.TestCase):
    """Работает с отдельной файловой SQLite на каждый сценарий."""

    def setUp(self) -> None:
        """Применяет миграции и создаёт четыре независимых профиля."""
        self.path = Path(self.enterContext(TemporaryDirectory(prefix="family-membership-"))) / "family.db"
        self.db = self.enterContext(connect_database(self.path))
        apply_migrations(self.db)
        with self.db:
            self.db.execute("INSERT INTO app_users (id) VALUES (1), (2), (3), (4)")
        self.repository = SQLiteFamilyRepository(self.db)
        self.family = ManageFamily(self.repository, clock=lambda: _NOW)

    def test_create_and_find_member_family(self) -> None:
        """Назначает первого владельца, проверяет название и отказывает при повторе."""
        self.assertIsNone(self.family.find_for_member(1))
        with self.assertRaises(InvalidFamilyName):
            self.family.create(1, " \t\n")
        home = self.family.create(1, "  Дом  ")

        self.assertEqual((home.name, home.owner_app_user_id, home.created_at), ("Дом", 1, _NOW))
        self.assertEqual(self.repository.find_for_member(1), home)
        self.assertIsNone(self.repository.find_for_member(2))
        with self.assertRaises(AlreadyInFamily):
            self.family.create(1, "Другая семья")
        self.assertEqual(self._families(), [(home.id, 1)])
        self.assertEqual(self.family.list_members(1, home.id), (FamilyMember(1, _NOW, True),))

    def test_create_rolls_back_when_owner_cannot_be_inserted(self) -> None:
        """Сбой FK во вставке владельца откатывает созданную запись семьи."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.create(900, "Дом", _NOW)
        self.assertEqual(self._families(), [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM family_members").fetchone()[0], 0)
        self.assertFalse(self.db.in_transaction)

    def test_concurrent_create_has_exactly_one_winner(self) -> None:
        """Два соединения не могут создать две семьи для одного профиля."""
        ready = Barrier(2, timeout=5)

        def create(name: str) -> bool:
            with connect_database(self.path) as db:
                ready.wait()
                try:
                    SQLiteFamilyRepository(db).create(1, name, _NOW)
                except AlreadyInFamily:
                    return False
                return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(create, ("Первый", "Второй")), (True, False))
        self.assertEqual(self._families(), [(self.repository.find_for_member(1).id, 1)])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM family_members").fetchone()[0], 1)

    def test_list_members_is_scoped_and_shows_current_owner_first(self) -> None:
        """Состав виден любому участнику только в пределах его семьи."""
        home = self.family.create(1, "Дом")
        other = self.family.create(3, "Другая семья")
        self._join(home.id, 2)

        self.assertEqual(self.family.list_members(2, home.id), (FamilyMember(1, _NOW, True), FamilyMember(2, _NOW, False)))
        self.assertEqual(self.repository.find_for_member(2), home)
        with self.assertRaises(FamilyAccessEnded):
            self.family.list_members(2, other.id)
        with self.assertRaises(FamilyAccessEnded):
            self.family.list_members(4, home.id)

    def test_leave_checks_owner_state_and_expected_family(self) -> None:
        """Владелец получает подсказку по составу, а обычный участник может выйти."""
        home = self.family.create(1, "Дом")
        with self.assertRaises(OwnerCannotLeave) as alone:
            self.family.leave(1, home.id)
        self.assertFalse(alone.exception.has_other_members)
        self._join(home.id, 2)
        with self.assertRaises(OwnerCannotLeave) as with_people:
            self.family.leave(1, home.id)
        self.assertTrue(with_people.exception.has_other_members)

        self.family.leave(2, home.id)
        self.assertIsNone(self.family.find_for_member(2))
        other = self.family.create(2, "Другая семья")
        with self.assertRaises(FamilyAccessEnded):
            self.family.leave(2, home.id)
        self.assertEqual(self.family.find_for_member(2), other)
        self.assertEqual(self.family.list_members(1, home.id), (FamilyMember(1, _NOW, True),))

    def test_remove_member_requires_current_owner_and_current_target(self) -> None:
        """Устаревшая роль и цель из другой семьи не меняют состав."""
        home = self.family.create(1, "Дом")
        other = self.family.create(3, "Другая семья")
        self._join(home.id, 2)
        with self.assertRaises(FamilyAccessEnded):
            self.family.remove_member(3, home.id, 2)
        with self.assertRaises(OwnerRequired):
            self.family.remove_member(2, home.id, 1)
        with self.assertRaises(OtherMemberRequired):
            self.family.remove_member(1, home.id, 1)
        with self.assertRaises(OtherMemberRequired):
            self.family.remove_member(1, home.id, 3)

        self.family.remove_member(1, home.id, 2)
        self.assertIsNone(self.family.find_for_member(2))
        with self.assertRaises(OtherMemberRequired):
            self.family.remove_member(1, home.id, 2)
        self.assertEqual(self.family.find_for_member(3), other)
        self.assertEqual(self.family.list_members(1, home.id), (FamilyMember(1, _NOW, True),))

    def test_transfer_applies_only_to_current_members_and_keeps_old_owner(self) -> None:
        """Передача проверяет цель и оставляет бывшего владельца участником."""
        home = self.family.create(1, "Дом")
        self._join(home.id, 2)
        self._join(home.id, 3)
        self.family.leave(3, home.id)
        with self.assertRaises(OtherMemberRequired):
            self.family.transfer_ownership(1, home.id, 3)
        self.assertEqual(self.family.find_for_member(1), home)

        self.family.transfer_ownership(1, home.id, 2)
        self.assertEqual(self.family.list_members(1, home.id), (FamilyMember(2, _NOW, True), FamilyMember(1, _NOW, False)))
        with self.assertRaises(OwnerRequired):
            self.family.transfer_ownership(1, home.id, 2)
        self.family.leave(1, home.id)
        self.assertIsNone(self.family.find_for_member(1))
        self.assertEqual(self._families(), [(home.id, 2)])

    def test_transfer_and_close_roll_back_if_a_write_fails(self) -> None:
        """Сбой записи нового владельца или каскадного удаления сохраняет семью."""
        home = self.family.create(1, "Дом")
        self._join(home.id, 2)
        with self.db:
            self.db.execute(
                "CREATE TRIGGER fail_transfer BEFORE UPDATE OF owner_app_user_id ON families "
                "BEGIN SELECT RAISE(ABORT, 'test write failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test write failure"):
            self.family.transfer_ownership(1, home.id, 2)
        self.assertEqual(self.family.find_for_member(1), home)
        self.assertEqual(len(self.family.list_members(1, home.id)), 2)

        self.family.leave(2, home.id)
        self._seed_family_data(home.id)
        with self.db:
            self.db.execute(
                "CREATE TRIGGER fail_close BEFORE DELETE ON family_shopping_items "
                "BEGIN SELECT RAISE(ABORT, 'test delete failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test delete failure"):
            self.family.close(1, home.id)
        self.assertEqual(self.family.find_for_member(1), home)
        self.assertEqual(self._family_data_counts(home.id), (1, 1))
        self.assertFalse(self.db.in_transaction)

    def test_close_rechecks_member_count_and_removes_only_its_own_data(self) -> None:
        """Закрытие отвергает изменившийся состав и чистит только выбранную семью."""
        home = self.family.create(1, "Дом")
        other = self.family.create(3, "Другая семья")
        self._seed_family_data(home.id)
        self._seed_family_data(other.id)
        self._join(home.id, 2)
        with self.assertRaises(FamilyHasOtherMembers):
            self.family.close(1, home.id)
        with self.assertRaises(OwnerRequired):
            self.family.close(2, home.id)
        with self.assertRaises(FamilyAccessEnded):
            self.family.close(1, other.id)
        self.assertEqual(self._family_data_counts(home.id), (1, 1))
        self.family.remove_member(1, home.id, 2)

        self.family.close(1, home.id)
        self.assertIsNone(self.family.find_for_member(1))
        self.assertEqual(self._families(), [(other.id, 3)])
        self.assertEqual(self._family_data_counts(home.id), (0, 0))
        self.assertEqual(self._family_data_counts(other.id), (1, 1))

    def _join(self, family_id: int, app_user_id: int) -> None:
        with self.db:
            self.db.execute("INSERT INTO family_members VALUES (?, ?, ?)", (family_id, app_user_id, _NOW.isoformat()))

    def _families(self) -> list[tuple]:
        return [tuple(row) for row in self.db.execute("SELECT id, owner_app_user_id FROM families ORDER BY id")]

    def _seed_family_data(self, family_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO family_invites (family_id, code_digest, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (family_id, str(family_id), _NOW.isoformat(), _NOW.isoformat()),
            )
            self.db.execute(
                "INSERT INTO family_shopping_items (family_id, number, name, status, created_at) "
                "VALUES (?, 1, 'Хлеб', 'needed', ?)", (family_id, _NOW.isoformat()),
            )

    def _family_data_counts(self, family_id: int) -> tuple[int, int]:
        invites = self.db.execute("SELECT COUNT(*) FROM family_invites WHERE family_id = ?", (family_id,)).fetchone()[0]
        items = self.db.execute("SELECT COUNT(*) FROM family_shopping_items WHERE family_id = ?", (family_id,)).fetchone()[0]
        return invites, items
