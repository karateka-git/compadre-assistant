"""Безопасные примитивы подготовки production-выпуска без переключения каналов.

Модуль не содержит исполняемой команды: остановка writers и последующий возврат
должны находиться в одной операции контроллера развёртывания.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Iterator, Mapping


SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
VERSION_PATTERN = re.compile(r"v[1-9][0-9]*\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
OCI_REVISION = "org.opencontainers.image.revision"
CHANNELS = ("tg_assistant", "vk_assistant")


class PreparationError(RuntimeError):
    """Обозначает отказ подготовки до изменения работающих каналов."""


def _run(*args: str, cwd: Path | None = None) -> str:
    """Запускает локальную команду и возвращает только её стандартный вывод."""
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        # stderr Git/Docker может содержать URL и иные данные окружения.
        raise PreparationError(f"Команда {args[0]} завершилась с кодом {result.returncode}")
    return result.stdout.strip()


def require_external_state_dir(state_dir: Path, checkout: Path) -> Path:
    """Проверяет, что состояние находится вне Git checkout.

    Args:
        state_dir: Каталог записи состояния и блокировки.
        checkout: Корень production checkout.

    Returns:
        Канонический путь каталога состояния.

    Raises:
        PreparationError: Состояние могло бы попасть в Git checkout.
    """
    target = state_dir.resolve()
    if target.is_relative_to(checkout.resolve()):
        raise PreparationError("Состояние выпуска должно находиться вне Git checkout")
    return target


@contextmanager
def deploy_lock(state_dir: Path, checkout: Path) -> Iterator[None]:
    """Удерживает межпроцессную блокировку на всё время выпуска.

    Lock-файл никогда не удаляется: удаление inode допускает второй lock.
    Предназначен для Linux VPS и требует запуска от доверенного владельца.
    """
    import fcntl

    directory = require_external_state_dir(state_dir, checkout)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd = os.open(directory / "deploy.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PreparationError("Уже выполняется другой выпуск") from error
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def require_clean_checkout(checkout: Path, expected_sha: str) -> None:
    """Требует чистый detached checkout точного SHA перед сборкой.

    Args:
        checkout: Корень production Git checkout.
        expected_sha: Полный проверенный SHA выпуска.

    Raises:
        PreparationError: Checkout грязный, привязан к ветке или имеет иной SHA.
    """
    if not SHA_PATTERN.fullmatch(expected_sha):
        raise PreparationError("Некорректный полный SHA")
    if _run("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=checkout):
        raise PreparationError("Production checkout содержит локальные изменения")
    branch = subprocess.run(
        ("git", "symbolic-ref", "--quiet", "--short", "HEAD"),
        cwd=checkout, text=True, capture_output=True, check=False,
    )
    if branch.returncode == 0:
        raise PreparationError("Production checkout должен быть в detached HEAD")
    if branch.returncode != 1:
        raise PreparationError("Не удалось подтвердить detached HEAD")
    if _run("git", "rev-parse", "HEAD", cwd=checkout) != expected_sha:
        raise PreparationError("HEAD не совпадает с проверенным SHA")


def require_exact_tag(checkout: Path, version: str, sha: str) -> None:
    """Сверяет локальный прямой commit tag с выбранным SHA после fetch."""
    if not VERSION_PATTERN.fullmatch(version) or not SHA_PATTERN.fullmatch(sha):
        raise PreparationError("Некорректная версия или SHA")
    target = _run("git", "cat-file", "-t", f"refs/tags/{version}", cwd=checkout)
    if target != "commit" or _run("git", "rev-parse", f"refs/tags/{version}", cwd=checkout) != sha:
        raise PreparationError("Локальный тег не указывает напрямую на выбранный commit")


def pinned_tag(sha: str) -> str:
    """Строит неизменяемое локальное имя образа для полного SHA."""
    if not SHA_PATTERN.fullmatch(sha):
        raise PreparationError("Некорректный полный SHA образа")
    return f"compadre-assistant:release-{sha}"


def build_pinned_image(checkout: Path, sha: str) -> tuple[str, str]:
    """Собирает образ чистого commit и проверяет фактический image ID/revision.

    Returns:
        Пару (локальный тег, фактический Docker image ID).
    """
    require_clean_checkout(checkout, sha)
    tag = pinned_tag(sha)
    _run("docker", "build", "--label", f"{OCI_REVISION}={sha}", "--tag", tag, ".", cwd=checkout)
    require_clean_checkout(checkout, sha)
    image = json.loads(_run("docker", "image", "inspect", tag, "--format", "{{json .}}"))
    image_id = image.get("Id")
    if (not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or
            image.get("Config", {}).get("Labels", {}).get(OCI_REVISION) != sha):
        raise PreparationError("Docker image ID или OCI revision не подтверждены")
    return tag, image_id


def check_candidate_image(image_id: str, sha: str) -> None:
    """Проверяет точный image ID и synthetic pipeline без сети и секретов."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or not SHA_PATTERN.fullmatch(sha):
        raise PreparationError("Некорректный image ID или SHA")
    image = json.loads(_run("docker", "image", "inspect", image_id, "--format", "{{json .}}"))
    if (image.get("Id") != image_id or
            image.get("Config", {}).get("Labels", {}).get(OCI_REVISION) != sha):
        raise PreparationError("Image ID или OCI revision candidate не подтверждены")
    for mode in ("--sqlite-smoke", "--pipeline-smoke", "--analysis-smoke"):
        _run(
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev", "--entrypoint", "python", image_id,
            "-m", "compadre_assistant", mode,
        )


def require_disk_space(path: Path, database: Path, *, reserve_bytes: int = 1024**3) -> None:
    """Отклоняет backup, если места меньше трёх размеров БД и резерва."""
    if not database.is_file():
        raise PreparationError("Production SQLite отсутствует")
    required = max(database.stat().st_size * 3, reserve_bytes)
    if shutil.disk_usage(path).free < required:
        raise PreparationError("Недостаточно свободного места для backup и миграции")


def _integrity(connection: sqlite3.Connection) -> None:
    """Требует успешный полный SQLite integrity_check."""
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise PreparationError("SQLite integrity_check завершился ошибкой")


def backup_sqlite(source: Path, destination: Path) -> Path:
    """Создаёт отдельную SQLite API копию без перезаписи и проверяет её.

    Вызывающий контроллер обязан предварительно остановить обоих writers,
    приостановить ночной backup timer и проверить свободное место.

    Raises:
        PreparationError: Исходная БД отсутствует, копия существует или повреждена.
    """
    if not source.is_file() or destination.exists():
        raise PreparationError("Исходная БД отсутствует или backup уже существует")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy)
                _integrity(copy)
        with destination.open("rb+") as file:
            os.fsync(file.fileno())
        return destination
    except (OSError, sqlite3.Error, PreparationError):
        destination.unlink(missing_ok=True)
        raise


def validate_schema(database: Path, expected_names: list[str]) -> None:
    """Проверяет integrity и точный список применённых миграций после --migrate."""
    if not database.is_file():
        raise PreparationError("Production SQLite отсутствует")
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        _integrity(connection)
        rows = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    if rows != list(enumerate(expected_names, start=1)):
        raise PreparationError("Схема БД не совпадает с миграциями candidate image")


def write_state(state_dir: Path, checkout: Path, state: Mapping[str, object]) -> None:
    """Атомарно сохраняет состояние выпуска вне checkout с правами 0600."""
    directory = require_external_state_dir(state_dir, checkout)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    payload = dict(state)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / "state.json")
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_state(state_dir: Path, checkout: Path) -> dict[str, object] | None:
    """Читает последнее атомарно записанное состояние или возвращает None."""
    path = require_external_state_dir(state_dir, checkout) / "state.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PreparationError("Некорректное сохранённое состояние выпуска")
    return value
