"""Изолированный broker только для миграции private SQLite-копии этапа 7.

Запускается отдельным доверенным процессом вне controller audit guard.
Не добавляет исключений в guard и не принимает произвольные Docker-команды.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from scripts.stage7_transport import IsolationError, migration_command


# Только технические ENV базового Python image; credentials в образе запрещены.
IMAGE_ENV = frozenset({"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
                       "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"})


def migrate(private_root: Path, database: Path, image_id: str, candidate_sha: str) -> None:
    """В отдельном Linux-процессе мигрирует только проверенную writable копию.

    Args:
        private_root: Приватный root stage7, отличный от production.
        database: Единственная SQLite в отдельном scenarios подкаталоге.
        image_id: Immutable Docker image ID из проверенного допуска.
        candidate_sha: Точный SHA кандидата из того же допуска CI.

    Raises:
        IsolationError: Входы, OCI image, Docker либо миграция не подтверждены.

    Note:
        CI/snapshot admission проверяет вызывающий до остановки ботов. Эта
        функция не выполняет systemd, polling, live bind или controller deploy.
        stdout/stderr мигратора никогда не возвращаются в отчёт.
    """
    command = migration_command(private_root, database, image_id)
    if (sys.platform != "linux" or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha)
            or private_root.stat().st_uid != os.geteuid() or private_root.stat().st_mode & 0o077
            or any(path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077
                   for path in (database.parent, database))):
        raise IsolationError("STAGE7_MIGRATION_PRIVATE")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(private_root),
           "DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONFIG": str(private_root / "empty-docker-config")}
    config = private_root / "empty-docker-config"
    # Отсутствующий каталог не позволяет Docker прочитать credentials из HOME.
    if os.path.lexists(config):
        raise IsolationError("STAGE7_DOCKER_CONFIG")
    name = "compadre-stage7-migrate-" + uuid.uuid4().hex
    started = False
    try:
        result = subprocess.run(("/usr/bin/docker", "image", "inspect", image_id),
                                env=env, cwd=private_root, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise IsolationError("STAGE7_IMAGE_INSPECT")
        images = json.loads(result.stdout)
        if not isinstance(images, list) or len(images) != 1:
            raise IsolationError("STAGE7_IMAGE_INSPECT")
        image = images[0]
        settings = image["Config"]
        if (image["Id"] != image_id or (settings.get("Labels") or {}).get("org.opencontainers.image.revision") != candidate_sha
                or settings.get("Volumes") or settings.get("WorkingDir") != "/app"
                or any(entry.split("=", 1)[0] not in IMAGE_ENV for entry in settings.get("Env", []))):
            raise IsolationError("STAGE7_IMAGE_CONTRACT")
        # Повторная проверка mount после inspect сужает окно случайной подмены.
        if migration_command(private_root, database, image_id) != command:
            raise IsolationError("STAGE7_MIGRATION_CHANGED")
        started = True
        result = subprocess.run(("/usr/bin/docker", "run", "--name", name, *command[2:]), env=env, cwd=private_root,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=300, check=False)
        if result.returncode:
            raise IsolationError("STAGE7_MIGRATION_FAILED")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        raise IsolationError("STAGE7_MIGRATION_FAILED") from error
    finally:
        if started:
            # При timeout клиент Docker не гарантирует остановку контейнера.
            # Удаляется только уникальное имя этого broker; production IDs неизвестны.
            try:
                subprocess.run(("/usr/bin/docker", "rm", "-f", name), env=env, cwd=private_root,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
                remaining = subprocess.run(("/usr/bin/docker", "ps", "--all", "--quiet", "--no-trunc",
                                             "--filter", f"name=^/{name}$"), env=env, cwd=private_root,
                                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
                if remaining.returncode or remaining.stdout.strip():
                    raise IsolationError("STAGE7_MIGRATION_CLEANUP")
            except (OSError, subprocess.TimeoutExpired) as error:
                raise IsolationError("STAGE7_MIGRATION_CLEANUP") from error
