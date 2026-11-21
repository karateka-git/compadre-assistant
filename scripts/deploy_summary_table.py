"""Проверяет данные завершённых запусков и обновляет строки таблицы выкладок."""

from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
import re

from scripts.deploy_summary import ATTEMPT, SHA, VERSION, AttemptSummary

START = "<!-- deploy-summary:start -->"
END = "<!-- deploy-summary:end -->"
RECORD = re.compile(r"<!-- deploy-attempts (\[.*\]) -->")
UNKNOWN = "нет данных"
SCENARIOS = {"initial", "major", "minor", "patch", "resume", "unknown"}
OUTCOMES = {"success": "Успех", "restored": "Возврат подтверждён",
            "unconfirmed": "Результат не подтверждён"}


def _stamp(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9T:.+Z-]{20,40}", value):
        raise ValueError("Некорректная отметка времени")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("Время отчёта должно быть в UTC")
    return stamp


def validated(data: dict) -> AttemptSummary:
    """Принимает только технические поля фиксированного формата для Markdown."""
    if not isinstance(data, dict) or data.keys() != {field.name for field in fields(AttemptSummary)}:
        raise ValueError("Не совпадает набор полей отчёта")
    row = AttemptSummary(**data)
    if (not isinstance(row.attempt, str) or not isinstance(row.source, str)
            or not ATTEMPT.fullmatch(row.attempt) or not ATTEMPT.fullmatch(row.source)
            or row.scenario not in SCENARIOS or row.outcome not in OUTCOMES
            or row.version is not None and not VERSION.fullmatch(row.version)
            or row.sha is not None and not SHA.fullmatch(row.sha)):
        raise ValueError("Некорректные реквизиты попытки")
    _stamp(row.started_at)
    for stamp in (row.installed_from, row.ended_at):
        if stamp is not None:
            _stamp(stamp)
    for value in (row.deployment, row.api, row.downtime_ms):
        if value is not None and (type(value) is not int or not 0 <= value < 2**63):
            raise ValueError("Некорректное числовое поле попытки")
    if row.deployment == 0 or row.outcome != "unconfirmed" and not (
            row.deployment and row.version and row.sha and row.ended_at):
        raise ValueError("Неполное подтверждение установки")
    return row


def _url(repository: str, key: str) -> str:
    run, attempt = key.split(":")
    return f"https://github.com/{repository}/actions/runs/{run}/attempts/{attempt}"


def _seconds(milliseconds: int | None) -> str:
    if milliseconds is None:
        return UNKNOWN
    return format(Decimal(milliseconds) / 1000, "f").replace(".", ",")


def _render(repository: str, rows: list[AttemptSummary]) -> str:
    rows.sort(key=lambda r: (_stamp(r.started_at), r.attempt))
    source = next((r for r in rows if r.attempt == r.source and
                   (r.scenario != "resume" or not r.deployment)), None)
    first = source or rows[0]
    known = source is not None and all(r.scenario != "unknown" for r in rows)
    final = next((r for r in reversed(rows) if r.outcome != "unconfirmed"), rows[-1])
    version = next((r.version for r in rows if r.version), UNKNOWN)
    links = [f"[{version}]({_url(repository, first.source)})"]
    links.extend(f"[resume]({_url(repository, r.attempt)})" for r in rows if r.attempt != first.source)
    scenario = first.scenario.upper() if first.scenario not in ("unknown", "resume") else UNKNOWN
    if any(r.scenario == "resume" for r in rows):
        scenario += ", восстановление"
    installs = [r for r in rows if r.installed_from]
    elapsed = None
    if known and installs and final.ended_at and final.outcome != "unconfirmed":
        delta = _stamp(final.ended_at) - min(_stamp(r.installed_from) for r in installs)
        if delta >= timedelta(0):
            elapsed = delta // timedelta(milliseconds=1)
    downtime = None
    if known and installs and all(r.downtime_ms is not None for r in installs):
        downtime = sum(r.downtime_ms for r in installs)
    api = sum(r.api for r in rows) if known and all(r.api is not None for r in rows) else UNKNOWN
    deployment = next((r.deployment for r in rows if r.deployment), None)
    key = f"deployment:{deployment}" if deployment else f"run:{first.source}"
    records = json.dumps([asdict(r) for r in rows], separators=(",", ":"), sort_keys=True)
    return (f"| {_stamp(first.started_at).date()} | {' · '.join(links)} | {scenario} | "
            f"{_seconds(elapsed)} | {_seconds(downtime)} | {api} | {OUTCOMES[final.outcome]} "
            f"<!-- {key} --> <!-- deploy-attempts {records} --> |")


def attempts(document: str):
    """Читает проверяемые отчёты из управляемой части документа."""
    for match in RECORD.finditer(_table(document)[1]):
        for data in json.loads(match.group(1)):
            yield validated(data)


def _table(document: str) -> tuple[str, str, str]:
    if document.count(START) != 1 or document.count(END) != 1:
        raise ValueError("Границы сводки повреждены")
    prefix, rest = document.split(START)
    table, suffix = rest.split(END)
    return prefix + START, table, END + suffix


def link_deployment_status(document: str, repository: str, attempt: str, pending: str) -> str:
    """Связывает результат обработанной установки с журналом статусов её Deployment."""
    prefix, table, suffix = _table(document)
    lines = table.splitlines(keepends=True)
    for i, line in enumerate(lines):
        match = re.search(r"\| (Успех|Возврат подтверждён) <!-- deployment:([1-9][0-9]{0,19}) -->", line)
        if not match:
            continue
        title, deployment = match.groups()
        url = f"https://api.github.com/repos/{repository}/deployments/{deployment}/statuses"
        link = f"[{title}]({url})"
        if f"]({_url(repository, attempt)})" in line or link in pending:
            lines[i] = line[:match.start(1)] + link + line[match.end(1):]
    return prefix + "".join(lines) + suffix


def verify_table_change(before: str, after: str) -> None:
    """Проверяет, что PR изменил только строки выпусков между служебными метками."""
    prefix, table, suffix = _table(before)
    updated_prefix, updated_table, updated_suffix = _table(after)
    updated_rows = updated_table.strip("\n").splitlines()
    if (prefix != updated_prefix or suffix != updated_suffix or table == updated_table
            or table.strip("\n").splitlines()[:2] != updated_rows[:2]
            or not all(line.startswith("| ") and line.endswith(" |") for line in updated_rows[2:])):
        raise ValueError("PR сводки должен менять только строки таблицы")


def update_table(document: str, repository: str, incoming: AttemptSummary) -> str:
    """Сводит попытки одного Deployment и сохраняет подтверждённые исторические строки."""
    if incoming.source != incoming.attempt and (incoming.scenario != "resume" or not incoming.deployment):
        # Отклонённый ввод оператора ещё не доказывает связь с прежней установкой.
        incoming = replace(incoming, source=incoming.attempt)
    prefix, table, suffix = _table(document)
    lines = table.strip("\n").splitlines()
    records = {}
    indexes = []
    for index, line in enumerate(lines):
        matches_key = any(f"]({_url(repository, key)})" in line for key in (incoming.attempt, incoming.source))
        matches_id = incoming.deployment and f"<!-- deployment:{incoming.deployment} -->" in line
        if not (matches_id or matches_key):
            continue
        match = RECORD.search(line)
        if not match:
            # Старые отчёты заполнялись по журналам до появления артефактов.
            if f"]({_url(repository, incoming.attempt)})" in line:
                return document
            raise ValueError("Для продолжения исторической установки требуется сверка строки")
        indexes.append(index)
        for data in json.loads(match.group(1)):
            item = validated(data)
            records[item.attempt] = item
    previous = records.get(incoming.attempt)
    if previous and previous != incoming:
        if incoming.scenario == "unknown":
            return document
        if previous.scenario != "unknown":
            raise ValueError("Изменился отчёт уже учтённой попытки")
    records[incoming.attempt] = incoming
    values = list(records.values())
    for field in ("deployment", "version", "sha"):
        if len({getattr(item, field) for item in values if getattr(item, field) is not None}) > 1:
            raise ValueError("Попытки относятся к разным выкладкам")
    # У прежнего протокола resume мог содержать только собственный ID и общий Deployment.
    sources = {r.source for r in values if r.scenario != "resume" or r.attempt != r.source}
    if len(sources) > 1:
        raise ValueError("Расходятся идентификаторы исходной попытки")
    row = _render(repository, values)
    for index in reversed(indexes):
        del lines[index]

    def order(line: str) -> tuple[str, int, int]:
        date = line.split(" | ", 1)[0]
        run = re.search(r"/actions/runs/([0-9]+)/attempts/([0-9]+)", line)
        return date, int(run[1]), int(run[2])

    row_order = order(row)
    position = next((i for i, line in enumerate(lines) if "/actions/runs/" in line
                     and order(line) > row_order), len(lines))
    lines.insert(position, row)
    return prefix + "\n" + "\n".join(lines) + "\n" + suffix
