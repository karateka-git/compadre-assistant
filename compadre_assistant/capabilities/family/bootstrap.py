"""Собирает семейные сценарии с отдельным соединением на обработку события."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.domain.invitations import ManageInvitations
from compadre_assistant.capabilities.family.domain.manage_family import ManageFamily
from compadre_assistant.capabilities.family.presentation.handler import FamilyCapability
from compadre_assistant.capabilities.family.presentation.members import MemberDisplayNames
from compadre_assistant.core.data.sqlite.connection import connect_database


def create_family_capability(database_path: Path, display_names: MemberDisplayNames) -> FamilyCapability:
    """Собирает семейный обработчик без удержания SQLite между событиями.

    Args:
        database_path: Общая база с применёнными миграциями.
        display_names: Публичное чтение имён проверенных участников.

    Returns:
        Обработчик семейного режима.
    """

    @contextmanager
    def open_scenarios() -> Iterator[tuple[ManageFamily, ManageInvitations]]:
        with connect_database(database_path) as connection:
            repository = SQLiteFamilyRepository(connection)
            yield ManageFamily(repository), ManageInvitations(repository)

    return FamilyCapability(open_scenarios, display_names)
