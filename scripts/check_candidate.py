"""Собирает candidate image и проверяет его на временной SQLite без сети."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Только синтетические значения: проверяется форма настроек без внешних API.
FAKE_ENVIRONMENT = {
    "APP_ENV": "ci",
    "APP_DEBUG": "false",
    "DATABASE_PATH": "/app/data/app.db",
    "ENABLED_CHANNELS": "telegram,vk",
    "ENABLED_CAPABILITIES": "",
    "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz",
    "VK_BOT_TOKEN": "ci-placeholder",
    "VK_GROUP_ID": "1",
}


def run(*arguments: str, cwd: Path = PROJECT_ROOT) -> str:
    """Выполняет команду и печатает безопасную диагностику при любом результате.

    Args:
        *arguments: Команда и её аргументы.
        cwd: Рабочий каталог процесса.

    Returns:
        Стандартный вывод без крайних пробелов.

    Raises:
        subprocess.CalledProcessError: Команда завершилась с ошибкой.
    """
    result = subprocess.run(
        arguments, cwd=cwd, text=True, capture_output=True,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, arguments)
    return result.stdout.strip()


def check_candidate(expected_sha: str) -> None:
    """Собирает образ точного commit и проверяет отдельные миграции и healthcheck.

    Args:
        expected_sha: Полный SHA candidate, проверенный вызывающим workflow.

    Raises:
        RuntimeError: Checkout не совпадает с запрошенным commit.
        subprocess.CalledProcessError: Сборка или проверка образа завершилась ошибкой.
    """
    actual_sha = run("git", "rev-parse", "HEAD")
    if actual_sha != expected_sha:
        raise RuntimeError("candidate checkout SHA differs from expected SHA")
    if run("git", "status", "--porcelain"):
        raise RuntimeError("candidate checkout must be clean before Docker build")
    image = f"compadre-assistant:ci-{expected_sha[:12]}"
    run("docker", "build", "--tag", image, ".")
    with TemporaryDirectory(prefix="candidate-health-") as directory:
        mount = f"type=bind,source={Path(directory).resolve()},target=/app/data"
        common = ["docker", "run", "--rm", "--network", "none", "--mount", mount]
        for name, value in FAKE_ENVIRONMENT.items():
            common.extend(("--env", f"{name}={value}"))
        run(*common, image, "python", "-m", "compadre_assistant", "--migrate",
            "--database-path", "/app/data/app.db")
        run(*common, image, "python", "-m", "compadre_assistant", "--healthcheck",
            "--channels", "telegram,vk")
    image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", image)
    print(f"Candidate healthcheck passed: SHA={expected_sha} image={image_id}")


def main() -> int:
    """Читает точный SHA candidate и возвращает код результата."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    check_candidate(args.expected_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
