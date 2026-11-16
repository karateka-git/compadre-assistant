"""Поведенческие тесты сценариев приглашений на мигрированной SQLite."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.domain.invitations import (
    InvalidInvitation, InvitationExpired, InvitationUnavailable, InvitationUsed, ManageInvitations,
)
from compadre_assistant.capabilities.family.domain.membership import AlreadyInFamily, FamilyAccessEnded
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations


_NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)  # Детерминированное начало интервала действия кода.
_FIRST_CODE = "KMPTKMPTKMPTKMPT"  # Детерминированный код для проверки коллизии.
_SECOND_CODE = "DHRWDHRWDHRWDHRW"  # Следующая попытка выдачи после коллизии.


class SQLiteFamilyInvitationRepositoryTest(unittest.TestCase):
    """Проверяет выдачу, вступление и конкуренцию на реальной SQLite."""

    def setUp(self) -> None:
        """Открывает временную БД с двумя семьями и свободными профилями."""
        self.path = Path(self.enterContext(TemporaryDirectory(prefix="family-invitations-"))) / "family.db"
        self.db = self.enterContext(connect_database(self.path))
        apply_migrations(self.db)
        with self.db:
            self.db.execute("INSERT INTO app_users (id) VALUES (1), (2), (3), (4), (5)")
        self.repository = SQLiteFamilyRepository(self.db)
        self.home = self.repository.create(1, "Дом", _NOW)
        self.other = self.repository.create(5, "Другая семья", _NOW)
        self.now = _NOW
        self.invites = ManageInvitations(self.repository, clock=lambda: self.now)

    def test_non_owner_issues_in_own_family_and_only_digest_is_saved(self) -> None:
        """Действующий участник может пригласить, а чужой профиль и семья отклоняются."""
        initial = self.invites.issue(1, self.home.id)
        self.assertEqual(self.invites.join(2, initial.code), self.home)
        self.now = datetime(2026, 11, 16, 18, tzinfo=timezone(timedelta(hours=6)))
        issued = self.invites.issue(2, self.home.id)
        self.assertEqual(issued.family, self.home)
        self.assertRegex(issued.code, r"^[2-9A-HJ-NP-Z]{4}(?:-[2-9A-HJ-NP-Z]{4}){3}$")
        self.assertEqual(issued.expires_at, _NOW + timedelta(hours=24))
        self.assertEqual(issued.expires_at.tzinfo, UTC)
        row = self.db.execute(
            "SELECT family_id, code_digest, created_at, expires_at, used_at FROM family_invites "
            "WHERE created_by_app_user_id = 2"
        ).fetchone()
        canonical = issued.code.replace("-", "")
        self.assertEqual(tuple(row), (
            self.home.id, sha256(canonical.encode("ascii")).hexdigest(), _NOW.isoformat(),
            issued.expires_at.isoformat(), None,
        ))
        with self.assertRaises(FamilyAccessEnded):
            self.invites.issue(2, self.other.id)
        self.repository.remove_member(1, self.home.id, 2)
        with self.assertRaises(FamilyAccessEnded):
            self.invites.issue(2, self.home.id)
        with self.db:
            self.db.execute("DELETE FROM app_users WHERE id = 2")
        self.assertEqual(self.invites.join(3, canonical.lower()), self.home)
        self.assertEqual(self.repository.find_for_member(3), self.home)
        self.assertEqual(self._invite_count(), 2)

    def test_join_distinguishes_invalid_used_and_expired_at_boundaries(self) -> None:
        """Код действует до точной границы 24 часов, а погашенный не меняет статус."""
        soon_used = self.invites.issue(1, self.home.id)
        soon_expired = self.invites.issue(1, self.home.id)
        for invalid in ("", "ends soon", "QQQQ-QQQQ-QQQQ-QQQQ", "ＫＭＰＴ" * 4):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidInvitation):
                self.invites.join(2, invalid)
        self.now = _NOW - timedelta(microseconds=1)
        with self.assertRaises(InvalidInvitation):
            self.invites.join(2, soon_used.code)
        self.now = soon_used.expires_at - timedelta(microseconds=1)
        self.assertEqual(self.invites.join(2, " " + soon_used.code + " "), self.home)
        self.now = soon_expired.expires_at
        with self.assertRaises(InvitationExpired):
            self.invites.join(3, soon_expired.code)
        with self.assertRaises(InvitationUsed):
            self.invites.join(3, soon_used.code)
        self.assertIsNone(self.repository.find_for_member(3))
        self.assertIsNone(self._used_at(soon_expired.code))

    def test_failed_join_preserves_code_and_family(self) -> None:
        """Существующее членство и сбой вставки не погашают действующее приглашение."""
        issued = self.invites.issue(1, self.home.id)
        with self.assertRaises(AlreadyInFamily):
            self.invites.join(5, issued.code)
        self.assertEqual(self.repository.find_for_member(5), self.other)
        self.assertIsNone(self._used_at(issued.code))
        with self.assertRaises(sqlite3.IntegrityError):
            self.invites.join(900, issued.code)
        self.assertIsNone(self._used_at(issued.code))
        self.assertFalse(self.db.in_transaction)

        self.assertEqual(self.invites.join(2, issued.code), self.home)
        self.assertEqual(self._used_at(issued.code), _NOW.isoformat())

    def test_issue_retries_code_collision_without_overwriting_the_first_invite(self) -> None:
        """Глобальная коллизия проверочного значения не меняет судьбу первого кода."""
        codes = iter((_FIRST_CODE, _FIRST_CODE, _SECOND_CODE))
        invites = ManageInvitations(self.repository, clock=lambda: self.now, code_factory=lambda: next(codes))
        first = invites.issue(1, self.home.id)
        second = invites.issue(5, self.other.id)
        self.assertNotEqual(first.code, second.code)
        self.assertEqual(self.invites.join(2, first.code), self.home)
        self.assertEqual(self.invites.join(3, second.code), self.other)
        self.assertEqual(self._invite_count(), 2)
        duplicate = ManageInvitations(self.repository, clock=lambda: self.now, code_factory=lambda: _FIRST_CODE)
        with self.assertRaises(InvitationUnavailable):
            duplicate.issue(1, self.home.id)
        self.assertEqual(self._invite_count(), 2)

    def test_close_invalidates_invitation(self) -> None:
        """Любой оставшийся код закрытой семьи прекращает действие."""
        issued = self.invites.issue(1, self.home.id)
        self.repository.close(1, self.home.id)
        with self.assertRaises(InvalidInvitation):
            self.invites.join(2, issued.code)
        self.assertIsNone(self.repository.find_for_member(2))
        self.assertEqual(self._invite_count(), 0)

    def test_two_connections_can_consume_invitation_only_once(self) -> None:
        """Два профиля в одной гонке получают одного участника и один отказ."""
        code = self.invites.issue(1, self.home.id).code
        ready = Barrier(2, timeout=5)

        def join(app_user_id: int) -> bool:
            with connect_database(self.path) as db:
                invites = ManageInvitations(SQLiteFamilyRepository(db), clock=lambda: _NOW)
                ready.wait()
                try:
                    invites.join(app_user_id, code)
                except InvitationUsed:
                    return False
                return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(join, (2, 3)))
        self.assertCountEqual(outcomes, (True, False))
        joined = 2 if outcomes[0] else 3
        self.assertEqual([member.app_user_id for member in self.repository.list_members(1, self.home.id)], [1, joined])
        self.assertEqual(self._used_at(code), _NOW.isoformat())

    def _invite_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM family_invites").fetchone()[0]

    def _used_at(self, code: str) -> str | None:
        digest = sha256(code.replace("-", "").encode("ascii")).hexdigest()
        return self.db.execute("SELECT used_at FROM family_invites WHERE code_digest = ?", (digest,)).fetchone()[0]
