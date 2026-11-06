"""Проверяет forward recovery v2 с настоящими state/SQLite и fake transport."""

from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_database_switch as switch
from scripts import deploy_forward_recovery as forward
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deploy_runtime as runtime
from tests.test_deploy_database_switch import IMAGE, MANIFEST, SHA
from tests.test_deploy_runtime import RuntimeHost


class ForwardHost(RuntimeHost):
    """Сохраняет реальные SQLite/state, подменяя только Docker и systemd."""

    def __init__(self, root: Path) -> None:
        """Создаёт state и следит за активностью oneshot unit."""
        super().__init__(root)
        self.main_active = False
        self.main_failed = False
        self.timer = {"enabled": False, "active": False}

    def unit_state(self, unit: str) -> str:
        """Возвращает состояние synthetic systemd unit."""
        if unit == prod.SERVICE:
            return "active" if self.main_active else "failed" if self.main_failed else "inactive"
        return "inactive"

    def unit_enabled(self, unit: str) -> bool:
        """Читает synthetic backup timer."""
        return self.timer["enabled"] if unit == prod.TIMER else False

    def unit_active(self, unit: str) -> bool:
        """Читает synthetic main или backup timer."""
        return self.main_active if unit == prod.SERVICE else self.timer["active"]

    def timer_snapshot(self) -> dict[str, bool]:
        """Копирует synthetic timer state."""
        return self.timer.copy()

    def timer_restore(self, snapshot: dict[str, bool]) -> None:
        """Возвращает сохранённый timer после финализации."""
        self.timer = snapshot.copy()

    def timer_suspend(self) -> None:
        """Оставляет synthetic backup выключенным при ошибке."""
        self.timer = {"enabled": False, "active": False}

    def stop_both(self) -> None:
        """Останавливает точные synthetic channel ID при ошибке."""
        for item in self.found.values():
            item["State"] = {"Running": False, "Status": "exited"}

    def require_recovery_evidence(self, state: dict) -> None:
        """Оставляет сеть за границей теста exact локального контракта."""

    def require_recovery_quiescence(self) -> None:
        """Требует marker и отсутствие synthetic writers."""
        if (self.unit_state(prod.SERVICE) != "inactive" or
                (self.state_dir / "deploy-in-progress").read_text() != "critical\n" or
                any(item["State"]["Running"] for item in self.found.values())):
            raise prod.DeployError("Writers не остановлены")

    def activate_unit(self) -> None:
        """Имитирует read-only adoption под занятым controller lock."""
        super().activate_unit()
        self.main_active = True

    def run(self, *args: str, env=None) -> str:
        """Допускает только reset-failed и Docker start точных ID."""
        if args[:2] == ("systemctl", "reset-failed"):
            self.commands.append(args)
            self.main_failed = False
            return ""
        return super().run(*args, env=env)


class ForwardRecoveryTests(unittest.TestCase):
    """Проверяет gate, durable intent и повтор после прерывания."""

    def _fixture(self, root: Path) -> tuple[ForwardHost, dict]:
        """Готовит отказ финализации после admission на настоящей новой БД."""
        host = ForwardHost(root)
        old = switch.identity(host.checkout / "data/app.db", "legacy", MANIFEST)
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST,
                 "controller_sha": "c" * 40, "old_sha": SHA, "old_image_id": IMAGE,
                 "old_tag": prep.pinned_tag(SHA),
                 "candidate": {"sha": SHA, "image_id": IMAGE, "tag": prep.pinned_tag(SHA),
                               "version": "v1.2.3", "run_id": 4, "run_attempt": 1},
                 "timer_before": {"enabled": True, "active": False},
                 "last_good": {"version": "v1.2.2", "sha": SHA,
                               "image_id": IMAGE, "tag": prep.pinned_tag(SHA)},
                 "compatibility_evidence": {"schema_fingerprint": old["fingerprint"]}}

        def record(stage: str, **changes) -> None:
            """Сохраняет каждый переход через настоящий atomic writer."""
            state.update(changes, stage=stage)
            prep.write_state(host.state_dir, host.checkout, state)

        switch.register_initial(host, state, record, prod._atomic_text)
        switch.transition(host, state, record, prod._atomic_text)
        for item in host.found.values():
            item["State"] = {"Running": False, "Status": "exited"}
        record("degraded", migration_attempted=False,
               rollback_origin_stage="finalization_intent",
               boot=runtime.boot_record(state, "success"))
        host.main_failed = True
        return host, state

    def _recover(self, host: ForwardHost) -> None:
        """Запускает recovery с реальным lock и изолированной Git-проверкой."""
        with patch.object(prep, "deploy_lock", return_value=nullcontext()), patch.object(
                prep, "require_clean_checkout", return_value=None):
            forward.recover(host)

    def test_recovers_same_ids_and_new_database(self) -> None:
        """Durable intent предшествует start; нет create и смены SQLite."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            selected = switch.read_selected(host, state)
            old_db = (host.checkout / "data/app.db").read_bytes()
            self._recover(host)
            state = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(state["stage"], "success")
            self.assertEqual(switch.read_selected(host, state), selected)
            self.assertEqual((host.checkout / "data/app.db").read_bytes(), old_db)
            self.assertFalse((host.state_dir / "deploy-in-progress").exists())
            self.assertEqual(host.timer, state["timer_before"])
            starts = [command for command in host.commands if command[:2] == ("docker", "start")]
            self.assertEqual(len(starts), 2)  # исходный deploy и единственный recovery start
            self.assertEqual(set(starts[-1][2:]), set(state["prepared_containers"].values()))
            self.assertFalse(any(command[:3] == ("docker", "compose", "create")
                                 for command in host.commands[len(host.commands) - 3:]))

    def test_rejects_changed_boundary_or_candidate(self) -> None:
        """Изменённые selector/admission/boot/ID закрывают start."""
        for field in ("rollback_origin_stage", "boot", "prepared_containers"):
            with self.subTest(field=field), TemporaryDirectory() as temporary:
                host, state = self._fixture(Path(temporary).resolve())
                if field == "rollback_origin_stage":
                    state[field] = "migration_started"
                elif field == "boot":
                    state[field] = {}
                else:
                    state[field] = {**state[field], "tg_assistant": "foreign"}
                prep.write_state(host.state_dir, host.checkout, state)
                before = list(host.commands)
                with self.assertRaises(prod.DeployError):
                    self._recover(host)
                self.assertEqual(host.commands, before)
                self.assertTrue((host.state_dir / "deploy-in-progress").exists())

    def test_rejects_selector_schema_or_evidence_drift(self) -> None:
        """Смена выбранной БД, схемы или CI evidence закрывает start."""
        for mutation in ("selector", "schema", "evidence"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as temporary:
                host, state = self._fixture(Path(temporary).resolve())
                if mutation == "selector":
                    prod._atomic_text(host.state_dir / "active-db.json",
                                      json.dumps(switch.selector(state, "old")))
                elif mutation == "schema":
                    with closing(sqlite3.connect(state["new"]["host_path"])) as connection:
                        connection.execute("CREATE TABLE unexpected(id INTEGER)")
                        connection.commit()
                else:
                    host.require_recovery_evidence = lambda _state: (_ for _ in ()).throw(
                        prod.DeployError("evidence changed"))  # type: ignore[method-assign]
                before = list(host.commands)
                with self.assertRaises((prod.DeployError, ValueError)):
                    self._recover(host)
                self.assertEqual(host.commands, before)

    def test_crash_after_unit_adoption_replays_finalization(self) -> None:
        """Повтор с уже восстановленным timer не создаёт и не запускает ID."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            boot = state["boot"]
            state.update(stage="runtime_adopted", forward_recovery=boot)
            prep.write_state(host.state_dir, host.checkout, state)
            host.marker(False)
            host.main_failed = False
            host.main_active = True
            host.timer = state["timer_before"].copy()
            for item in host.found.values():
                item["State"] = {"Running": True, "Status": "running"}
            before = len(host.commands)
            self._recover(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "success")
            self.assertFalse(any(command[:2] == ("docker", "start")
                                 for command in host.commands[before:]))

    def test_crash_after_marker_removal_replays_same_unit_adoption(self) -> None:
        """Снятый marker до systemd start не приводит к новому create."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            state.update(stage="finalization_intent", forward_recovery=state["boot"])
            prep.write_state(host.state_dir, host.checkout, state)
            host.marker(False)
            host.main_failed = False
            for item in host.found.values():
                item["State"] = {"Running": True, "Status": "running"}
            before = len(host.commands)
            self._recover(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "success")
            self.assertFalse(any(command[:2] == ("docker", "start")
                                 for command in host.commands[before:]))

    def test_retry_after_process_crash_uses_same_running_ids(self) -> None:
        """После durable intent и частичного start повтор не создаёт пару."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            boot = state["boot"]
            state.update(stage="forward_recovery_intent", forward_recovery=boot)
            prep.write_state(host.state_dir, host.checkout, state)
            host.main_failed = False
            host.found["tg_assistant"]["State"] = {"Running": True, "Status": "running"}
            before = len(host.commands)
            self._recover(host)
            state = prep.read_state(host.state_dir, host.checkout)
            starts = [command for command in host.commands[before:] if command[:2] == ("docker", "start")]
            self.assertEqual(starts, [("docker", "start", boot["containers"]["vk_assistant"])])
            self.assertEqual(state["stage"], "success")

    def test_failed_start_stays_degraded_with_marker(self) -> None:
        """Отказ после intent останавливает writers и сохраняет запрет boot."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            with patch.object(host, "verify_both", side_effect=prod.DeployError("health")):
                with self.assertRaises(prod.DeployError):
                    self._recover(host)
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(durable["stage"], "degraded")
            self.assertEqual(durable["forward_recovery"], durable["boot"])
            self.assertTrue((host.state_dir / "deploy-in-progress").exists())
            self.assertTrue(all(not item["State"]["Running"] for item in host.found.values()))
