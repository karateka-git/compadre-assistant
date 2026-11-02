"""Закрытый контракт запуска прежнего runtime на аттестованной новой схеме.

Файлы допуска и launch context предоставляет доверенный контроллер read-only.
Этот модуль проверяет соответствие, но не выдаёт и не аутентифицирует допуск.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .migration_runner import (
    MIGRATIONS_DIR, Migration, MigrationError, _discover_migrations, apply_migrations,
)

# Пути доступны только в специально собранном контроллером runtime-запуске.
ADMISSION_ENV = "COMPADRE_SCHEMA_ADMISSION"
LAUNCH_ENV = "COMPADRE_SCHEMA_LAUNCH_CONTEXT"
_SHA = re.compile(r"[0-9a-f]{40}")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTITY_FIELDS = {"base_sha", "base_image_id", "candidate_sha", "candidate_image_id", "controller_sha", "verification_id"}
_ADMISSION_FIELDS = _IDENTITY_FIELDS | {"format_version", "base_journal", "target_journal", "base_sql_sha256", "schema_sha256"}


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError("Duplicate compatibility field")
        result[key] = value
    return result


def _read_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
    except (OSError, ValueError) as error:
        raise MigrationError("Could not read compatibility contract") from error
    if not isinstance(value, dict):
        raise MigrationError("Compatibility contract must be an object")
    return value


def _journal(value: object) -> list:
    if not isinstance(value, list) or not value:
        raise MigrationError("Compatibility journal must be nonempty")
    for index, row in enumerate(value, 1):
        if (not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int
                or row[0] != index or not isinstance(row[1], str)
                or re.fullmatch(rf"{index:04d}_[a-z0-9_]+\.sql", row[1]) is None):
            raise MigrationError("Invalid compatibility journal")
    return value


def validate_release_pair(admission: dict, launch_context: dict) -> None:
    """Сверяет контракт с независимо проверенным контроллером контекстом пары.

    Args:
        admission: Декодированный допуск формата 1 после успешной аттестации.
        launch_context: Точные идентичности образов и проверки от контроллера.

    Raises:
        MigrationError: Формат, идентичности или история не совпадают.

    Контроллер обязан сам проверить Docker ID/revision и происхождение проверки.
    Сравнение JSON не заменяет эту доверенную границу и не разрешает выдачу допуска.
    """
    if not isinstance(admission, dict) or set(admission) != _ADMISSION_FIELDS:
        raise MigrationError("Invalid admission fields")
    if not isinstance(launch_context, dict) or set(launch_context) != _IDENTITY_FIELDS:
        raise MigrationError("Invalid launch context fields")
    if type(admission["format_version"]) is not int or admission["format_version"] != 1:
        raise MigrationError("Unsupported admission format")
    for key in _IDENTITY_FIELDS:
        value = admission[key]
        pattern = _IMAGE if key.endswith("image_id") else _SHA
        if key == "verification_id":
            pattern = _DIGEST
        if not isinstance(value, str) or not pattern.fullmatch(value) or launch_context[key] != value:
            raise MigrationError("Compatibility release pair does not match")
    for key in ("schema_sha256", "base_sql_sha256"):
        if not isinstance(admission[key], str) or not _DIGEST.fullmatch(admission[key]):
            raise MigrationError("Invalid compatibility digest")
    base = _journal(admission["base_journal"])
    target = _journal(admission["target_journal"])
    if len(target) <= len(base) or target[:len(base)] != base:
        raise MigrationError("Target must strictly extend the complete base journal")


def schema_fingerprint(connection: sqlite3.Connection) -> str:
    """Хеширует все записи sqlite_schema без rootpage и пользовательских данных.

    Включает внутренние индексы, sqlite_sequence, статистические и FTS-объекты;
    SQL сохраняется дословно. Вызывающий удерживает согласованную транзакцию.
    """
    rows = [list(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name, tbl_name"
    )]
    return _digest(rows)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def migration_sql_fingerprint(migrations_dir: Path = MIGRATIONS_DIR) -> str:
    """Хеширует упорядоченные имена и SHA-256 точных байтов базовых SQL-файлов."""
    return _digest([[item.version, item.name, hashlib.sha256(item.path.read_bytes()).hexdigest()]
                    for item in _discover_migrations(migrations_dir)])


def validate_compatible_schema(
    connection: sqlite3.Connection, admission: dict, launch_context: dict,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[Migration]:
    """Проверяет допуск новой схемы без DDL, SQL миграций и записи журнала.

    Args:
        connection: Соединение вне пользовательской транзакции.
        admission: Полный допуск конкретной пары и схемы.
        launch_context: Независимый доверенный контекст запуска контроллера.
        migrations_dir: Неизменяемые SQL-файлы базового образа.

    Returns:
        Пустой список: этот режим никогда не применяет миграции.

    Raises:
        MigrationError: Не совпадает контракт, локальная история или SQLite-схема.
    """
    validate_release_pair(admission, launch_context)
    migrations = _discover_migrations(migrations_dir)
    if ([[item.version, item.name] for item in migrations] != admission["base_journal"]
            or migration_sql_fingerprint(migrations_dir) != admission["base_sql_sha256"]):
        raise MigrationError("Base migration files do not match admission")
    if connection.in_transaction:
        raise MigrationError("Compatibility validation requires its own transaction")
    try:
        # IMMEDIATE блокирует concurrent DDL/журнал до окончания обеих проверок.
        connection.execute("BEGIN IMMEDIATE")
        rows = [list(row) for row in connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )]
        if rows != admission["target_journal"]:
            raise MigrationError("Target journal does not match admission")
        if schema_fingerprint(connection) != admission["schema_sha256"]:
            raise MigrationError("Target schema does not match admission")
    except sqlite3.Error as error:
        raise MigrationError("Could not validate compatible schema") from error
    finally:
        if connection.in_transaction:
            connection.rollback()
    return []


def prepare_runtime_schema(connection: sqlite3.Connection) -> list[Migration]:
    """Выбирает строгие миграции либо проверку двух файлов доверенного запуска.

    Неполный или повреждённый контекст закрывает запуск без fallback к миграциям.
    Отдельная команда --migrate эту функцию не вызывает.
    """
    admission_path = os.environ.get(ADMISSION_ENV)
    launch_path = os.environ.get(LAUNCH_ENV)
    if admission_path is None and launch_path is None:
        return apply_migrations(connection)
    if not admission_path or not launch_path:
        raise MigrationError("Both compatibility files are required")
    return validate_compatible_schema(connection, _read_json(admission_path), _read_json(launch_path))
