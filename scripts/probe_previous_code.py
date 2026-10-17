"""Проверяет чтение и запись прежним кодом после обновления тестовой БД."""

from __future__ import annotations

from pathlib import Path
import sys

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository


def main(database: Path) -> None:
    """Читает и меняет только помеченную искусственную строку.

    Args:
        database: Путь к временной БД, подготовленной полным CI.

    Raises:
        AssertionError: Прежний repository не может работать с новой схемой.
    """
    with connect_database(database) as connection:
        row = connection.execute(
            "SELECT id FROM app_users WHERE display_name = ?", ("CI fixture",)
        ).fetchone()
        if row is None:
            raise AssertionError("previous fixture row is absent")
        repository = SQLiteAppUserRepository(connection)
        user = repository.get_by_id(row[0])
        if user is None or user.display_name != "CI fixture":
            raise AssertionError("previous code could not read the fixture row")
        changed = repository.update_display_name(
            app_user_id=row[0], display_name="CI fixture read by previous code",
        )
        if changed.display_name != "CI fixture read by previous code":
            raise AssertionError("previous code could not update the fixture row")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
