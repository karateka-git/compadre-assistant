"""Собирает семейные сценарии с отдельным соединением на обработку события."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.data.sqlite_expenses import SQLiteExpenseRepository
from compadre_assistant.capabilities.family.domain.expenses import ExpenseRepository
from compadre_assistant.capabilities.family.presentation.expense_entry import ExpenseEntry
from compadre_assistant.capabilities.family.data.sqlite_shopping_batches import SQLiteShoppingBatchRepository
from compadre_assistant.capabilities.family.domain.shopping_notifications import ShoppingBatchRepository
from compadre_assistant.capabilities.family.presentation.notifications import ShoppingNotifications
from compadre_assistant.capabilities.family.domain.invitations import ManageInvitations
from compadre_assistant.capabilities.family.domain.manage_family import ManageFamily
from compadre_assistant.capabilities.family.domain.recognition import ExpenseRecognizer
from compadre_assistant.capabilities.family.domain.shopping import ManageShopping
from compadre_assistant.capabilities.family.presentation.handler import FamilyCapability
from compadre_assistant.capabilities.family.presentation.members import MemberDisplayNames
from compadre_assistant.core.data.sqlite.connection import connect_database


def create_family_capability(database_path: Path, display_names: MemberDisplayNames,
                             recognizer: ExpenseRecognizer | None = None) -> FamilyCapability:
    """Собирает семейный обработчик без удержания SQLite между событиями.

    Args:
        database_path: Общая база с применёнными миграциями.
        display_names: Публичное чтение имён проверенных участников.
        recognizer: Порт распознавания чеков и текста.

    Returns:
        Обработчик семейного режима.
    """

    @contextmanager
    def open_scenarios() -> Iterator[tuple[ManageFamily, ManageInvitations, ManageShopping]]:
        with connect_database(database_path) as connection:
            repository = SQLiteFamilyRepository(connection)
            yield ManageFamily(repository), ManageInvitations(repository), ManageShopping(repository)

    @contextmanager
    def expenses() -> Iterator[ExpenseRepository]:
        with connect_database(database_path) as connection:
            yield SQLiteExpenseRepository(connection)

    return FamilyCapability(open_scenarios, display_names, expense_entry=ExpenseEntry(expenses, recognizer))


def create_shopping_notifications(database_path: Path, names: MemberDisplayNames) -> ShoppingNotifications:
    """Собирает источник сводок с отдельным соединением на цикл проверки."""
    @contextmanager
    def open_queue() -> Iterator[ShoppingBatchRepository]:
        with connect_database(database_path) as connection:
            yield SQLiteShoppingBatchRepository(connection)

    return ShoppingNotifications(open_queue, names)
