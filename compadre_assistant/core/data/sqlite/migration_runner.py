from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_NAME_PATTERN = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """Сообщает об ошибке обнаружения или применения SQLite-схемы."""


@dataclass(frozen=True)
class Migration:
    """Описывает версию и неизменяемый SQL-файл изменения схемы."""
    version: int
    name: str
    path: Path


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[Migration]:
    """Применяет ещё не записанные версии схемы с межпроцессной проверкой.

    Args:
        connection: Соединение с общей SQLite-БД.
        migrations_dir: Каталог SQL-файлов схемы.

    Returns:
        Список применённых в этом вызове файлов.

    Raises:
        MigrationError: Обнаружена некорректная схема или ошибка SQLite.
    """
    migrations = _discover_migrations(migrations_dir)
    _ensure_migrations_table(connection)
    applied = _get_applied_migrations(connection)
    _validate_applied_migrations(migrations, applied)
    completed: list[Migration] = []

    for migration in migrations:
        applied_name = applied.get(migration.version)
        if applied_name is not None:
            if applied_name != migration.name:
                raise MigrationError(
                    f"Migration version {migration.version:04d} is already recorded "
                    f"as {applied_name}, not {migration.name}"
                )
            continue

        if _apply_migration(connection, migration):
            completed.append(migration)

    return completed


def _discover_migrations(migrations_dir: Path) -> list[Migration]:
    if not migrations_dir.is_dir():
        raise MigrationError(f"Migrations directory does not exist: {migrations_dir}")

    migrations: list[Migration] = []
    versions: set[int] = set()

    for path in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_NAME_PATTERN.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"Invalid migration file name: {path.name}")

        version = int(match.group("version"))
        if version in versions:
            raise MigrationError(f"Duplicate migration version: {version:04d}")

        versions.add(version)
        migrations.append(Migration(version=version, name=path.name, path=path))

    ordered = sorted(migrations, key=lambda migration: migration.version)
    if not ordered or [migration.version for migration in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        raise MigrationError("Migration versions must be consecutive starting at 0001")
    return ordered


def _validate_applied_migrations(
    migrations: list[Migration], applied: dict[int, str],
) -> None:
    """Отклоняет неизвестную схему и пропуски до выполнения нового SQL."""
    available = {migration.version: migration.name for migration in migrations}
    for version, name in applied.items():
        if available.get(version) != name:
            raise MigrationError(
                f"Applied migration {version:04d}:{name} does not match available files"
            )
    if sorted(applied) != list(range(1, len(applied) + 1)):
        raise MigrationError("Applied migrations must form a consecutive prefix from 0001")


def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()


def _get_applied_migrations(connection: sqlite3.Connection) -> dict[int, str]:
    rows = connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _apply_migration(
    connection: sqlite3.Connection,
    migration: Migration,
) -> bool:
    sql = migration.path.read_text(encoding="utf-8")

    try:
        connection.execute("BEGIN IMMEDIATE")
        applied = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = ?",
            (migration.version,),
        ).fetchone()
        if applied is not None:
            if applied[0] != migration.name:
                raise MigrationError(
                    f"Migration version {migration.version:04d} is already recorded "
                    f"as {applied[0]}, not {migration.name}"
                )
            connection.commit()
            return False

        for statement in _split_sql_statements(sql):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (migration.version, migration.name),
        )
        connection.commit()
        return True
    except MigrationError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise MigrationError(
            f"Could not apply migration {migration.name}: {error}"
        ) from error


def _split_sql_statements(sql: str) -> list[str]:
    """Разделяет SQL script на полные statements без потери транзакционности."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buffer.clear()

    remainder = "".join(buffer).strip()
    if remainder:
        raise MigrationError("Migration SQL ends with an incomplete statement")
    return statements
