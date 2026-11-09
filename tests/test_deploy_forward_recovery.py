"""Проверяет forward recovery v2 с настоящими state/SQLite и fake transport."""

from contextlib import closing, nullcontext
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_database_switch as switch
from scripts import deploy_forward_recovery as forward
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deploy_runtime as runtime
from scripts.select_deploy_release import SelectedRelease
from tests.test_deploy_database_recovery import OLD_IMAGE, OLD_SHA, annotated_release
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

    def recovery_inventory(self) -> dict:
        """Возвращает известные Docker ID без чтения selector."""
        return self.found

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
        if args[:2] in (("docker", "update"), ("docker", "stop")):
            self.commands.append(args)
            if args[1] == "stop":
                for item in self.found.values():
                    if item["Id"] == args[2]:
                        item["State"] = {"Running": False, "Status": "exited"}
            return ""
        return super().run(*args, env=env)


class AnnotatedForwardHost(ForwardHost):
    """Проверяет настоящий аннотированный тег, подменяя только внешний транспорт."""

    def __init__(self, root: Path) -> None:
        """Создаёт точный локальный Git checkout и синтетические внешние pin/token."""
        super().__init__(root)
        self.selected = SelectedRelease(prod.EXPECTED_REPOSITORY, "v1.2.3", "release/1.2", SHA,
                                        5, "main-full.yml", 8, 1)
        old = switch.identity(self.checkout / "data/app.db", "legacy", MANIFEST)
        self.api = annotated_release(self, old["fingerprint"])
        with (self.checkout / ".git/info/exclude").open("a", encoding="utf-8") as exclude:
            exclude.write("\n/data/\n")
        subprocess.run(("git", "checkout", "--detach", "--quiet", self.selected.sha),
                       cwd=self.checkout, check=True)
        prod._atomic_text(self.state_dir / "github-read-token", "synthetic-test-token")

    def select(self, version: str) -> SelectedRelease:
        """Возвращает смоделированный результат точного селектора Release и CI."""
        if version != self.selected.version:
            raise AssertionError(version)
        return self.selected

    def image(self, reference: str) -> dict:
        """Возвращает разные OCI SHA прежнего образа и кандидата из локального Git."""
        old = reference in (OLD_IMAGE, prep.pinned_tag(OLD_SHA))
        return {"Id": OLD_IMAGE if old else IMAGE,
                "Config": {"Labels": {prep.OCI_REVISION: OLD_SHA if old else self.selected.sha}}}

    def require_recovery_evidence(self, state: dict) -> None:
        """Выполняет проверку внешнего pin, локального тега и GitHub Contents."""
        prod.Host.require_recovery_evidence(self, state)

    def run(self, *args: str, env=None) -> str:
        """Моделирует stop unit и поиск точных ID для защиты после отказа."""
        if args[:2] == ("systemctl", "stop"):
            self.commands.append(args)
            self.main_active = self.main_failed = False
            return ""
        if args[:3] == ("docker", "ps", "--all"):
            self.commands.append(args)
            channel = args[-1].removeprefix("label=com.docker.compose.service=")
            return self.found[channel]["Id"]
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

        self._start_candidate(host, state)
        for item in host.found.values():
            item["State"] = {"Running": False, "Status": "exited"}
        state.update(stage="degraded", migration_attempted=False,
                     rollback_origin_stage="finalization_intent",
                     boot=runtime.boot_record(state, "success"))
        prep.write_state(host.state_dir, host.checkout, state)
        host.main_failed = True
        return host, state

    def _start_candidate(self, host: ForwardHost, state: dict) -> None:
        def record(stage: str, **changes) -> None:
            """Сохраняет каждый переход через настоящий atomic writer."""
            state.update(changes, stage=stage)
            prep.write_state(host.state_dir, host.checkout, state)

        switch.register_initial(host, state, record, prod._atomic_text)
        switch.transition(host, state, record, prod._atomic_text)

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

    def test_annotated_finalization_failure_recovers_only_the_same_release(self) -> None:
        """Отказ adoption и недоступность CI оставляют ту же БД для ручного forward."""
        with TemporaryDirectory() as temporary:
            host = AnnotatedForwardHost(Path(temporary).resolve())
            selected = host.selected
            candidate = asdict(selected) | {"image_id": IMAGE, "tag": prep.pinned_tag(selected.sha)}
            evidence = prod.compatibility.verify_tag_evidence(host.api, host.checkout, selected,
                                                              selected.compatibility.policy_sha, OLD_SHA)
            state = {"schema_before": MANIFEST, "schema_after": MANIFEST,
                     "controller_sha": "c" * 40, "old_sha": OLD_SHA, "old_image_id": OLD_IMAGE,
                     "old_tag": prep.pinned_tag(OLD_SHA), "candidate": candidate,
                     "timer_before": {"enabled": True, "active": True}, "migration_attempted": False,
                     "last_good": {"version": "v1.2.2", "sha": OLD_SHA,
                                   "image_id": OLD_IMAGE, "tag": prep.pinned_tag(OLD_SHA)},
                     "compatibility_evidence": evidence}
            self._start_candidate(host, state)
            ids = state["prepared_containers"]
            generation = state["new"]["generation_id"]
            with closing(sqlite3.connect(state["new"]["host_path"])) as db:
                db.execute("INSERT INTO messages VALUES(2, 'accepted-after-admission')")
                db.commit()
            before = len(host.commands)

            with patch.object(host, "activate_unit", side_effect=prod.DeployError("adoption failure")):
                with self.assertRaisesRegex(prod.DeployError, "adoption failure"):
                    host.finalize_database(state, "success")
            self.assertEqual(state["stage"], "finalization_intent")
            prod._state(host, state, "rollback_started", rollback_origin_stage=state["stage"])
            with patch.object(prep, "deploy_lock", return_value=nullcontext()), patch.object(
                prod.compatibility, "private_text", side_effect=lambda path: path.read_text().strip(),
            ), patch.object(prod, "GitHub", return_value=host.api), patch.object(
                prod.compatibility, "EvidenceAPI", side_effect=AssertionError("unexpected artifact API"),
            ):
                with patch.object(host.api, "get", side_effect=OSError("CI unavailable")) as unavailable:
                    with self.assertRaisesRegex(prod.DeployError, "SQLite recovery не подтверждён"):
                        prod._recover(host, state)
                unavailable.assert_called_once()
                blocked = prep.read_state(host.state_dir, host.checkout)
                self.assertEqual(blocked["stage"], "degraded")
                self.assertEqual(blocked["last_good"]["sha"], OLD_SHA)
                self.assertTrue(all(not item["State"]["Running"] for item in host.found.values()))
                self.assertEqual((host.state_dir / "deploy-in-progress").read_text(), "critical\n")
                self.assertEqual(host.timer, {"enabled": False, "active": False})
                self.assertEqual(switch.read_selected(host, state), "new")

                host.selected = replace(selected, run_attempt=selected.run_attempt + 1)
                with self.assertRaisesRegex(prod.DeployError, "writers остановлены и guards подтверждены") as error:
                    forward.recover(host)
                self.assertIn("CI attempt изменился", str(error.exception.__cause__))
                self.assertFalse(any(command[:2] == ("docker", "start") for command in host.commands[before:]))
                host.selected = selected
                with patch.object(host.api, "get", wraps=host.api.get) as get:
                    forward.recover(host)
                self.assertEqual(get.call_count, 4)

            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(durable["stage"], "success")
            self.assertEqual(durable["last_good"]["sha"], selected.sha)
            self.assertEqual(durable["new"]["generation_id"], generation)
            self.assertEqual(switch.read_selected(host, durable), "new")
            self.assertEqual(durable["boot"]["containers"], ids)
            self.assertEqual(host.timer, state["timer_before"])
            self.assertFalse((host.state_dir / "deploy-in-progress").exists())
            self.assertEqual([command for command in host.commands[before:] if command[:2] == ("docker", "start")],
                             [("docker", "start", *(ids[name] for name in prep.CHANNELS))])
            self.assertFalse(any(command[0] == "migrate" or command[:3] == ("docker", "compose", "create")
                                 for command in host.commands[before:]))
            backup = Path(durable["snapshot"]["path"])
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), durable["snapshot"]["sha256"])
            original = [("original",)]
            for path, expected in ((backup, original), (durable["old"]["host_path"], original),
                                   (durable["new"]["host_path"], original + [("accepted-after-admission",)])):
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.execute("SELECT text FROM messages ORDER BY id").fetchall(), expected)

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
                self.assertFalse(any(command[:2] == ("docker", "start")
                                     for command in host.commands[len(before):]))
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
                self.assertFalse(any(command[:2] == ("docker", "start")
                                     for command in host.commands[len(before):]))

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

    def test_partial_retry_evidence_failure_closes_running_writer(self) -> None:
        """API отказ до start переводит частично запущенную пару в degraded."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            state.update(stage="forward_recovery_intent", forward_recovery=state["boot"])
            prep.write_state(host.state_dir, host.checkout, state)
            host.main_failed = False
            host.found["tg_assistant"]["State"] = {"Running": True, "Status": "running"}
            host.require_recovery_evidence = lambda _state: (_ for _ in ()).throw(
                prod.DeployError("API unavailable"))  # type: ignore[method-assign]
            with self.assertRaises(prod.DeployError):
                self._recover(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "degraded")
            self.assertTrue(all(not item["State"]["Running"] for item in host.found.values()))
            self.assertEqual((host.state_dir / "deploy-in-progress").read_text(), "critical\n")
            self.assertEqual(host.timer, {"enabled": False, "active": False})

    def test_missing_marker_closes_partial_retry(self) -> None:
        """Пропавший marker восстанавливается и не оставляет polling writer."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            state.update(stage="forward_recovery_intent", forward_recovery=state["boot"])
            prep.write_state(host.state_dir, host.checkout, state)
            host.main_failed = False
            host.marker(False)
            host.found["tg_assistant"]["State"] = {"Running": True, "Status": "running"}
            with self.assertRaises(prod.DeployError):
                self._recover(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "degraded")
            self.assertTrue(all(not item["State"]["Running"] for item in host.found.values()))
            self.assertEqual((host.state_dir / "deploy-in-progress").read_text(), "critical\n")

    def test_label_inventory_stops_writer_if_compose_stop_fails(self) -> None:
        """Дополнительный stop по точным ID работает при сломанном Compose."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            state.update(stage="forward_recovery_intent", forward_recovery=state["boot"])
            prep.write_state(host.state_dir, host.checkout, state)
            host.main_failed = False
            host.found["tg_assistant"]["State"] = {"Running": True, "Status": "running"}
            host.require_recovery_evidence = lambda _state: (_ for _ in ()).throw(
                prod.DeployError("API unavailable"))  # type: ignore[method-assign]
            with patch.object(host, "stop_both", side_effect=prod.DeployError("Compose unavailable")):
                with self.assertRaises(prod.DeployError):
                    self._recover(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "degraded")
            self.assertTrue(all(not item["State"]["Running"] for item in host.found.values()))
            self.assertTrue(any(command[:2] == ("docker", "stop") for command in host.commands))

    def test_failed_docker_stop_does_not_claim_guards_confirmed(self) -> None:
        """Невозможность остановить writer получает отдельный безопасный отказ."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            state.update(stage="forward_recovery_intent", forward_recovery=state["boot"])
            prep.write_state(host.state_dir, host.checkout, state)
            host.main_failed = False
            host.found["tg_assistant"]["State"] = {"Running": True, "Status": "running"}
            host.require_recovery_evidence = lambda _state: (_ for _ in ()).throw(
                prod.DeployError("API unavailable"))  # type: ignore[method-assign]
            original = host.run

            def fail_stop(*args: str, env=None) -> str:
                """Отказывает именно в прямом Docker stop после Compose failure."""
                if args[:2] == ("docker", "stop"):
                    raise prod.DeployError("Docker stop unavailable")
                return original(*args, env=env)

            host.run = fail_stop  # type: ignore[method-assign]
            with patch.object(host, "stop_both", side_effect=prod.DeployError("Compose unavailable")):
                with self.assertRaisesRegex(prod.DeployError, "не доказаны"):
                    self._recover(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "degraded")
            self.assertEqual((host.state_dir / "deploy-in-progress").read_text(), "critical\n")
            self.assertTrue(host.found["tg_assistant"]["State"]["Running"])

    def test_adopted_retry_api_failure_closes_running_pair(self) -> None:
        """Даже после systemd adoption сетевой отказ закрывает оба канала."""
        with TemporaryDirectory() as temporary:
            host, state = self._fixture(Path(temporary).resolve())
            state.update(stage="runtime_adopted", forward_recovery=state["boot"])
            prep.write_state(host.state_dir, host.checkout, state)
            host.marker(False)
            host.main_failed = False
            host.main_active = True
            host.timer = state["timer_before"].copy()
            for item in host.found.values():
                item["State"] = {"Running": True, "Status": "running"}
            host.require_recovery_evidence = lambda _state: (_ for _ in ()).throw(
                prod.DeployError("API unavailable"))  # type: ignore[method-assign]
            with self.assertRaises(prod.DeployError):
                self._recover(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "degraded")
            self.assertTrue(all(not item["State"]["Running"] for item in host.found.values()))
            self.assertEqual((host.state_dir / "deploy-in-progress").read_text(), "critical\n")
            self.assertEqual(host.timer, {"enabled": False, "active": False})
