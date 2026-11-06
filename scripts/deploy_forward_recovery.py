"""Продолжает единственный проверенный v2 candidate после degraded финализации."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_runtime as runtime
from scripts import deploy_prod as prod


FORWARD_STAGES = frozenset({"forward_recovery_intent", "forward_recovery_verified",
                            "finalization_intent", "runtime_adopted"})


def _require_exact_candidate(host: prod.Host, state: dict) -> dict:
    """Сверяет границу, выбранную SQLite и прежние ID перед любой записью.

    Args:
        host: Production transport с внешним state.
        state: Прочитанное durable состояние исходного выпуска.

    Returns:
        Точный boot intent сохранённого candidate.

    Raises:
        DeployError: Другая история, БД, runtime или контейнеры не доказаны.
    """
    if (state.get("stage") not in FORWARD_STAGES | {"degraded"}
            or state.get("rollback_origin_stage") != "finalization_intent"
            or state.get("protocol_version") != switch.PROTOCOL_VERSION
            or state.get("migration_attempted") is not False):
        raise prod.DeployError("Forward recovery допустим только после отказа финализации без новой миграции")
    boot = runtime.boot_record(state, "success")
    if state.get("boot") != boot or state.get("forward_recovery", boot) != boot:
        raise prod.DeployError("Candidate boot или forward intent изменился")
    if state.get("stage") in FORWARD_STAGES and state.get("forward_recovery") != boot:
        raise prod.DeployError("Нет durable forward intent для повтора")
    if switch.read_selected(host, state) != "new":
        raise prod.DeployError("Forward recovery требует выбранную новую SQLite")
    prep.require_clean_checkout(host.checkout, boot["sha"])
    if host.runtime_tag() != boot["tag"]:
        raise prod.DeployError("Candidate runtime pin изменился")
    for reference in (boot["tag"], boot["image_id"]):
        image = host.image(reference)
        if (image["Id"] != boot["image_id"] or
                (image.get("Config", {}).get("Labels") or {}).get(prep.OCI_REVISION) != boot["sha"]):
            raise prod.DeployError("Candidate OCI pin изменился")
    host.require_recovery_evidence(state)
    # verify сверяет оба точных ID, env/mount, schema admission и чужие writers.
    check = state.copy()
    check["stage"] = "finalization_intent"
    runtime.verify(host, check, require_running=False, allow_marker=True)
    return boot


def recover(host: prod.Host) -> None:
    """Под lock возобновляет те же ID на NEW DB и завершает systemd adoption.

    Повтор после process crash использует сохранённый intent и не создаёт
    контейнеров либо поколений SQLite. При отказе оставляет degraded guards.

    Args:
        host: Production transport с установленным controller snapshot.

    Raises:
        DeployError: Недоказанный контракт или отказ возобновления каналов.
    """
    with prep.deploy_lock(host.state_dir, host.checkout):
        state = prep.read_state(host.state_dir, host.checkout)
        if not state:
            raise prod.DeployError("Отсутствует durable state для forward recovery")
        host.require_switch_rollout()
        boot = _require_exact_candidate(host, state)
        marker = host.state_dir / "deploy-in-progress"
        if (state["stage"] in ("degraded", "forward_recovery_intent", "forward_recovery_verified")
                and marker.read_text(encoding="utf-8") != "critical\n"):
            raise prod.DeployError("Forward recovery без критического marker запрещён")
        if (state["stage"] != "runtime_adopted" and
                (host.unit_enabled(prod.TIMER) or host.unit_active(prod.TIMER) or
                 host.unit_state(prod.BACKUP_SERVICE) != "inactive")):
            raise prod.DeployError("Backup не остановлен до forward recovery")
        if state["stage"] == "runtime_adopted" and host.timer_snapshot() not in (
                {"enabled": False, "active": False}, state["timer_before"]):
            raise prod.DeployError("Backup timer изменился вне финализации")
        if host.unit_state(prod.SERVICE) not in ("failed", "inactive", "active"):
            raise prod.DeployError("Неоднозначное состояние основного unit")
        try:
            if state["stage"] == "degraded":
                if host.unit_state(prod.SERVICE) == "failed":
                    host.run("systemctl", "reset-failed", prod.SERVICE)
                if host.unit_state(prod.SERVICE) != "inactive":
                    raise prod.DeployError("Основной unit не inactive после reset-failed")
                host.require_recovery_quiescence()
                prod._state(host, state, "forward_recovery_intent", forward_recovery=boot)
            if state["stage"] in ("forward_recovery_intent", "forward_recovery_verified"):
                if host.unit_state(prod.SERVICE) != "inactive":
                    raise prod.DeployError("Основной unit запущен до финализации")
            found = host.containers()
            stopped = [boot["containers"][name] for name in prep.CHANNELS
                       if not found[name]["State"]["Running"]]
            if stopped:
                host.run("docker", "start", *stopped)
            current = prep.read_state(host.state_dir, host.checkout)
            if not current or {key: value for key, value in current.items() if key != "updated_at"} != {
                    key: value for key, value in state.items() if key != "updated_at"}:
                raise prod.DeployError("Forward recovery state изменился во время start")
            check = state.copy()
            check["stage"] = "finalization_intent"
            runtime.verify(host, check, require_running=True, allow_marker=True)
            host.verify_both(boot["image_id"], boot["sha"])
            if state["stage"] == "forward_recovery_intent":
                prod._state(host, state, "forward_recovery_verified")
            host.finalize_database(state, "success")
        except Exception as error:
            prod._degrade(host, state, "Forward recovery не подтверждён")
            raise prod.DeployError("Forward recovery не подтверждён; guards сохранены") from error


def main() -> None:
    """Разрешает root-only CLI только из установленного snapshot с safe path."""
    try:
        if (os.name != "posix" or os.geteuid() != 0 or sys.argv[1:]
                or Path(__file__).resolve().parents[1] != Path("/usr/local/lib/compadre-assistant-deploy")
                or not sys.flags.safe_path):
            raise prod.DeployError("Forward recovery требует root controller snapshot и -P")
        recover(prod.Host())
    except Exception as error:
        print(f"deploy-forward-recovery: отказ {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
