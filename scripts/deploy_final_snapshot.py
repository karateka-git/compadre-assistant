"""Готовит окончательный снимок SQLite и отдельное поколение для переключения."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory
from typing import Callable

from scripts import deploy_prepare as prep
from scripts import predeploy_compatibility as compatibility


@dataclass(frozen=True)
class FinalSnapshot:
    """Доказательство сохранённого снимка прежнего поколения."""

    path: Path
    sha256: str


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _regular_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise compatibility.PredeployError("SAFE_FINAL_FILE_IDENTITY")


def _standalone(path: Path) -> None:
    _regular_file(path)
    # Переключение journal_mode выполняет checkpoint. Публиковать отдельно
    # main-файл можно только после успешного закрытия и без sidecars.
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise compatibility.PredeployError("SAFE_FINAL_CHECKPOINT")
    if any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise compatibility.PredeployError("SAFE_FINAL_SIDECARS")


def _publish(staged: Path, destination: Path, *, readonly: bool = False) -> None:
    if any(os.path.lexists(str(destination) + suffix) for suffix in ("", "-wal", "-shm", "-journal")):
        raise compatibility.PredeployError("SAFE_FINAL_NAME_COLLISION")
    with staged.open("rb+") as stream:
        if readonly:
            os.chmod(staged, 0o400)
        os.fsync(stream.fileno())
    try:
        if os.name == "posix":
            # link создаёт новое имя атомарно и никогда не затирает старый inode.
            # После crash до unlink кандидат с двумя именами не допускается.
            os.link(staged, destination)
            staged.unlink()
        else:
            os.rename(staged, destination)
    except FileExistsError as error:
        raise compatibility.PredeployError("SAFE_FINAL_NAME_COLLISION") from error
    _regular_file(destination)
    _sync_directory(destination.parent)
    _sync_directory(staged.parent)


def prepare(
    source: Path, state_dir: Path, generation: str, before: list[str], expected: list[str],
    fingerprint: str, *, require_quiescence: Callable[[], None],
    migrate: Callable[[Path], None], record_snapshot: Callable[[FinalSnapshot], None],
) -> Path:
    """Сохраняет проверенный снимок и готовит отдельный файл candidate.

    Вызывающий удерживает deploy lock, владеет каталогами и подтверждает
    выбор source. При отказе вызывающий возобновляет прежний runtime.

    Args:
        source: Канонический файл выбранного поколения в каталоге data.
        state_dir: Существующий приватный внешний каталог контроллера.
        generation: Новый уникальный идентификатор поколения, 32 hex-символа.
        before: Точный журнал применённых миграций старого поколения.
        expected: Ожидаемый журнал candidate.
        fingerprint: Ожидаемый fingerprint candidate из проверенного CI.
        require_quiescence: Проверяет marker, остановку всех writers и backup.
        migrate: Запускает доверенный candidate CLI, завершает контейнер до
            выхода и получает путь к SQLite в отдельном приватном каталоге.
        record_snapshot: Надёжно сохраняет путь и digest проверенного снимка.

    Returns:
        Путь готового candidate с уникальным basename в каталоге data.

    Raises:
        PredeployError: Подготовка или проверка отказала. Сохранённый снимок
            остаётся доступным для разбора.
    """
    with compatibility.check("SAFE_FINAL_PREPARATION"):
        if (not re.fullmatch(r"[0-9a-f]{32}", generation)
                or not source.is_absolute() or source.resolve() != source
                or not state_dir.is_absolute() or state_dir.resolve() != state_dir
                or source.is_relative_to(state_dir) or state_dir.is_relative_to(source.parent)):
            raise compatibility.PredeployError("SAFE_FINAL_PATH")
        require_quiescence()
        _regular_file(source)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(source) + suffix)
            if os.path.lexists(sidecar):
                _regular_file(sidecar)

        snapshot = state_dir / f"final-{generation}.db"
        candidate = source.parent / f"app-{generation}.db"
        if candidate == source:
            raise compatibility.PredeployError("SAFE_FINAL_NAME_COLLISION")
        # WAL может содержать больше данных, чем основной файл старого поколения.
        usage = source.stat().st_size
        wal = Path(str(source) + "-wal")
        if wal.exists():
            usage += wal.stat().st_size
        for directory in {state_dir, source.parent}:
            prep.require_disk_space(directory, source, reserve_bytes=max(1024**3, usage * 3))

        with TemporaryDirectory(prefix=".final-", dir=state_dir) as temporary:
            staged = prep.backup_sqlite(source, Path(temporary) / snapshot.name)
            _standalone(staged)
            prep.validate_schema(staged, before)
            with closing(compatibility.open_copy_readonly(staged)) as connection:
                compatibility.require_copy_foreign_keys(connection)
            with staged.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            # Снимок никогда не монтируется в мигратор и не открывается для записи.
            _publish(staged, snapshot, readonly=True)
        record_snapshot(FinalSnapshot(snapshot, digest))

        with TemporaryDirectory(prefix=".candidate-", dir=source.parent) as temporary:
            staged = prep.backup_sqlite(snapshot, Path(temporary) / candidate.name)
            with compatibility.check("SAFE_FINAL_MIGRATION"):
                migrate(staged)
            _standalone(staged)
            compatibility.verify_migrated_copy(snapshot, staged, expected, fingerprint)
            require_quiescence()
            _publish(staged, candidate)
        return candidate
