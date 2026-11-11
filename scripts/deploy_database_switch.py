"""Переключает уникальные поколения SQLite до допуска новых polling-процессов."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from scripts import deploy_final_snapshot as snapshot
from scripts import deploy_metrics as metrics
from scripts import deploy_prepare as prep
from scripts import predeploy_compatibility as compatibility


# Версия отделяет новый протокол от legacy state без границы приёма.
PROTOCOL_VERSION = 2

# Формат записи о завершённой очистке прежнего поколения в durable state.
OLD_CLEANUP_VERSION = 1

# Только эти незавершённые стадии имеют определённый маршрут восстановления.
RECOVERABLE_STAGES = frozenset({
    "ready", "critical", "writers_stopped", "snapshot_verified",
    "candidate_copy_verified", "switch_intent", "database_selected",
    "runtime_prepared", "admission_recorded", "candidate_started",
    "rollback_started", "recovery_intent", "recovery_selected",
    "recovery_prepared", "recovery_verified",
    "finalization_intent", "runtime_adopted",
})


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
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar):
            sidecar_info = sidecar.lstat()
            if not stat.S_ISREG(sidecar_info.st_mode) or sidecar_info.st_nlink != 1:
                raise ValueError("SQLite sidecar имеет alias или не является обычным файлом")
    prep.validate_schema(path, manifest)
    with closing(compatibility.open_copy_readonly(path)) as connection:
        compatibility.require_copy_foreign_keys(connection)
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


def require_boundary(state: dict) -> None:
    """Проверяет версию, transition и монотонную привязку admission."""
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
    elif "admission" in state:
        raise ValueError("Сохранённый admission противоречит границе false")


def old_cleanup_record(state: dict) -> dict:
    """Строит привязку результата очистки к точному переходу.

    Сама привязка не разрешает удаление и не доказывает, что оно выполнено.

    Args:
        state: Durable состояние с зарегистрированными old и new.

    Returns:
        Версионированная запись для фиксации после проверенного удаления.

    Raises:
        KeyError: В состоянии отсутствует обязательная идентичность.
    """
    old, new = state["old"], state["new"]
    return {"version": OLD_CLEANUP_VERSION, "transition_id": state["transition_id"],
            "old_generation_id": old["generation_id"], "old_host_path": old["host_path"],
            "old_device": old["device"], "old_inode": old["inode"],
            "new_generation_id": new["generation_id"]}


def _require_cleaned_old(host, state: dict, selected: dict) -> None:
    """Подтверждает завершённую очистку без чтения удалённой SQLite.

    Запись возникает только после отдельной процедуры очистки. До её
    завершения отсутствие old остаётся отказом; активная new проверяется
    вызывающим кодом через require_identity.
    """
    old, new = state.get("old"), state.get("new")
    boot, candidate, good = state.get("boot"), state.get("candidate"), state.get("last_good")
    if not all(isinstance(item, dict) for item in (old, new, boot, candidate, good)):
        raise ValueError("Очистка не имеет полного состояния перехода")
    try:
        old_path = Path(old["host_path"])
        expected_name = ("app.db" if old["generation_id"] == "legacy"
                         else f'app-{old["generation_id"]}.db')
        record = old_cleanup_record(state)
        cleanup = state.get("old_generation_cleanup")
        valid = (isinstance(cleanup, dict) and cleanup.keys() == record.keys()
                 and "old_generation_cleanup_intent" not in state
                 and type(cleanup["version"]) is int and cleanup == record
                 and type(cleanup["old_device"]) is int and type(cleanup["old_inode"]) is int
                 and type(old["device"]) is int and type(old["inode"]) is int
                 and (old["generation_id"] == "legacy"
                      or re.fullmatch(r"[0-9a-f]{32}", old["generation_id"]) is not None)
                 and old_path.is_absolute() and old_path.resolve() == old_path
                 and old_path.parent == host.checkout / "data" and old_path.name == expected_name
                 and old["generation_id"] != new["generation_id"]
                 and state.get("stage") == "success" and state["admission_may_have_started"] is True
                 and selected == selector(state, "new")
                 and state.get("switch_intent") == {"from": old["generation_id"],
                                                     "to": new["generation_id"]}
                 and boot.get("outcome") == "success"
                 and boot.get("transition_id") == state["transition_id"]
                 and boot.get("generation_id") == new["generation_id"]
                 and boot.get("database_path") == new["container_path"]
                 and boot.get("sha") == candidate.get("sha") == good.get("sha")
                 and boot.get("image_id") == candidate.get("image_id") == good.get("image_id")
                 and boot.get("tag") == candidate.get("tag") == good.get("tag")
                 and good.get("version") == candidate.get("version"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Неполная запись очистки прежнего поколения") from error
    if not valid or any(os.path.lexists(str(old_path) + suffix)
                        for suffix in ("", "-wal", "-shm", "-journal")):
        raise ValueError("Очистка прежнего поколения не подтверждена")


def read_selected(host, state: dict) -> str:
    """Определяет выбранную БД, учитывая доказанную очистку старого файла.

    Args:
        host: Адаптер приватного состояния и каталога данных.
        state: Durable переход с selector и возможной записью очистки.

    Returns:
        Имя выбранной стороны: old или new.

    Raises:
        ValueError: Selector, поколение или запись очистки расходятся.
    """
    require_boundary(state)
    path = host.state_dir / "active-db.json"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name == "posix" and (info.st_uid != 0 or info.st_gid != 0
                                       or stat.S_IMODE(info.st_mode) != 0o600))):
        raise ValueError("Selector не является приватным обычным файлом")
    value = prep.parse_state_object(path.read_text(encoding="utf-8"))
    if "new" in state and state["new"]["generation_id"] == state["old"]["generation_id"]:
        raise ValueError("Новое поколение повторяет прежнее имя")
    if "old_generation_cleanup" in state:
        _require_cleaned_old(host, state, value)
        require_identity(state["new"], host.checkout / "data")
    else:
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
           transition_id=uuid.uuid4().hex, old=old, admission_may_have_started=False,
           registration_intent={"generation_id": old["generation_id"], "host_path": old["host_path"]})
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
    with metrics.stage("v2.final_copy"):
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
    with metrics.stage("v2.start_channels"):
        durable = prep.read_state(host.state_dir, host.checkout)
        if (durable is None or durable.get("admission_may_have_started") is not True
                or durable.get("admission") != state["admission"]
                or durable.get("prepared_containers") != ids or read_selected(host, durable) != "new"):
            raise ValueError("Durable admission не подтверждён")
        host.start_database_containers(ids, candidate["image_id"], new["container_path"])
    record("candidate_started")


def recovery_target(host, state: dict) -> str:
    """Проверяет направление возврата, не меняя selector или содержимое SQLite."""
    selected = read_selected(host, state)
    target = "new" if state["admission_may_have_started"] else "old"
    intent = state.get("recovery_intent")
    expected = {"transition_id": state["transition_id"],
                "from": state[selected]["generation_id"], "to": state[target]["generation_id"],
                "image_id": state["old_image_id"], "sha": state["old_sha"]}
    if intent is not None:
        # После обратного replace selector уже old, но intent всё ещё new -> old.
        sources = {state["old"]["generation_id"]}
        if "new" in state:
            sources.add(state["new"]["generation_id"])
        if (not isinstance(intent, dict) or intent.get("from") not in sources
                or (selected != target and intent.get("from") != state[selected]["generation_id"])
                or intent != expected | {"from": intent.get("from")}):
            raise ValueError("Неоднозначный recovery intent")
    return target


def recover(host, state: dict, record, publish) -> None:
    """Возвращает прежний runtime на допустимое поколение под deploy lock.

    После durable admission сохраняет новую SQLite и требует повторный CI
    допуск точной пары. Marker и остановленный timer остаются до отдельной
    финализации boot-контракта вызывающим controller.
    Ни snapshot, ни поколения и sidecars не удаляются и не перезаписываются.

    Args:
        host: Адаптер Docker/systemd с закрытым автозапуском.
        state: Перечитанное durable состояние перехода.
        record: Durable writer стадии и полей recovery.
        publish: Atomic writer selector с синхронизацией каталога.

    Raises:
        ValueError: Граница, поколение или направление recovery не доказаны.
    """
    host.require_recovery_quiescence()
    require_boundary(state)
    if (state.get("protocol_version") != PROTOCOL_VERSION
            or state.get("stage") not in RECOVERABLE_STAGES):
        raise ValueError("Неизвестный или degraded протокол")
    host.require_recovery_image(state)
    if "new" in state or "snapshot" in state:
        evidence = state["snapshot"]
        path = Path(evidence["path"])
        if (path.parent != host.state_dir or path.resolve() != path
                or not re.fullmatch(r"final-[0-9a-f]{32}\.db", path.name)
                or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_nlink != 1
                or evidence.get("integrity") is not True or evidence.get("foreign_keys") is not True
                or any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
            raise ValueError("Окончательный snapshot не подтверждён")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != evidence["sha256"]:
                raise ValueError("Digest окончательного snapshot изменился")
    # Только точное сохранённое намерение разрешает завершить регистрацию.
    # Отсутствие selector в любом другом состоянии не означает legacy app.db.
    path = host.state_dir / "active-db.json"
    if not os.path.lexists(path):
        old = state["old"]
        if (state.get("admission_may_have_started") is not False or "new" in state
                or state.get("registration_intent") != {
                    "generation_id": old["generation_id"], "host_path": old["host_path"]}):
            raise ValueError("Selector утрачен без registration intent")
        require_identity(old, host.checkout / "data")
        host.require_recovery_observation(state)
        publish(path, json.dumps(selector(state, "old")) + "\n")
    elif state.get("selector_rebind") is not None:
        rebind = state["selector_rebind"]
        observed = prep.parse_state_object(compatibility.private_text(path))
        if observed == rebind.get("from"):
            if (state.get("admission_may_have_started") is not False or "new" in state
                    or rebind.get("to") != selector(state, "old")
                    or observed != selector(state, "old") | {"transition_id": observed.get("transition_id")}
                    or not re.fullmatch(r"[0-9a-f]{32}", str(observed.get("transition_id", "")))):
                raise ValueError("Неоднозначная регистрация нового перехода")
            require_identity(state["old"], host.checkout / "data")
            host.require_recovery_observation(state)
            publish(path, json.dumps(selector(state, "old")) + "\n")
    target = recovery_target(host, state)
    host.require_recovery_observation(state)
    if target == "new":
        host.require_recovery_evidence(state)
    selected = read_selected(host, state)
    intent = state.get("recovery_intent") or {
        "transition_id": state["transition_id"], "from": state[selected]["generation_id"],
        "to": state[target]["generation_id"], "image_id": state["old_image_id"], "sha": state["old_sha"]}
    record("recovery_intent", recovery_intent=intent)
    if selected != target:
        publish(path, json.dumps(selector(state, target)) + "\n")
    if read_selected(host, state) != target:
        raise ValueError("Поколение recovery не подтверждено")
    record("recovery_selected", selected_generation=state[target]["generation_id"])
    host.restore_checkout(state["old_sha"])
    host.prepare_runtime_schema(state, record)
    database_path = state[target]["container_path"]
    host.set_database_runtime(state["old_tag"], state["old_image_id"], database_path, recovery=True)
    ids = host.prepare_database_containers(state["old_tag"], state["old_image_id"], database_path, recovery=True)
    record("recovery_prepared", recovery_containers=ids)
    host.start_recovery_containers(ids)
    host.verify_both(state["old_image_id"], state["old_sha"])
    # Вызывающий controller отдельно фиксирует boot intent и финализирует unit/timer.
    record("recovery_verified")
