"""Проверяет durable boot-контракт и запускает только существующую пару ID."""

from __future__ import annotations

import os
import sys

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts.deploy_prod import DeployError, Host


# Эти стадии разрешают boot, но не дают разрешения менять выбранное поколение.
BOOT_STAGES = frozenset({"finalization_intent", "runtime_adopted", "success", "rolled_back"})


def boot_record(state: dict, outcome: str) -> dict:
    """Выводит точный boot-контракт из admission либо завершённого recovery.

    Args:
        state: Durable состояние перехода v2.
        outcome: Итог success или rolled_back.

    Raises:
        DeployError: Итог, граница или ранее проверенные ID неоднозначны.
    """
    switch.require_boundary(state)
    if outcome == "success":
        if state["admission_may_have_started"] is not True:
            raise DeployError("Успешный boot без durable admission запрещён")
        side, runtime, ids = "new", state["candidate"], state["prepared_containers"]
    elif outcome == "rolled_back":
        side = "new" if state["admission_may_have_started"] else "old"
        runtime = {"sha": state["old_sha"], "image_id": state["old_image_id"], "tag": state["old_tag"]}
        ids = state["recovery_containers"]
        intent = state.get("recovery_intent", {})
        if (intent.get("to") != state[side]["generation_id"]
                or intent.get("image_id") != runtime["image_id"] or intent.get("sha") != runtime["sha"]):
            raise DeployError("Возврат не имеет точного durable intent")
    else:
        raise DeployError("Неизвестный итог boot")
    if set(ids) != set(prep.CHANNELS) or len(set(ids.values())) != len(prep.CHANNELS):
        raise DeployError("Boot требует полную уникальную пару ID")
    return {"outcome": outcome, "transition_id": state["transition_id"],
            "generation_id": state[side]["generation_id"], "database_path": state[side]["container_path"],
            "sha": runtime["sha"], "image_id": runtime["image_id"], "tag": runtime["tag"],
            "containers": ids}


def verify(host: Host, state: dict, *, require_running: bool, allow_marker: bool = False) -> dict:
    """Сверяет boot, selector, image/OCI, env/mounts и фактические ID без записи.

    Проверка не вызывает compose up/create и не заменяет отсутствующий контейнер.

    Args:
        host: Адаптер Docker/systemd и внешнего состояния.
        state: Durable состояние перехода с boot intent.
        require_running: Требовать уже работающую пару для read-only adoption.
        allow_marker: Разрешить marker только внутри controller finalization.

    Returns:
        Подтверждённый boot contract.

    Raises:
        DeployError: Запуск, pin, поколение или фактическая пара не доказаны.
    """
    if not allow_marker and os.path.lexists(host.state_dir / "deploy-in-progress"):
        raise DeployError("Boot закрыт marker")
    if os.path.lexists(host.state_dir / "recovery-blocked.json"):
        raise DeployError("Boot закрыт recovery marker")
    if state.get("stage") not in BOOT_STAGES:
        raise DeployError("Стадия не разрешает штатный boot")
    boot = state.get("boot")
    if not isinstance(boot, dict) or boot != boot_record(state, boot.get("outcome")):
        raise DeployError("Durable boot intent не подтверждён")
    side = switch.read_selected(host, state)
    if state[side]["generation_id"] != boot["generation_id"] or host.runtime_tag() != boot["tag"]:
        raise DeployError("Boot selector/runtime pin не совпадают")
    image = host.image(boot["tag"])
    if (image["Id"] != boot["image_id"]
            or image.get("Config", {}).get("Labels", {}).get(prep.OCI_REVISION) != boot["sha"]):
        raise DeployError("Boot image ID/revision не подтверждены")
    found = host.containers()
    if set(found) != set(prep.CHANNELS):
        raise DeployError("Boot обнаружил неполную пару")
    for name, item in found.items():
        paths = [entry for entry in item["Config"].get("Env", []) if entry.startswith("DATABASE_PATH=")]
        mounts = [mount for mount in item.get("Mounts", []) if mount.get("Destination") == "/app/data"]
        if (item["Id"] != boot["containers"][name] or item.get("Image") != boot["image_id"]
                or paths != ["DATABASE_PATH=" + boot["database_path"]]
                or item["HostConfig"]["RestartPolicy"]["Name"] != "no" or item.get("RestartCount") != 0):
            raise DeployError("Boot runtime не совпадает с intent")
        if (len(mounts) != 1 or mounts[0].get("Type") != "bind"
                or mounts[0].get("Source") != str(host.checkout / "data") or mounts[0].get("RW") is not True
                or any(mount.get("Destination", "").startswith("/app/data/")
                       or (mount.get("Source") == str(host.checkout / "data")
                           and mount.get("Destination") != "/app/data") for mount in item.get("Mounts", []))):
            raise DeployError("Boot data mount не подтверждён")
        if item["State"].get("Status") not in ("running", "exited", "created"):
            raise DeployError("Boot контейнер не в устойчивом состоянии")
        if require_running and item["State"].get("Running") is not True:
            raise DeployError("Read-only adoption требует уже работающую пару")
        host.require_runtime_schema(item, state)
    host.require_no_foreign_database_mounts(set(boot["containers"].values()))
    host.require_no_duplicate_polling()
    return boot


def _start_locked(host: Host) -> None:
    state = prep.read_state(host.state_dir, host.checkout) or {}
    host.require_switch_rollout()
    boot = verify(host, state, require_running=False)
    if boot["outcome"] == "rolled_back" and state["admission_may_have_started"]:
        host.require_recovery_evidence(state)
    # Ни одного нового ID или нового compose create при повторе boot.
    found = host.containers()
    stopped = [boot["containers"][name] for name in prep.CHANNELS if not found[name]["State"]["Running"]]
    if stopped:
        host.run("docker", "start", *stopped)
    verify(host, prep.read_state(host.state_dir, host.checkout) or {}, require_running=True)
    host.verify_both(boot["image_id"], boot["sha"])


def start(host: Host) -> None:
    """Под lock запускает пару либо без lock только принимает уже работающую.

    При systemctl start из controller lock уже удерживается родителем.
    В этом случае helper не меняет Docker: только read-only adoption точных ID.
    Это исключает взаимоблокировку oneshot unit и не разрешает гонку start.

    Args:
        host: Адаптер внешнего состояния и Docker/systemd.

    Raises:
        DeployError: Gate, boot-контракт или фактические контейнеры не прошли проверку.
    """
    try:
        lock = prep.deploy_lock(host.state_dir, host.checkout)
        lock.__enter__()
    except prep.PreparationError:
        state = prep.read_state(host.state_dir, host.checkout) or {}
        host.require_switch_rollout()
        boot = verify(host, state, require_running=True)
        current = prep.read_state(host.state_dir, host.checkout) or {}
        if current.get("boot") != boot:
            raise DeployError("Boot intent изменился во время adoption")
        verify(host, current, require_running=True)
    else:
        try:
            _start_locked(host)
        finally:
            lock.__exit__(None, None, None)


def stop(host: Host) -> None:
    """Останавливает найденные channel ID без чтения selector и без запуска.

    Stop не берёт deploy lock: controller уже удерживает его при systemctl stop.
    Остановка writers допустима и при повреждённом состоянии.

    Args:
        host: Адаптер поиска и остановки известных каналов.

    Raises:
        DeployError: Inventory или остановка writers не подтверждены.
    """
    found = host.recovery_inventory()
    for item in found.values():
        host.run("docker", "update", "--restart=no", item["Id"])
        host.run("docker", "stop", item["Id"])
    if any(item["State"].get("Running") for item in host.recovery_inventory().values()):
        raise DeployError("Штатный stop не остановил writers")


def finalize(host: Host, state: dict, record, outcome: str) -> None:
    """Durable-порядком возвращает oneshot unit и timer без нового polling ID.

    Повтор finalization_intent/runtime_adopted использует тот же boot contract.
    Ошибка возвращается controller для recovery; данные не восстанавливаются.

    Args:
        host: Адаптер Docker/systemd под controller deploy lock.
        state: Durable состояние проверенного candidate/recovery runtime.
        record: Atomic state writer с fsync файла и каталога.
        outcome: Итог success или rolled_back.

    Raises:
        DeployError: Intent, unit, runtime или timer не подтверждены.
    """
    host.require_switch_rollout()
    boot = boot_record(state, outcome)
    if state.get("stage") in ("finalization_intent", "runtime_adopted"):
        if state.get("boot") != boot:
            raise DeployError("Повтор финализации изменил intent")
    else:
        expected = ({"candidate_started", "forward_recovery_verified"} if outcome == "success"
                    else {"recovery_verified"})
        if state.get("stage") not in expected:
            raise DeployError("Финализация требует проверенный runtime")
        if state.get("stage") == "forward_recovery_verified" and state.get("forward_recovery") != boot:
            raise DeployError("Forward recovery не имеет точного durable intent")
        record("finalization_intent", boot=boot)
    verify(host, state, require_running=True, allow_marker=True)
    host.marker(False)
    host.activate_unit()
    verify(host, state, require_running=True)
    host.verify_both(boot["image_id"], boot["sha"])
    record("runtime_adopted")
    host.timer_restore(state["timer_before"])
    if outcome == "success":
        candidate = state["candidate"]
        good = {"version": candidate["version"], "sha": candidate["sha"], "image_id": candidate["image_id"],
                "tag": candidate["tag"], "backup_id": state["snapshot"]["path"],
                "ci_run_id": candidate["run_id"], "ci_run_attempt": candidate["run_attempt"],
                "kind": candidate.get("kind", "ordinary"), "deployment": state.get("deployment")}
    else:
        good = state["last_good"]
    record(outcome, last_good=good, error=None)


def main() -> None:
    """Обрабатывает root-only start/stop для отдельного systemd drop-in."""
    try:
        if os.name != "posix" or os.geteuid() != 0 or sys.argv[1:] not in (["start"], ["stop"]):
            raise DeployError("Runtime helper предназначен для root start/stop на Linux")
        (start if sys.argv[1] == "start" else stop)(Host())
    except Exception as error:
        print(f"deploy-runtime: отказ {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
