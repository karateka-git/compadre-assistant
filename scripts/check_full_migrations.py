"""Проверяет текущие миграции на новых и искусственных прежних SQLite-БД."""

from __future__ import annotations

import argparse
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import (
    MIGRATIONS_DIR, apply_migrations,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_PATH = "compadre_assistant/core/data/sqlite/migrations"
PROBE_PATH = Path(__file__).with_name("probe_previous_code.py")


def git_output(*arguments: str) -> bytes:
    """Возвращает байты локального Git или завершает проверку при ошибке."""
    return subprocess.run(
        ["git", *arguments], cwd=PROJECT_ROOT, check=True, capture_output=True,
    ).stdout


def previous_migrations(previous_sha: str) -> dict[str, bytes]:
    """Читает SQL предыдущего выпуска из точного Git commit.

    Args:
        previous_sha: Полный SHA ранее развёрнутого или опубликованного commit.

    Returns:
        Имена и содержимое SQL-файлов предыдущего выпуска.

    Raises:
        ValueError: Ссылка не является полным SHA commit или схема отсутствует.
    """
    if len(previous_sha) != 40 or any(char not in "0123456789abcdef" for char in previous_sha):
        raise ValueError("previous SHA must be a full lowercase commit SHA")
    resolved = git_output("rev-parse", "--verify", f"{previous_sha}^{{commit}}").decode().strip()
    if resolved != previous_sha:
        raise ValueError("previous SHA does not identify the requested commit")
    names = git_output("ls-tree", "-r", "--name-only", previous_sha, "--", MIGRATIONS_PATH)
    paths = [name.decode() for name in names.splitlines() if name.endswith(b".sql")]
    if not paths:
        raise ValueError("previous commit has no SQLite migrations")
    return {Path(path).name: git_output("show", f"{previous_sha}:{path}") for path in paths}


def assert_database(connection: sqlite3.Connection, expected_names: list[str]) -> None:
    """Проверяет точный журнал миграций и целостность тестовой БД."""
    recorded = [
        row[0] for row in connection.execute(
            "SELECT name FROM schema_migrations ORDER BY version"
        )
    ]
    if recorded != expected_names:
        raise AssertionError(f"unexpected migration journal: {recorded}")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise AssertionError("SQLite integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise AssertionError("SQLite foreign_key_check failed")


def run_previous_code(previous_sha: str, database: Path, root: Path) -> None:
    """Запускает read/write probe прежнего кода на обновлённой тестовой БД.

    Args:
        previous_sha: Полный SHA прежнего кода.
        database: Обновлённая временная SQLite-БД.
        root: Временный каталог для архивированного исходного кода.
    """
    archive = git_output("archive", "--format=tar", previous_sha, "compadre_assistant", "document_chunker")
    checkout = root / "previous-code"
    checkout.mkdir()
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as source:
        source.extractall(checkout, filter="data")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(checkout)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(
        [sys.executable, str(PROBE_PATH), str(database)],
        cwd=checkout, env=env, check=True, timeout=30,
    )


def check_synthetic_pending_migration(root: Path, current_paths: list[Path]) -> None:
    """Проверяет реальный путь pending SQL через временную следующую версию.

    Args:
        root: Временный каталог проверки.
        current_paths: SQL-файлы candidate в порядке применения.
    """
    directory = root / "synthetic-migrations"
    directory.mkdir()
    for path in current_paths:
        (directory / path.name).write_bytes(path.read_bytes())
    database = root / "synthetic.db"
    with connect_database(database) as connection:
        apply_migrations(connection, directory)
        connection.execute("INSERT INTO app_users (display_name) VALUES (?)", ("Synthetic fixture",))
        connection.commit()
    version = len(current_paths) + 1
    synthetic_name = f"{version:04d}_ci_probe.sql"
    (directory / synthetic_name).write_text(
        "ALTER TABLE app_users ADD COLUMN ci_probe INTEGER NOT NULL DEFAULT 0;\n"
        "UPDATE app_users SET ci_probe = 7 WHERE display_name = 'Synthetic fixture';\n",
        encoding="utf-8",
    )
    with connect_database(database) as connection:
        completed = apply_migrations(connection, directory)
        if [item.name for item in completed] != [synthetic_name]:
            raise AssertionError("synthetic pending migration was not applied once")
        assert_database(connection, [path.name for path in current_paths] + [synthetic_name])
        row = connection.execute(
            "SELECT display_name, ci_probe FROM app_users WHERE display_name = ?",
            ("Synthetic fixture",),
        ).fetchone()
        if row is None or tuple(row) != ("Synthetic fixture", 7):
            raise AssertionError("synthetic pending migration lost fixture data")
        if apply_migrations(connection, directory):
            raise AssertionError("synthetic pending migration is not idempotent")


def check_migrations(previous_sha: str) -> None:
    """Проверяет оба пути создания БД и сохранение искусственных строк.

    Args:
        previous_sha: Полный SHA предыдущего опубликованного выпуска.

    Raises:
        AssertionError: Нарушен журнал, сохранность данных или целостность.
        ValueError: История SQL не совпадает с предыдущим выпуском.
    """
    previous = previous_migrations(previous_sha)
    current_paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    current_names = [path.name for path in current_paths]
    previous_names = sorted(previous)
    if current_names[:len(previous_names)] != previous_names:
        raise ValueError("previous migrations are not a prefix of current migrations")
    for name, sql in previous.items():
        # На Windows checkout может использовать CRLF; Git сравнивает эти SQL
        # после нормализации строк, поэтому физический формат не должен мешать.
        if (MIGRATIONS_DIR / name).read_bytes().replace(b"\r\n", b"\n") != sql.replace(b"\r\n", b"\n"):
            raise ValueError(f"previous migration changed: {name}")

    with TemporaryDirectory(prefix="full-migrations-") as temporary:
        root = Path(temporary)
        fresh_database = root / "fresh.db"
        with connect_database(fresh_database) as connection:
            apply_migrations(connection)
            assert_database(connection, current_names)
            if apply_migrations(connection):
                raise AssertionError("fresh database migrations are not idempotent")

        previous_directory = root / "previous-migrations"
        previous_directory.mkdir()
        for name, sql in previous.items():
            (previous_directory / name).write_bytes(sql)
        upgraded_database = root / "upgraded.db"
        with connect_database(upgraded_database) as connection:
            apply_migrations(connection, previous_directory)
            assert_database(connection, previous_names)
            connection.execute("INSERT INTO app_users (display_name) VALUES (?)", ("CI fixture",))
            connection.commit()
            fixture_id = connection.execute("SELECT id FROM app_users WHERE display_name = ?", ("CI fixture",)).fetchone()[0]

        with connect_database(upgraded_database) as connection:
            completed = apply_migrations(connection)
            if [item.name for item in completed] != current_names[len(previous_names):]:
                raise AssertionError("pending migration sequence differs from candidate")
            assert_database(connection, current_names)
            row = connection.execute("SELECT display_name FROM app_users WHERE id = ?", (fixture_id,)).fetchone()
            if row is None or row[0] != "CI fixture":
                raise AssertionError("previous fixture row was not preserved")
            if apply_migrations(connection):
                raise AssertionError("upgraded database migrations are not idempotent")
        run_previous_code(previous_sha, upgraded_database, root)
        with connect_database(upgraded_database) as connection:
            assert_database(connection, current_names)
            row = connection.execute("SELECT display_name FROM app_users WHERE id = ?", (fixture_id,)).fetchone()
            if row is None or row[0] != "CI fixture read by previous code":
                raise AssertionError("previous code could not update the fixture row")
        check_synthetic_pending_migration(root, current_paths)
    print(f"Migration checks passed: fresh, upgraded, previous code {previous_sha}")


def main() -> int:
    """Читает SHA предыдущего выпуска и возвращает код результата."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-sha", required=True)
    args = parser.parse_args()
    check_migrations(args.previous_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
