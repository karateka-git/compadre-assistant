"""Доставляет точному прежнему runtime readonly допуск новой SQLite-схемы."""

import json
import os
from pathlib import Path
import stat

from compadre_assistant.core.data.sqlite import runtime_schema
from scripts import deploy_prepare as prep


# Постоянные имена относятся к приватному controller state, вне release checkout.
ADMISSION = "schema-admission.json"
LAUNCH = "schema-launch.json"
OVERRIDE = "schema-compose.yml"
CONTAINER_ADMISSION = "/run/compadre-schema/admission.json"
CONTAINER_LAUNCH = "/run/compadre-schema/launch.json"


def _verification_id(evidence: dict) -> str:
    if "tag_object_sha" in evidence:
        return evidence["verification_id"]
    return evidence["artifact_digest"].removeprefix("sha256:")


def prepare(host, state: dict, record, publish) -> None:
    """Под остановленными writers закрепляет контракт exact old image/new schema.

    Перед вызовом controller повторно проверяет CI evidence и восстанавливает
    чистый checkout старого SHA. При одинаковых manifest допуск не нужен.

    Args:
        host: Адаптер с остановленными writers под deploy lock.
        state: Переход с точными SHA/image ID и проверенным evidence.
        record: Durable writer стадии и полей состояния.
        publish: Atomic writer приватных файлов с fsync.

    Raises:
        ValueError: История, схема или release pair не допускают прежний runtime.
    """
    host.require_recovery_quiescence()
    side = "new" if state["admission_may_have_started"] else "old"
    target = state[side]["manifest"]
    prep.require_clean_checkout(host.checkout, state["old_sha"])
    migrations_dir = host.checkout / "compadre_assistant/core/data/sqlite/migrations"
    migrations = runtime_schema._discover_migrations(migrations_dir)
    if target == [item.name for item in migrations]:
        record("recovery_selected", runtime_schema=None)
        return
    base = [[item.version, item.name] for item in migrations]
    target_journal = [[int(name[:4]), name] for name in target]
    evidence = state["compatibility_evidence"]
    if evidence["schema_fingerprint"] != state[side]["fingerprint"]:
        raise ValueError("Schema admission fingerprint не совпадает с evidence")
    context = {"base_sha": state["old_sha"], "base_image_id": state["old_image_id"],
               "candidate_sha": state["candidate"]["sha"], "candidate_image_id": state["candidate"]["image_id"],
               "controller_sha": state["controller_sha"],
               "verification_id": _verification_id(evidence)}
    admission = context | {"format_version": 1, "base_journal": base, "target_journal": target_journal,
                           "base_sql_sha256": runtime_schema.migration_sql_fingerprint(migrations_dir),
                           "schema_sha256": state[side]["fingerprint"]}
    runtime_schema.validate_release_pair(admission, context)
    schema = {"admission": admission, "launch": context}
    for name, value in ((ADMISSION, admission), (LAUNCH, context)):
        publish(host.state_dir / name, json.dumps(value, sort_keys=True) + "\n")
    publish(host.state_dir / OVERRIDE, json.dumps(compose_override(host.state_dir), sort_keys=True) + "\n")
    record("recovery_selected", runtime_schema=schema)


def compose_override(state_dir: Path) -> dict:
    """Формирует readonly mounts и переменные запуска schema validator.

    Args:
        state_dir: Приватный каталог controller state на Linux.

    Returns:
        Compose override обоих каналов, сериализуемый в JSON/YAML.
    """
    service = {"environment": {runtime_schema.ADMISSION_ENV: CONTAINER_ADMISSION,
                               runtime_schema.LAUNCH_ENV: CONTAINER_LAUNCH},
               "volumes": [{"type": "bind", "source": str(state_dir / name), "target": target,
                            "read_only": True, "bind": {"create_host_path": False}}
                           for name, target in ((ADMISSION, CONTAINER_ADMISSION), (LAUNCH, CONTAINER_LAUNCH))]}
    return {"services": {name: service for name in prep.CHANNELS}}


def require_files(host, state: dict) -> None:
    """Проверяет неизменность readonly admission/context и Compose override.

    Args:
        host: Адаптер с приватным state_dir.
        state: Durable переход с сохранённым runtime_schema.

    Raises:
        ValueError: Файлы, права либо привязка контракта к переходу не совпадают.
    """
    schema = state.get("runtime_schema")
    if schema is None:
        return
    runtime_schema.validate_release_pair(schema["admission"], schema["launch"])
    side = "new" if state["admission_may_have_started"] else "old"
    expected = {"base_sha": state["old_sha"], "base_image_id": state["old_image_id"],
                "candidate_sha": state["candidate"]["sha"], "candidate_image_id": state["candidate"]["image_id"],
                "controller_sha": state["controller_sha"],
                "verification_id": _verification_id(state["compatibility_evidence"])}
    if (schema["launch"] != expected or schema["admission"]["schema_sha256"] != state[side]["fingerprint"]
            or schema["admission"]["target_journal"] != [[int(name[:4]), name] for name in state[side]["manifest"]]):
        raise ValueError("Runtime schema admission не принадлежит выбранному переходу")
    for name, value in ((ADMISSION, schema["admission"]), (LAUNCH, schema["launch"]),
                        (OVERRIDE, compose_override(host.state_dir))):
        path = host.state_dir / name
        if path.is_symlink() or prep.parse_state_object(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Runtime schema файл не совпадает с durable state")
        if path.stat().st_nlink != 1:
            raise ValueError("Runtime schema файл имеет hardlink")
        info = path.stat()
        if os.name == "posix" and (stat.S_IMODE(info.st_mode) != 0o600
                                   or info.st_uid != os.geteuid() or info.st_gid != os.getegid()):
            raise ValueError("Runtime schema файл не является приватным файлом controller")


def require_container(item: dict, state: dict, state_dir: Path) -> None:
    """Проверяет точные schema env/mounts либо полное отсутствие допуска.

    Args:
        item: Inspect одного канала, не предназначенный для журнала.
        state: Durable переход с необязательным runtime_schema.
        state_dir: Ожидаемый source приватных readonly файлов.

    Raises:
        ValueError: Runtime получил чужой, writable или неполный допуск.
    """
    schema = state.get("runtime_schema")
    values = [entry for entry in item["Config"].get("Env", [])
              if entry.split("=", 1)[0] in (runtime_schema.ADMISSION_ENV, runtime_schema.LAUNCH_ENV)]
    targets = (CONTAINER_ADMISSION, CONTAINER_LAUNCH)
    mounts = [mount for mount in item.get("Mounts", []) if mount.get("Destination") in targets]
    if schema is None:
        if values or mounts:
            raise ValueError("Runtime содержит незарегистрированный schema admission")
        return
    expected = [runtime_schema.ADMISSION_ENV + "=" + CONTAINER_ADMISSION,
                runtime_schema.LAUNCH_ENV + "=" + CONTAINER_LAUNCH]
    if sorted(values) != sorted(expected) or len(mounts) != 2:
        raise ValueError("Runtime schema env/mounts не подтверждены")
    for filename, target in ((ADMISSION, CONTAINER_ADMISSION), (LAUNCH, CONTAINER_LAUNCH)):
        found = [mount for mount in mounts if mount.get("Destination") == target]
        if (len(found) != 1 or found[0].get("Type") != "bind" or found[0].get("RW") is not False
                or found[0].get("Source") != str(state_dir / filename)):
            raise ValueError("Runtime schema mount не является точным readonly bind")
