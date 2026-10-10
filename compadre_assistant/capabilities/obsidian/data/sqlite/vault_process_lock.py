"""Межпроцессное исключение внешней записи одного vault без блокировки всей БД."""

from __future__ import annotations

from contextlib import contextmanager
import errno
from pathlib import Path
import sqlite3
import sys
import time
from typing import Iterator


@contextmanager
def vault_process_lock(
    connection: sqlite3.Connection, vault_id: int, *, blocking: bool = True
) -> Iterator[bool]:
    """Удерживает OS lock конкретного vault между процессами Telegram/VK.

    Lock-файл находится рядом с SQLite-БД, поэтому переживает перезапуск как
    имя ресурса; сам OS lock освобождается при crash. Для in-memory SQLite
    межпроцессного ресурса нет.

    Args:
        connection: Соединение с общей SQLite-БД.
        vault_id: Устойчивый ID vault, определяющий отдельный lock-файл.
        blocking: Ждать занятый lock либо сразу вернуть `False`.

    Yields:
        `True` при полученном lock; `False` при занятом неблокирующем lock.

    Raises:
        ValueError: Идентификатор vault некорректен.
    """
    if vault_id <= 0:
        raise ValueError("vault_id must be positive")
    database = next(
        (row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"),
        "",
    )
    if not database:
        yield True
        return
    database_path = Path(database).resolve()
    path = database_path.with_name(f"{database_path.name}.vault-{vault_id}.lock")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if not blocking:
                        yield False
                        return
                    time.sleep(0.05)
            try:
                yield True
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(
                    stream.fileno(),
                    fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
                )
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
