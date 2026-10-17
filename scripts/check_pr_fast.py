"""Проверяет tracked-файлы и неизменность миграций для быстрого CI PR."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


MIGRATIONS_DIR = "compadre_assistant/core/data/sqlite/migrations/"
MIGRATION_NAME = re.compile(r"(?P<version>\d{4})_[a-z0-9_]+\.sql\Z")
PRIVATE_KEY_MARKER = re.compile(
    rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)


def git_bytes(*arguments: str) -> bytes:
    """Возвращает вывод локального Git без обращения к сети."""
    return subprocess.run(
        ["git", *arguments], check=True, stdout=subprocess.PIPE
    ).stdout


def tracked_paths() -> list[str]:
    """Читает имена tracked-файлов, сохраняя пробелы и Unicode."""
    return [
        path.decode("utf-8", errors="surrogateescape")
        for path in git_bytes("ls-files", "-z").split(b"\0")
        if path
    ]


def forbidden_path(path: str) -> bool:
    """Определяет файлы секретов и SQLite по имени в любом каталоге."""
    name = Path(path).name.lower()
    if name == ".env.example":
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in {"id_rsa", "id_ed25519"}
        or name.endswith((
            ".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3",
            ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm",
            ".sqlite3-wal", ".sqlite3-shm",
        ))
    )


def check_tracked_files(paths: list[str]) -> list[str]:
    """Находит запрещённые пути и признаки приватных ключей в tracked-файлах."""
    errors: list[str] = []
    for path in paths:
        if forbidden_path(path):
            errors.append(f"Недопустимый tracked-файл: {path}")
            continue
        if PRIVATE_KEY_MARKER.search(Path(path).read_bytes()):
            errors.append(f"Приватный ключ в tracked-файле: {path}")
    return errors


def check_migrations(base_sha: str, paths: list[str]) -> list[str]:
    """Сверяет существующие SQL с базой PR и проверяет новые номера/имена."""
    errors: list[str] = []
    base_paths = {
        path.decode("utf-8", errors="surrogateescape")
        for path in git_bytes(
            "ls-tree", "-r", "-z", "--name-only", base_sha, "--", MIGRATIONS_DIR
        ).split(b"\0")
        if path and path.endswith(b".sql")
    }
    current_paths = {
        path for path in paths
        if path.startswith(MIGRATIONS_DIR) and Path(path).suffix.lower() == ".sql"
    }
    for path in sorted(base_paths):
        if path not in current_paths:
            errors.append(f"Удалена существующая миграция: {path}")
        elif git_bytes("show", f"{base_sha}:{path}") != git_bytes("show", f"HEAD:{path}"):
            errors.append(f"Изменена существующая миграция: {path}")

    versions: set[int] = set()
    for path in sorted(current_paths):
        match = MIGRATION_NAME.fullmatch(Path(path).name)
        if match is None:
            errors.append(f"Неверное имя миграции: {path}")
            continue
        version = int(match.group("version"))
        if version in versions:
            errors.append(f"Повторный номер миграции: {version:04d}")
        versions.add(version)
    if versions != set(range(1, max(versions, default=0) + 1)):
        errors.append("Номера миграций должны идти подряд от 0001")
    return errors


def main() -> int:
    """Печатает нарушения без содержимого файлов и возвращает ненулевой код."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", required=True)
    args = parser.parse_args()
    paths = tracked_paths()
    errors = check_tracked_files(paths) + check_migrations(args.base_sha, paths)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("Tracked-файлы и миграции проверены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
