"""Измерения выпуска без влияния на допуск, ожидания и recovery."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
from pathlib import Path
from threading import Event, Thread
import time
from typing import Any, Callable, Iterator
from uuid import uuid4


# Это интервал предупреждения, а не deadline системной операции.
WARNING_SECONDS = 60
_current: ContextVar[Report | None] = ContextVar("deploy_report", default=None)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        # Закрытый stdout не должен превращать наблюдение в причину rollback.
        pass


class Report:
    """Хранит только технические поля одной попытки в приватном JSON."""

    def __init__(self, path: Path, writer: Callable, operation: str) -> None:
        """Задаёт уникальный путь истории и атомарный writer контроллера."""
        self.path = path
        self.writer = writer
        self.started = time.monotonic()
        self.down_since: float | None = None
        self.down_total = 0.0
        self.value: dict[str, Any] = {
            "schema_version": 1, "operation": operation, "started_at": _utc(),
            "ended_at": None, "stage": "entry", "result": "running", "reason": None,
            "sha": None, "run_id": None, "run_attempt": None, "stages": [],
        }

    def save(self) -> None:
        """Обновляет снимок; отказ телеметрии не меняет исход операции."""
        now = time.monotonic()
        self.value["wall_seconds"] = round(now - self.started, 3)
        self.value["downtime_seconds"] = round(
            self.down_total + (now - self.down_since if self.down_since is not None else 0), 3,
        )
        self.value["downtime_open"] = self.down_since is not None
        try:
            self.writer(self.path, json.dumps(self.value, ensure_ascii=False) + "\n")
        except Exception:
            _emit("deploy-metrics result=unavailable reason=report_write_failed")


def tracked(function: Callable) -> Callable:
    """Оборачивает команду отчётом, сохраняя её исключения и результат."""
    @wraps(function)
    def wrapped(host: Any, *args: Any, **kwargs: Any) -> Any:
        from scripts.deploy_prod import _atomic_text

        report = Report(host.state_dir / f"metrics-{uuid4().hex}.json", _atomic_text,
                        function.__name__)
        token = _current.set(report)
        report.save()
        try:
            result = function(host, *args, **kwargs)
        except BaseException as error:
            report.value.update(result="failure", reason=type(error).__name__)
            raise
        else:
            report.value.update(result="success")
            return result
        finally:
            report.value["ended_at"] = _utc()
            report.save()
            _current.reset(token)
    return wrapped


def update(**values: Any) -> None:
    """Записывает явно выбранные технические поля без текста исключений."""
    report = _current.get()
    if report is not None:
        report.value.update(values)
        report.save()


def available() -> None:
    """Закрывает интервал простоя после финальной проверки обоих каналов."""
    report = _current.get()
    if report is not None and report.down_since is not None:
        report.down_total += time.monotonic() - report.down_since
        report.down_since = None
        report.save()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Измеряет достигнутую операцию и предупреждает, не отменяя ожидание.

    Args:
        name: Статическое имя этапа без пользовательских данных.

    Yields:
        Управление исходной операции; её исключение передаётся без изменений.
    """
    report = _current.get()
    if report is None:
        yield
        return
    started = time.monotonic()
    if name.endswith("stop_both") and report.down_since is None:
        report.down_since = started
    entry = {"stage": name, "started_at": _utc(), "ended_at": None,
             "result": "running", "reason": None, "seconds": None}
    report.value["stages"].append(entry)
    report.save()
    _emit(f"deploy-metrics stage={name} result=started")
    stopped = Event()

    def warn() -> None:
        while not stopped.wait(WARNING_SECONDS):
            _emit(f"deploy-metrics stage={name} result=waiting "
                  f"seconds={time.monotonic() - started:.1f} reason=long_stage")

    observer = Thread(target=warn, daemon=True)
    try:
        observer.start()
    except Exception:
        # Нехватка ресурсов наблюдателя также не должна отменять deploy.
        pass
    try:
        yield
    except BaseException as error:
        entry.update(result="failure", reason=type(error).__name__)
        raise
    else:
        entry.update(result="success")
    finally:
        stopped.set()
        entry.update(ended_at=_utc(), seconds=round(time.monotonic() - started, 3))
        report.save()
        _emit(f"deploy-metrics stage={name} result={entry['result']} seconds={entry['seconds']}")
