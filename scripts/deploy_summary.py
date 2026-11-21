"""Формирует небольшой отчёт одной попытки выкладки для сводной таблицы."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys


TIMING_PREFIX = "deploy-install-timing "
ATTEMPT = re.compile(r"[1-9][0-9]{0,19}:[1-9][0-9]{0,9}")
VERSION = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


def utc_now() -> str:
    """Возвращает отметку UTC для сопоставления разных процессов и попыток."""
    return datetime.now(timezone.utc).isoformat()


def api_count(rows: list[dict]) -> int:
    """Суммирует фактические обращения к GitHub в проверенном отчёте транспорта."""
    return sum(row["attempts"] for row in rows if row["target"] in ("github_api", "github_graphql_query"))


@dataclass
class AttemptSummary:
    """Сохраняет технические поля попытки до и после подтверждения результата."""

    attempt: str
    source: str
    version: str | None
    sha: str | None
    scenario: str
    started_at: str
    installed_from: str | None = None
    ended_at: str | None = None
    deployment: int | None = None
    downtime_ms: int | None = None
    api: int | None = None
    outcome: str = "unconfirmed"

    def __post_init__(self) -> None:
        """Начинает отдельный подсчёт доставленных отчётов контроллера."""
        self._vps_requests = 0
        self._complete = True

    def observe_ssh(self, report: dict | None) -> None:
        """При отсутствии отчёта SSH оставляет совокупный расход неизвестным."""
        if report is None:
            self._complete = False
        else:
            self._vps_requests += api_count(report["http"])

    def finish(self, rows: list[dict]) -> None:
        """Записывает конец попытки и полный расход API обеих сторон при наличии данных."""
        self.ended_at = utc_now()
        if self._complete:
            self.api = api_count(rows) + self._vps_requests
        self.save()

    @classmethod
    def start(cls, version: str, sha: str, scenario: str) -> AttemptSummary:
        """Создаёт начальную запись, включая идентификатор исходного запуска для resume."""
        key = f"{os.getenv('GITHUB_RUN_ID', '')}:{os.getenv('GITHUB_RUN_ATTEMPT', '')}"
        source = os.getenv("RELEASE_SOURCE_ATTEMPT") or key
        summary = cls(key, source if ATTEMPT.fullmatch(source) else key,
                      version if VERSION.fullmatch(version) else None,
                      sha if SHA.fullmatch(sha) else None, scenario, utc_now())
        summary.save()
        return summary

    def save(self) -> None:
        """Атомарно сохраняет отчёт, если workflow указал путь в каталоге runner."""
        target = os.getenv("DEPLOY_SUMMARY_PATH")
        if not target:
            return
        path = Path(target)
        try:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(self), ensure_ascii=True) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError:
            print("Не удалось сохранить метрики попытки выкладки", file=sys.stderr)

    def installation(self) -> None:
        """Отмечает отправку команды установки, в том числе при последующем обрыве SSH."""
        self.installed_from = utc_now()
        self.save()

    def timing(self, stderr: str | bytes | None) -> None:
        """Использует подтверждённый интервал контроллера для выбранного SHA."""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        for line in (stderr or "").splitlines():
            if line.startswith(TIMING_PREFIX) and len(line) < 512:
                try:
                    timing = json.loads(line[len(TIMING_PREFIX):])
                    if (isinstance(timing, dict) and timing.get("sha") == self.sha and self.sha
                            and timing.get("version") == self.version
                            and type(timing.get("downtime_ms")) is int and timing["downtime_ms"] >= 0):
                        stamp = datetime.fromisoformat(timing["started_at"])
                        if stamp.utcoffset() == timezone.utc.utcoffset(stamp):
                            self.installed_from = stamp.isoformat()
                            self.downtime_ms = timing["downtime_ms"]
                except (TypeError, KeyError, ValueError, RecursionError):
                    continue
