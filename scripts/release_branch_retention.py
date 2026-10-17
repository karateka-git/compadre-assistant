"""Строит безопасный план хранения релизных веток по полному снимку."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import re
from typing import Mapping


BRANCH = re.compile(r"release/(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
REF = re.compile(r"[A-Za-z0-9._/-]+\Z")
MAX_SNAPSHOT_AGE = timedelta(minutes=5)


class RetentionError(ValueError):
    """Означает, что снимок не доказывает безопасность кандидатов."""


@dataclass(frozen=True)
class RetentionPlan:
    """Содержит сохраняемые ветки и кандидатов для отдельной проверки."""

    active: str
    retained: tuple[str, ...]
    candidates: tuple[str, ...]


def _branches(values: object, label: str, known: set[str]) -> set[str]:
    """Проверяет полный список веток, полученный из одного источника."""
    if not isinstance(values, list) or any(not isinstance(x, str) or x not in known for x in values):
        raise RetentionError(f"Некорректный список {label}")
    if len(values) != len(set(values)):
        raise RetentionError(f"Повторяются ветки в {label}")
    return set(values)


def _line(name: str) -> tuple[int, int]:
    """Возвращает числовой порядок канонической релизной линии."""
    match = BRANCH.fullmatch(name)
    if match is None:
        raise RetentionError(f"Некорректное имя ветки: {name}")
    return int(match[1]), int(match[2])


def _open_pr_branches(values: object, known: set[str], repository: str) -> set[str]:
    """Находит base и принадлежащие проекту head всех открытых PR.

    Args:
        values: Полный список открытых PR с номером, base и head.
        known: Полный набор релизных refs проекта.
        repository: Репозиторий, в котором открыты PR.

    Returns:
        Релизные линии, занятые любым концом открытого PR.

    Raises:
        RetentionError: PR нельзя однозначно сопоставить с локальными refs.
    """
    if not isinstance(values, list):
        raise RetentionError("Некорректный список открытых PR")
    protected: set[str] = set()
    numbers: set[int] = set()
    for pr in values:
        if (not isinstance(pr, dict) or set(pr) != {"number", "base", "head"} or
                type(pr["number"]) is not int or pr["number"] <= 0 or pr["number"] in numbers):
            raise RetentionError("Некорректный или повторный открытый PR")
        numbers.add(pr["number"])
        for side in ("base", "head"):
            endpoint = pr[side]
            if (not isinstance(endpoint, dict) or set(endpoint) != {"repository", "ref"} or
                    not isinstance(endpoint["repository"], str) or
                    REPOSITORY.fullmatch(endpoint["repository"]) is None or
                    not isinstance(endpoint["ref"], str) or
                    REF.fullmatch(endpoint["ref"]) is None):
                raise RetentionError(f"Неполный {side} открытого PR")
            same_repository = endpoint["repository"].casefold() == repository.casefold()
            if side == "base" and not same_repository:
                raise RetentionError("Base PR относится к другому репозиторию")
            if same_repository:
                ref = endpoint["ref"]
                if BRANCH.fullmatch(ref):
                    if ref not in known:
                        raise RetentionError("Ref открытого PR отсутствует в списке веток")
                    protected.add(ref)
    return protected


def plan_retention(snapshot: Mapping[str, object], *, now: datetime) -> RetentionPlan:
    """Находит ветки вне окна хранения, не выполняя удаление.

    Args:
        snapshot: Полный снимок VPS и GitHub, собранный для одного решения.
        now: Текущее время UTC для ограничения возраста серверного снимка.

    Returns:
        План с сохранёнными ветками и кандидатами на отдельную проверку.

    Raises:
        RetentionError: Данные неполны, устарели или противоречат друг другу.
    """
    required = {"repository", "server", "branches", "supported", "open_prs",
                "pending", "rollback", "protected_tags", "complete"}
    if set(snapshot) != required or snapshot["repository"] != "karateka-git/compadre-assistant":
        raise RetentionError("Неверный репозиторий или неполный снимок")
    complete = snapshot["complete"]
    if (not isinstance(complete, dict) or set(complete) !=
            {"server", "branches", "supported", "open_prs", "pending", "rollback", "protected_tags"}
            or any(value is not True for value in complete.values())):
        raise RetentionError("Не подтверждена полнота всех источников")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise RetentionError("now должен быть временем UTC")
    server = snapshot["server"]
    if not isinstance(server, dict) or set(server) != {"version", "stage", "observed_at", "marker_absent"}:
        raise RetentionError("Неполное состояние VPS")
    version = server["version"]
    match = VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None or server["stage"] != "success" or server["marker_absent"] is not True:
        raise RetentionError("Нет подтверждённого успешного активного выпуска")
    try:
        observed = datetime.fromisoformat(server["observed_at"])
    except (TypeError, ValueError) as error:
        raise RetentionError("Некорректное время VPS") from error
    if (observed.tzinfo is None or observed.utcoffset() != timedelta(0) or
            not timedelta(0) <= now - observed <= MAX_SNAPSHOT_AGE):
        raise RetentionError("Снимок VPS устарел или находится в будущем")
    active = f"release/{match[1]}.{match[2]}"
    branches = snapshot["branches"]
    if (not isinstance(branches, list) or not branches or
            any(not isinstance(item, dict) or set(item) != {"name", "sha"}
                or not isinstance(item["name"], str) or
                not isinstance(item["sha"], str) or not SHA.fullmatch(item["sha"])
                for item in branches)):
        raise RetentionError("Неполный список refs GitHub")
    names = [item["name"] for item in branches]
    if len(names) != len(set(names)):
        raise RetentionError("Повторяются refs GitHub")
    for name in names:
        _line(name)
    known = set(names)
    if active not in known:
        raise RetentionError("Активная ветка отсутствует в GitHub")
    protected = set()
    for label in ("supported", "pending", "rollback"):
        protected.update(_branches(snapshot[label], label, known))
    protected.update(_open_pr_branches(snapshot["open_prs"], known, snapshot["repository"]))
    tags = snapshot["protected_tags"]
    if not isinstance(tags, list) or any(not isinstance(tag, str) or VERSION.fullmatch(tag) is None
                                         for tag in tags) or len(tags) != len(set(tags)):
        raise RetentionError("Неполный или некорректный список защищённых тегов")
    if version not in tags:
        raise RetentionError("Тег активного выпуска не подтверждён")
    ordered = sorted(names, key=_line, reverse=True)
    active_index = ordered.index(active)
    # Новые линии сохраняются: они могут ожидать первого развёртывания.
    window = set(ordered[:active_index + 4])
    retained = window | protected
    candidates = known - retained
    return RetentionPlan(active, tuple(sorted(retained, key=_line, reverse=True)),
                         tuple(sorted(candidates, key=_line, reverse=True)))


def revalidate_candidate(initial: Mapping[str, object], fresh: Mapping[str, object],
                         branch: str, *, now: datetime) -> str:
    """Проверяет кандидата непосредственно перед отдельным удалением ref.

    Args:
        initial: Полный снимок, по которому согласован первоначальный план.
        fresh: Новый полный снимок VPS и GitHub непосредственно перед действием.
        branch: Одно согласованное имя ветки.
        now: Текущее время UTC.

    Returns:
        SHA ref, который оператор обязан сверить с новым чтением GitHub.

    Raises:
        RetentionError: Состояние изменилось или ветка больше не кандидат.
    """
    original = plan_retention(initial, now=now)
    current = plan_retention(fresh, now=now)
    initial_time = datetime.fromisoformat(initial["server"]["observed_at"])
    fresh_time = datetime.fromisoformat(fresh["server"]["observed_at"])
    if fresh_time <= initial_time:
        raise RetentionError("Повторный снимок VPS должен быть новее исходного")
    if branch not in original.candidates or branch not in current.candidates:
        raise RetentionError("Ветка не является устойчивым кандидатом")
    # Любое изменение наблюдаемых данных требует нового плана и согласования.
    original_data = deepcopy(dict(initial))
    fresh_data = deepcopy(dict(fresh))
    original_data["server"].pop("observed_at")
    fresh_data["server"].pop("observed_at")
    if original_data != fresh_data or original != current:
        raise RetentionError("Состояние изменилось после согласования плана")
    return next(item["sha"] for item in fresh["branches"] if item["name"] == branch)
