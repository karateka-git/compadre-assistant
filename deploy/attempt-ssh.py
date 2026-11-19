"""Ограничивает отдельный SSH-ключ runner командами точного Deployment v2."""

import os
import re


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
    if len(parts) != 11 or parts[:3] != fixed or parts[4] != "--attempt":
        raise ValueError("Недопустимая команда")
    version, action, sha, key, owner, snapshot, number = parts[3], *parts[5:]
    if (not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
            or action not in {"reserve", "resume", "create", "in_progress", "finish", "install", "base", "complete", "close"}
            or not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (sha, snapshot))
            or not all(re.fullmatch(r"[1-9][0-9]{0,19}:[1-9][0-9]{0,9}", value) for value in (key, owner))
            or not re.fullmatch(r"0|[1-9][0-9]{0,19}", number)):
        raise ValueError("Недопустимые аргументы")
    return parts


if __name__ == "__main__":
    try:
        argv = command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError:
        raise SystemExit(1)
    os.execve("/usr/bin/sudo", argv, {"PATH": "/usr/bin:/bin"})
