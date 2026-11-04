"""Переключает уникальные поколения SQLite до допуска новых polling-процессов."""

from contextlib import closing
import json
import os
from pathlib import Path
import re
import stat
import uuid

from scripts import deploy_final_snapshot as snapshot
from scripts import deploy_prepare as prep
from scripts import predeploy_compatibility as compatibility


# Версия отделяет новый протокол от legacy state без границы приёма.
PROTOCOL_VERSION = 2


def identity(path: Path, generation: str, manifest: list[str]) -> dict:
    """Проверяет существующий файл и описывает его поколение и схему."""
    if path.resolve() != path or not path.is_absolute():
        raise ValueError("Неканонический путь поколения")
    expected = "app.db" if generation == "legacy" else f"app-{generation}.db"
    if (generation != "legacy" and not re.fullmatch(r"[0-9a-f]{32}", generation)) or path.name != expected:
        raise ValueError("Некорректное имя поколения")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("SQLite имеет alias или не является обычным файлом")
    prep.validate_schema(path, manifest)
    with closing(compatibility.open_copy_readonly(path)) as connection:
        fingerprint = compatibility.schema_fingerprint(connection)
    return {"generation_id": generation, "host_path": str(path),
            "container_path": f"/app/data/{path.name}", "device": info.st_dev,
            "inode": info.st_ino, "manifest": manifest, "fingerprint": fingerprint}


def require_identity(value: dict, data: Path) -> None:
    """Сверяет файл, имя и схему с зарегистрированной идентичностью."""
    path = Path(value["host_path"])
    if path.parent != data or identity(path, value["generation_id"], value["manifest"]) != value:
        raise ValueError("Идентичность поколения изменилась")


def selector(state: dict, side: str) -> dict:
    """Выводит единственный допустимый selector из зарегистрированного перехода."""
    item = state[side]
    return {"protocol_version": PROTOCOL_VERSION, "transition_id": state["transition_id"],
            "generation_id": item["generation_id"], "name": Path(item["host_path"]).name}


def read_selected(host, state: dict) -> str:
    """Определяет фактически выбранную БД без догадок по stage или runtime.env."""
    if (state.get("protocol_version") != PROTOCOL_VERSION
            or type(state.get("admission_may_have_started")) is not bool
            or not re.fullmatch(r"[0-9a-f]{32}", str(state.get("transition_id", "")))):
        raise ValueError("Недоказанная версия или граница приёма")
    if state["admission_may_have_started"]:
        expected_admission = {"transition_id": state["transition_id"],
                              "generation_id": state["new"]["generation_id"],
                              "image_id": state["candidate"]["image_id"],
                              "sha": state["candidate"]["sha"]}
        if state.get("admission") != expected_admission:
            raise ValueError("Граница приёма не привязана к поколению и runtime")
    path = host.state_dir / "active-db.json"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name == "posix" and (info.st_uid != 0 or info.st_gid != 0
                                       or stat.S_IMODE(info.st_mode) != 0o600))):
        raise ValueError("Selector не является приватным обычным файлом")
    value = json.loads(path.read_text(encoding="utf-8"))
    if "new" in state and state["new"]["generation_id"] == state["old"]["generation_id"]:
        raise ValueError("Новое поколение повторяет прежнее имя")
    for side in ("old", "new"):
        if side in state:
            require_identity(state[side], host.checkout / "data")
    for side in ("old", "new"):
        if side in state and value == selector(state, side):
            if side == "new" and state.get("switch_intent") != {
                    "from": state["old"]["generation_id"], "to": state["new"]["generation_id"]}:
                raise ValueError("Новая SQLite выбрана без durable intent")
            if state["admission_may_have_started"] and side != "new":
                raise ValueError("После границы выбрано прежнее поколение")
            return side
    raise ValueError("Selector не принадлежит переходу")


def register_initial(host, state: dict, record, publish) -> None:
    """Регистрирует legacy app.db на месте только под остановленными writers.

    Вызывающий обязан отдельно доказать стабильность прежнего runtime по 6.5.
    Повтор после частичной регистрации требует recovery, а не перезаписи selector.
    """
    host.require_snapshot_quiescence()
    if os.path.lexists(host.state_dir / "active-db.json") or "protocol_version" in state:
        raise ValueError("Начальная регистрация уже выполнена или прервана")
    old = identity(host.checkout / "data/app.db", "legacy", state["schema_before"])
    record("writers_stopped", protocol_version=PROTOCOL_VERSION,
           transition_id=uuid.uuid4().hex, old=old, admission_may_have_started=False)
    publish(host.state_dir / "active-db.json", json.dumps(selector(state, "old")) + "\n")
    if read_selected(host, state) != "old":
        raise ValueError("Начальное поколение не подтверждено")


def transition(host, state: dict, record, publish) -> None:
    """Готовит окончательную копию, выбирает её и запускает проверенные каналы.

    Args:
        host: Адаптер с остановленными writers под внешним deploy lock.
        state: Подготовленное состояние с old, controller_sha и CI evidence.
        record: Durable-запись стадии и изменений состояния.
        publish: Atomic write с fsync файла и родительского каталога.

    Raises:
        ValueError: Selector, идентичность или durable-граница не подтверждены.
    """
    host.require_snapshot_quiescence()
    if state.get("stage") != "writers_stopped" or state.get("admission_may_have_started") is not False:
        raise ValueError("Переход уже начат или writers не остановлены")
    if read_selected(host, state) != "old":
        raise ValueError("Исходное поколение не выбрано")
    if not re.fullmatch(r"[0-9a-f]{40}", str(state.get("controller_sha", ""))):
        raise ValueError("SHA установленного controller не подтверждён")
    generation = uuid.uuid4().hex
    evidence = state["compatibility_evidence"]
    candidate = state["candidate"]
    path = snapshot.prepare(
        Path(state["old"]["host_path"]), host.state_dir, generation,
        state["schema_before"], state["schema_after"], evidence["schema_fingerprint"],
        require_quiescence=host.require_snapshot_quiescence,
        migrate=lambda work: host.migrate_copy(candidate["image_id"], work.parent, basename=work.name),
        record_snapshot=lambda item: record("snapshot_verified", snapshot={
            "path": str(item.path), "sha256": item.sha256, "integrity": True, "foreign_keys": True}),
    )
    new = identity(path, generation, state["schema_after"])
    record("candidate_copy_verified", new=new)
    record("switch_intent", switch_intent={"from": state["old"]["generation_id"], "to": generation})
    host.require_snapshot_quiescence()
    publish(host.state_dir / "active-db.json", json.dumps(selector(state, "new")) + "\n")
    if read_selected(host, state) != "new":
        raise ValueError("Публикация selector не подтверждена")
    record("database_selected", selected_generation=generation)
    host.set_database_runtime(candidate["tag"], candidate["image_id"], new["container_path"])
    ids = host.prepare_database_containers(candidate["tag"], candidate["image_id"], new["container_path"])
    record("runtime_prepared", prepared_containers=ids)
    # Даже отказ start после этой durable-записи запрещает возврат к old.
    record("admission_recorded", admission_may_have_started=True,
           admission={"transition_id": state["transition_id"], "generation_id": generation,
                      "image_id": candidate["image_id"], "sha": candidate["sha"]})
    durable = prep.read_state(host.state_dir, host.checkout)
    if (durable is None or durable.get("admission_may_have_started") is not True
            or durable.get("admission") != state["admission"]
            or durable.get("prepared_containers") != ids or read_selected(host, durable) != "new"):
        raise ValueError("Durable admission не подтверждён")
    host.start_database_containers(ids, candidate["image_id"], new["container_path"])
    record("candidate_started")
