"""Связывает миграции опубликованного выпуска с принятой SQLite."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
import json
import re

from scripts.ci_repository_compatibility import resolve_release_tag
from scripts.deploy_prepare import SHA_PATTERN, VERSION_PATTERN
from scripts.full_ci_controller import GitHub


MIGRATIONS_PREFIX = "compadre_assistant/core/data/sqlite/migrations/"
EXPECTED_MIGRATION_LABEL = "Ожидаемая миграция SQLite:"
_MIGRATION = re.compile(r"([0-9]{4})_[a-z0-9_]+\.sql")


def _migration_version(name: object) -> int:
    """Возвращает положительную версию канонического имени миграции."""
    match = _MIGRATION.fullmatch(name) if isinstance(name, str) else None
    if match is None or match[1] == "0000":
        raise ValueError("Некорректное имя миграции SQLite")
    return int(match[1])


def _require_release(version: str, sha: str) -> None:
    """Проверяет канонические идентификаторы выпуска."""
    if (not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version)
            or not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha)):
        raise ValueError("Сводка SQLite требует каноническую версию и полный SHA")


def expected_migration_from_manifest(paths: Iterable[str]) -> str:
    """Определяет последнюю миграцию по проверенному манифесту кандидата.

    Args:
        paths: Пути из манифеста точного Git-коммита, проверенного в CI.

    Returns:
        Имя последней ожидаемой SQL-миграции.

    Raises:
        ValueError: В каталоге нет миграций или их последовательность неверна.
    """
    names = []
    for path in paths:
        if not isinstance(path, str):
            raise ValueError("Некорректный путь в манифесте миграций кандидата")
        if path.startswith(MIGRATIONS_PREFIX):
            name = path.removeprefix(MIGRATIONS_PREFIX)
            if "/" not in name and name.endswith(".sql"):
                names.append(name)
    names.sort()
    if not names or [_migration_version(name) for name in names] != list(range(1, len(names) + 1)):
        raise ValueError("Миграции кандидата не образуют непрерывную последовательность")
    return names[-1]


def expected_migration_at(api: GitHub, sha: str) -> str:
    """Читает последнюю миграцию из каталога в точном коммите кандидата.

    Args:
        api: Клиент GitHub репозитория выпуска.
        sha: Полный SHA коммита, подтверждённого при публикации.

    Returns:
        Каноническое имя последней миграции из Git.

    Raises:
        ValueError: Каталог или список миграций не удалось подтвердить.
    """
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise ValueError("Ожидаемая миграция требует полного SHA кандидата")
    entries = api.get(f"/contents/{MIGRATIONS_PREFIX.rstrip('/')}", ref=sha)
    # Contents API выдаёт не более 1000 записей каталога.
    if not isinstance(entries, list) or len(entries) >= 1000:
        raise ValueError("Не подтверждён полный каталог миграций кандидата")
    paths = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or "/" in name or entry.get("path") != MIGRATIONS_PREFIX + name:
            raise ValueError("Не подтверждён путь из каталога миграций кандидата")
        if name.endswith(".sql"):
            if entry.get("type") != "file":
                raise ValueError("Миграция кандидата не является файлом")
            paths.append(entry["path"])
    return expected_migration_from_manifest(paths)


@dataclass(frozen=True)
class InstallationSchemaEvidence:
    """Фиксирует принятую SQLite для точного Release и попытки CI."""

    release_id: int
    run_id: int
    run_attempt: int
    generation_id: str
    migration: str

    def __post_init__(self) -> None:
        """Отклоняет неполные или неканонические сведения о принятой схеме."""
        if any(type(value) is not int or value <= 0 for value in (
            self.release_id, self.run_id, self.run_attempt,
        )):
            raise ValueError("Некорректный Release или попытка CI в теге установки")
        if not isinstance(self.generation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.generation_id):
            raise ValueError("Некорректное поколение SQLite в теге установки")
        _migration_version(self.migration)

    @classmethod
    def from_receipt(cls, receipt: Mapping[str, object], version: str, sha: str) -> InstallationSchemaEvidence:
        """Выделяет сведения о SQLite из свежей подтверждённой приёмки.

        Args:
            receipt: Запись технической приёмки, прочитанная с доверенного VPS.
            version: Версия, подтверждённая опубликованным Release.
            sha: Полный SHA этого Release.

        Returns:
            Стабильные реквизиты принятой схемы без сведений о контейнерах.

        Raises:
            ValueError: Запись не соответствует версии, SHA или SQLite v2.
        """
        _require_release(version, sha)
        runtime_id = receipt.get("runtime_id")
        if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
                or receipt.get("version") != version or receipt.get("sha") != sha
                or receipt.get("kind") not in ("ordinary", "emergency")
                or not isinstance(runtime_id, str) or not re.fullmatch(r"[0-9a-f]{64}", runtime_id)):
            raise ValueError("Приёмка не соответствует опубликованному выпуску")
        return cls(receipt.get("release_id"), receipt.get("run_id"), receipt.get("run_attempt"),
                   receipt.get("generation_id"), receipt.get("migration"))

    def message(self) -> str:
        """Сериализует аннотацию тега установки версии 1."""
        return json.dumps({"version": 1, **asdict(self)}, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_message(cls, message: str) -> InstallationSchemaEvidence:
        """Читает единственный канонический JSON аннотированного тега.

        Args:
            message: Сообщение Git-объекта аннотированного тега установки.

        Returns:
            Реквизиты подтверждённой схемы.

        Raises:
            ValueError: Формат, версия или поля сообщения некорректны.
        """
        if not isinstance(message, str) or len(message) > 1024:
            raise ValueError("Некорректный размер или формат аннотации тега установки")
        body = message.rstrip("\n")
        data = json.loads(body)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data.pop("version") != 1:
            raise ValueError("Некорректная версия аннотации тега установки")
        try:
            evidence = cls(**data)
        except TypeError as error:
            raise ValueError("Неполная аннотация тега установки") from error
        if evidence.message() != body:
            raise ValueError("Аннотация тега установки не каноническая")
        return evidence


@dataclass(frozen=True)
class InstallationTag:
    """Описывает проверенный ref установки и необязательную аннотацию."""

    version: str
    sha: str
    object_sha: str | None
    evidence: InstallationSchemaEvidence | None


def read_installation_tag(reference: object, version: str, sha: str,
                          read_tag: Callable[[str], object] | None = None) -> InstallationTag:
    """Читает точный ref установки, включая прежний прямой ref.

    Args:
        reference: Ответ GitHub для конкретного ref production/<version>.
        version: Каноническая версия опубликованного Release.
        sha: Полный SHA опубликованного коммита.
        read_tag: Чтение Git-объекта аннотированного тега по SHA.

    Returns:
        Точный тег установки. Для прямого ref сведения о SQLite отсутствуют.

    Raises:
        ValueError: Имя, цель, объект или аннотация тега не подтверждены.
    """
    _require_release(version, sha)
    tag_name = f"production/{version}"
    if not isinstance(reference, dict) or reference.get("ref") != f"refs/tags/{tag_name}":
        raise ValueError("Не подтверждено имя тега установки")
    target, object_sha, message = resolve_release_tag(reference, tag_name, read_tag)
    if target != sha:
        raise ValueError("Тег установки указывает на другой SHA")
    evidence = InstallationSchemaEvidence.from_message(message) if message is not None else None
    return InstallationTag(version, target, object_sha, evidence)


def build_summary(version: str, sha: str, release_id: int, run_id: int | None, run_attempt: int | None,
                  expected_migration: str, installation: InstallationTag | None) -> dict[str, object]:
    """Объединяет ожидание выпуска с принятой SQLite в одной сводке.

    Args:
        version: Версия проверенного опубликованного Release.
        sha: Полный SHA релизного коммита.
        release_id: Точный идентификатор опубликованного Release.
        run_id: Идентификатор прогона CI обычного выпуска.
        run_attempt: Номер попытки прогона обычного выпуска.
        expected_migration: Последняя миграция из манифеста кандидата.
        installation: Проверенный тег установки, если он создан.

    Returns:
        Реквизиты релиза, ожидание CI и подтверждённые сведения об установке.

    Raises:
        ValueError: Тег установки относится к другому Release, попытке или схеме.
    """
    _require_release(version, sha)
    _migration_version(expected_migration)
    if (type(release_id) is not int or release_id <= 0
            or ((run_id, run_attempt) != (None, None)
                and any(type(value) is not int or value <= 0 for value in (run_id, run_attempt)))):
        raise ValueError("Сводка SQLite требует точный Release и попытку CI")
    summary = {
        "version": version, "sha": sha, "release_id": release_id,
        "run_id": run_id, "run_attempt": run_attempt, "expected_migration": expected_migration,
        "installation_status": "pending", "installation_tag_object_sha": None,
        "generation_id": None, "applied_migration": None,
    }
    if installation is None:
        return summary
    if (installation.version, installation.sha) != (version, sha):
        raise ValueError("Тег установки относится к другому выпуску")
    evidence = installation.evidence
    if evidence is None:
        return summary | {"installation_status": "legacy_tag"}
    if (evidence.release_id != release_id
            or (run_id is not None and (evidence.run_id, evidence.run_attempt) != (run_id, run_attempt))
            or evidence.migration != expected_migration):
        raise ValueError("Принятая SQLite не совпала с Release, попыткой CI или ожидаемой миграцией")
    return summary | {"installation_status": "accepted", "installation_tag_object_sha": installation.object_sha,
                      "run_id": evidence.run_id, "run_attempt": evidence.run_attempt,
                      "generation_id": evidence.generation_id, "applied_migration": evidence.migration}
