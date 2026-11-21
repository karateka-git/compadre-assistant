"""Ограничивает отдельный SSH-ключ runner командами точного Deployment v2."""

import re
import subprocess
import sys


def command(original: str) -> list[str]:
    """Проверяет полную команду OpenSSH и строит argv для sudo без shell.

    Args:
        original: Исходная строка запроса из SSH_ORIGINAL_COMMAND.

    Returns:
        Канонические аргументы запуска закреплённого deploy-prod.

    Raises:
        ValueError: Запрос не соответствует единственному разрешённому контракту.
    """
    fixed = ["sudo", "-n", "/usr/local/sbin/deploy-prod"]
    parts = original.split(" ")
    if parts[:3] != fixed or not (
            len(parts) == 11 and parts[4] == "--attempt" or len(parts) == 12 and parts[4] == "--patch-attempt"):
        raise ValueError("Недопустимая команда")
    version, action, sha, key, owner, snapshot = parts[3], *parts[5:10]
    if (not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
            or action not in {"reserve", "resume", "create", "in_progress", "finish", "install", "complete", "close"}
            or not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (sha, snapshot))
            or not all(re.fullmatch(r"[1-9][0-9]{0,19}:[1-9][0-9]{0,9}", value) for value in (key, owner))
            or not all(re.fullmatch(r"0|[1-9][0-9]{0,19}", value) for value in parts[10:])):
        raise ValueError("Недопустимые аргументы")
    return parts


def dispatch(frame: bytes) -> int:
    """Запускает root-wrapper с проверенными argv и ограниченным stdin.

    Args:
        frame: Исходная команда OpenSSH, NUL-разделитель и байты stdin runner.

    Returns:
        Код завершения установленного контроллера.

    Raises:
        ValueError: Заголовок, длина либо наличие пакета не соответствует действию.
    """
    header, separator, body = frame.partition(b"\0")
    if not separator or len(header) > 1024 or len(body) > 16384:
        raise ValueError("Некорректная длина запроса")
    argv = command(header.decode("ascii"))
    if bool(body) != (argv[4] == "--patch-attempt" and argv[5] in ("reserve", "resume")):
        raise ValueError("Некорректный stdin команды")
    return subprocess.run(argv[2:], input=body, env={"PATH": "/usr/bin:/bin"}).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(dispatch(sys.stdin.buffer.read(1024 + 1 + 16384 + 1)))
    except (ValueError, OSError):
        raise SystemExit(1)
