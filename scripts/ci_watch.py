"""Показывает длительное ожидание CI-команды без изменения её результата."""

from __future__ import annotations

import argparse
import signal
import subprocess
import time
from typing import Sequence


# Порог относится только к сообщению: deadline остаётся у GitHub job.
WARNING_SECONDS = 60
# В лог попадают только статические метки, а не argv, env или вывод команды.
STAGES = ("dependencies", "unit", "smoke", "migrations", "candidate", "compose")


def _warn(stage: str, elapsed: float) -> None:
    try:
        print(f"::warning::CI stage={stage} result=waiting "
              f"seconds={elapsed:.1f} reason=long_stage", flush=True)
    except OSError:
        # Закрытый канал диагностики не должен менять исход проверяемой команды.
        pass


def run(command: Sequence[str], stage: str) -> int:
    """Ожидает команду, периодически сообщая о долгом выполнении.

    Args:
        command: Аргументы команды без shell-интерпретации; stdout/stderr наследуются.
        stage: Одна из фиксированных безопасных меток STAGES.

    Returns:
        Исходный returncode процесса, включая отрицательный номер сигнала POSIX.

    Raises:
        ValueError: Команда пуста или метка не поддерживается.
        OSError: Команду невозможно запустить.

    Вызывать из главного потока: сигналы runner передаются дочернему процессу.
    Новый process group не создаётся, поэтому group cancellation runner сохранён.
    """
    if stage not in STAGES or not command:
        raise ValueError("Нужны команда и поддерживаемая метка CI")
    started = time.monotonic()
    with subprocess.Popen(command) as process:
        previous = {}

        def forward(signum: int, frame: object) -> None:
            """Передаёт штатную отмену runner без собственного timeout."""
            try:
                process.send_signal(signum)
            except OSError:
                # Процесс мог завершиться между получением сигнала и пересылкой.
                pass

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, forward)
            while True:
                try:
                    return process.wait(timeout=WARNING_SECONDS)
                except subprocess.TimeoutExpired:
                    _warn(stage, time.monotonic() - started)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main() -> None:
    """Запускает CI-команду и сохраняет её exit status, не печатая argv/env."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        code = run(command, args.stage)
    except (OSError, ValueError):
        print("CI command could not be started", flush=True)
        code = 127
    # Shell представляет завершение сигналом N как статус 128+N.
    raise SystemExit(code if code >= 0 else 128 - code)


if __name__ == "__main__":
    main()
