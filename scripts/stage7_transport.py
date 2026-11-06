"""Закрывает внешние операции проверочного процесса этапа 7.

Устанавливать guard в отдельном процессе до исполнения проверяемого controller.
Guard нельзя снять; это защита от случайного обращения доверенного Python-кода,
а не sandbox для вредоносного native-кода. Реальный мигратор здесь не разрешён.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import parse_qs, unquote, urlsplit


class IsolationError(PermissionError):
    """Запрещает действие вне приватного контура без вывода аргументов команды."""


def install_guard(private_root: Path) -> None:
    """Запрещает процессы, сеть и файловые изменения вне приватного каталога.

    Args:
        private_root: Существующий отдельный каталог копий, не production-путь.

    Raises:
        IsolationError: Корень небезопасен либо запрещённая операция обнаружена.

    Note:
        Допускаются только descriptors, открытые после установки через
        проверенный os.open. dir_fd, symlink и неизвестные descriptors запрещены.
    """
    root = private_root.absolute()
    protected = tuple(Path(p).absolute().resolve() for p in (
        "/opt/obs-chat-bot", "/var/lib/compadre-assistant-deploy",
        "/usr/local/lib/compadre-assistant-deploy", "/usr/local/sbin/deploy-prod",
        "/etc/systemd", "/run/systemd", "/run/docker.sock", "/var/run/docker.sock",
    ))
    if (not root.is_dir() or root.resolve() != root or
            any(root == p or root.is_relative_to(p) or p.is_relative_to(root) for p in protected)):
        raise IsolationError("STAGE7_UNSAFE_ROOT")
    if os.name == "posix" and (root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077):
        raise IsolationError("STAGE7_PRIVATE_ROOT_REQUIRED")
    original_open, original_close = os.open, os.close
    descriptors = {}

    def check_descriptor(fd):
        saved = descriptors.get(fd)
        if saved is None:
            raise IsolationError("STAGE7_FILE_DESCRIPTOR")
        path, identity = saved
        current = os.fstat(fd)
        info = path.stat()
        if (current.st_dev, current.st_ino) != identity or (info.st_dev, info.st_ino) != identity:
            raise IsolationError("STAGE7_REUSED_DESCRIPTOR")
        check_path(path, writing=True)
        return path

    def check_path(value, *, writing=False, unlinking=False):
        if isinstance(value, int):
            check_descriptor(value)
            return
        path = Path(os.fsdecode(value)).absolute().resolve()
        if any(path == p or path.is_relative_to(p) for p in protected):
            raise IsolationError("STAGE7_PRODUCTION_PATH")
        if writing and not path.is_relative_to(root):
            raise IsolationError("STAGE7_EXTERNAL_WRITE")
        if writing and path.exists():
            info = path.stat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1 and not unlinking:
                raise IsolationError("STAGE7_HARDLINK")

    def private_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise IsolationError("STAGE7_DIR_FD")
        check_path(path, writing=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        fd = original_open(path, flags, mode)
        resolved = Path(path).absolute().resolve()
        if resolved.is_relative_to(root):
            info = os.fstat(fd)
            descriptors[fd] = (resolved, (info.st_dev, info.st_ino))
        return fd

    def private_close(fd):
        try:
            return original_close(fd)
        finally:
            descriptors.pop(fd, None)

    def audit(event, args):
        if (event.startswith(("subprocess.", "socket.")) or
                event in {"os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty", "os.startfile", "os.startfile/2"}):
            raise IsolationError("STAGE7_EXTERNAL_OPERATION")
        if event == "open":
            flags = args[2]
            check_path(args[0], writing=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        elif event == "sqlite3.connect":
            value = str(args[0])
            if value.startswith("file:"):
                uri = urlsplit(value)
                query = parse_qs(uri.query)
                if uri.netloc or uri.fragment or query not in ({"mode": ["ro"]}, {"mode": ["rw"]}):
                    raise IsolationError("STAGE7_SQLITE_URI")
                value = unquote(uri.path)
                if os.name == "nt" and re.match(r"/[A-Za-z]:/", value):
                    value = value[1:]
            check_path(value, writing=True)
        elif event == "fcntl.flock":
            check_descriptor(args[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime"}:
            # dir_fd запрещён: относительный путь иначе может разрешиться вне root.
            if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.utime"} and isinstance(args[-1], int) and args[-1] != -1:
                raise IsolationError("STAGE7_DIR_FD")
            check_path(args[0], writing=True, unlinking=event == "os.remove")
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if event == "os.symlink":
                raise IsolationError("STAGE7_LINK")
            if any(fd != -1 for fd in args[2:] if isinstance(fd, int)):
                raise IsolationError("STAGE7_DIR_FD")
            check_path(args[0], writing=True)
            check_path(args[1], writing=True)

    sys.dont_write_bytecode = True
    sys.addaudithook(audit)
    os.open, os.close = private_open, private_close


class ModelTransport:
    """Возвращает только заранее заданные ответы без выполнения команд хоста."""

    def __init__(self, responses: dict[tuple[str, ...], str] | None = None):
        """Создаёт закрытую модель с точными командами и отдельным журналом."""
        self.responses = dict(responses or {})
        self.commands: list[tuple[str, ...]] = []

    def run(self, *args: str, env=None) -> str:
        """Моделирует точную разрешённую команду, отвергая любой неизвестный вызов.

        Args:
            *args: Точные аргументы команды модели.
            env: Не исполняемое окружение, оставленное для интерфейса Host.run.

        Returns:
            Заранее заданный ответ модели.

        Raises:
            IsolationError: Команда не зарегистрирована в модели.
        """
        command = tuple(args)
        self.commands.append(command)
        if command not in self.responses:
            raise IsolationError("STAGE7_UNMODELED_COMMAND")
        return self.responses[command]


def migration_command(private_root: Path, database: Path, image_id: str) -> tuple[str, ...]:
    """Готовит, но не исполняет единственный разрешённый контракт мигратора 7.3.

    Args:
        private_root: Канонический приватный корень испытаний.
        database: Отдельная SQLite внутри scenarios, вне fixtures исходного снимка.
        image_id: Точный immutable ID образа; OCI/CI проверяются вызывающим до 7.3.

    Returns:
        Аргументы Docker без сети, credentials, privileged и production mounts.

    Raises:
        IsolationError: Образ, имя или копия не соответствуют закрытому контракту.
    """
    root = private_root.absolute()
    path = database.absolute()
    if (root.resolve() != root or not root.is_dir() or path.resolve() != path or
            any(root == Path(p).absolute() or root.is_relative_to(Path(p).absolute()) or
                Path(p).absolute().is_relative_to(root)
                for p in ("/opt/obs-chat-bot", "/var/lib/compadre-assistant-deploy")) or
            not path.is_relative_to(root / "scenarios") or not path.is_file() or
            path.stat().st_nlink != 1 or not path.stat().st_mode & stat.S_IWUSR or
            any(character in str(path.parent) for character in ",\r\n") or
            re.fullmatch(r"app(?:-[0-9a-f]{32})?\.db", path.name) is None or
            re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None):
        raise IsolationError("STAGE7_MIGRATION_CONTRACT")
    if any(item.name not in {path.name, path.name + "-wal", path.name + "-shm", path.name + "-journal"}
           or item.is_symlink() or not item.is_file() or item.stat().st_nlink != 1
           for item in path.parent.iterdir()):
        raise IsolationError("STAGE7_MIGRATION_MOUNT")
    return ("docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,nosuid,nodev", "--mount",
            f"type=bind,src={path.parent},dst=/app/data", "--entrypoint", "python",
            image_id, "-m", "compadre_assistant", "--migrate", "--database-path",
            f"/app/data/{path.name}")
