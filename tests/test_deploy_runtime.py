"""Проверяет boot/adoption и финализацию v2 с настоящими state и SQLite."""

from contextlib import closing, nullcontext
import copy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deploy_runtime as runtime
from scripts import deploy_schema_admission as schema
from compadre_assistant.core.data.sqlite import runtime_schema
from tests.test_deploy_database_switch import SwitchHost, SHA, IMAGE, MANIFEST


class RuntimeHost(SwitchHost):
    """Подменяет только Docker/systemd transport; boot/state выполняются реально."""

    def require_switch_rollout(self):
        """Разрешает только изолированный тест, production gate остаётся закрыт."""

    def image(self, reference):
        """Возвращает точную OCI revision синтетического candidate."""
        return {"Id": IMAGE, "Config": {"Labels": {prep.OCI_REVISION: SHA}}}

    def verify_both(self, image, sha):
        """Проверяет уже работающую пару fake transport."""
        if any(not item["State"]["Running"] for item in self.found.values()):
            raise prod.DeployError("not running")

    def activate_unit(self):
        """Запускает реальный adoption helper с занятым родительским lock."""
        with patch.object(prep, "deploy_lock", side_effect=prep.PreparationError("locked")):
            runtime.start(self)
        self.unit_adopted = True

    def timer_restore(self, snapshot):
        """Сохраняет исходные enabled/active в fake systemd transport."""
        self.restored_timer = snapshot

    def require_recovery_quiescence(self):
        """Изолирует подготовку schema файлов от Linux transport."""

    def require_recovery_evidence(self, state):
        """Изолирует сетевое evidence от контрактов boot helper."""


class DeployRuntimeTests(unittest.TestCase):
    """Проверяет новые контракты 6.5; Linux эксплуатационный стенд не подменяется."""

    def _fixture(self, root):
        host = RuntimeHost(root)
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST, "controller_sha": "c" * 40,
                 "old_sha": SHA, "old_image_id": IMAGE, "old_tag": prep.pinned_tag(SHA),
                 "candidate": {"sha": SHA, "image_id": IMAGE, "tag": prep.pinned_tag(SHA),
                               "version": "v1.2.3", "run_id": 4, "run_attempt": 1},
                 "timer_before": {"enabled": True, "active": False},
                 "last_good": {"version": "v1.2.2", "sha": SHA, "image_id": IMAGE, "tag": prep.pinned_tag(SHA)}}
        state["compatibility_evidence"] = {"schema_fingerprint": switch.identity(
            host.checkout / "data/app.db", "legacy", MANIFEST)["fingerprint"]}
        def record(stage, **changes):
            state.update(changes, stage=stage)
            prep.write_state(host.state_dir, host.checkout, state)
        switch.register_initial(host, state, record, prod._atomic_text)
        switch.transition(host, state, record, prod._atomic_text)
        return host, state, record

    def test_finalization_adopts_without_create_or_start_and_restores_exact_timer(self):
        """Oneshot adoption не берёт чужой lock и не создаёт второй polling."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._fixture(Path(temporary).resolve())
            before = list(host.commands)
            runtime.finalize(host, state, record, "success")
            self.assertEqual("success", state["stage"])
            self.assertEqual(SHA, state["last_good"]["sha"])
            self.assertEqual("c" * 40, state["controller_sha"])
            self.assertFalse((host.state_dir / "deploy-in-progress").exists())
            self.assertTrue(host.unit_adopted)
            self.assertEqual({"enabled": True, "active": False}, host.restored_timer)
            self.assertEqual(before, host.commands)
            self.assertTrue(all(item["HostConfig"]["RestartPolicy"]["Name"] == "no" for item in host.found.values()))

    def test_busy_lock_never_starts_stopped_container(self):
        """При занятом lock возможен только read-only adopt уже работающей пары."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._fixture(Path(temporary).resolve())
            record("finalization_intent", boot=runtime.boot_record(state, "success"))
            host.marker(False)
            host.found["tg_assistant"]["State"] = {"Running": False, "Status": "exited"}
            before = list(host.commands)
            with patch.object(prep, "deploy_lock", side_effect=prep.PreparationError("locked")):
                with self.assertRaises(prod.DeployError):
                    runtime.start(host)
            self.assertEqual(before, host.commands)

    def test_boot_restarts_only_durable_ids_and_repeat_is_read_only(self):
        """Штатный boot остаётся доступен после ротации CI pin и сохраняет ID."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._fixture(Path(temporary).resolve())
            runtime.finalize(host, state, record, "success")
            prod._atomic_text(host.state_dir / "compatibility-policy.sha", "d" * 40 + "\n")
            for item in host.found.values():
                item["State"] = {"Running": False, "Status": "exited"}
            with (patch.object(prep, "deploy_lock", return_value=nullcontext()),
                  patch.object(host, "require_recovery_evidence", side_effect=AssertionError("завершённый boot"))):
                runtime.start(host)
                before = list(host.commands)
                runtime.start(host)
            self.assertEqual(before, host.commands)
            self.assertEqual(("docker", "start", *[state["boot"]["containers"][name] for name in prep.CHANNELS]), host.commands[-1])

    def test_interrupted_finalization_resumes_same_boot_record(self):
        """Повтор после удаления marker завершает прежний intent без новых ID."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._fixture(Path(temporary).resolve())
            with patch.object(host, "activate_unit", side_effect=RuntimeError("interrupted")):
                with self.assertRaises(RuntimeError):
                    runtime.finalize(host, state, record, "success")
            boot = copy.deepcopy(state["boot"])
            self.assertEqual("finalization_intent", state["stage"])
            runtime.finalize(host, state, record, "success")
            self.assertEqual(boot, state["boot"])
            self.assertEqual("success", state["stage"])

    def test_rollback_finalization_preserves_selected_new_database(self):
        """После admission rolled_back сохраняет новое поколение и last_good."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._fixture(Path(temporary).resolve())
            selected = Path(state["new"]["host_path"])
            with closing(sqlite3.connect(selected)) as connection:
                connection.execute("INSERT INTO messages VALUES(2, 'after-admission')")
                connection.commit()
            good = copy.deepcopy(state["last_good"])
            record("recovery_verified", recovery_containers=state["prepared_containers"],
                   recovery_intent={"to": state["new"]["generation_id"], "image_id": IMAGE, "sha": SHA})
            runtime.finalize(host, state, record, "rolled_back")
            self.assertEqual("rolled_back", state["stage"])
            self.assertEqual(good, state["last_good"])
            self.assertEqual("new", switch.read_selected(host, state))
            with closing(sqlite3.connect(selected)) as connection:
                self.assertEqual(2, connection.execute("SELECT count(*) FROM messages").fetchone()[0])

    def test_unknown_stage_marker_mutated_id_and_admission_fail_closed(self):
        """Boot не исправляет marker, неизвестную историю или подменённый runtime."""
        for fault in ("marker", "stage", "id", "admission", "selector"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                host, state, record = self._fixture(Path(temporary).resolve())
                record("finalization_intent", boot=runtime.boot_record(state, "success"))
                host.marker(False)
                if fault == "marker":
                    host.marker(True)
                elif fault == "stage":
                    record("unknown")
                elif fault == "id":
                    host.found["tg_assistant"]["Id"] = "foreign-id"
                elif fault == "admission":
                    state["admission"]["sha"] = "f" * 40
                    record(state["stage"])
                else:
                    (host.state_dir / "active-db.json").write_text("{}", encoding="utf-8")
                before = list(host.commands)
                with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                    with self.assertRaises((prod.DeployError, ValueError)):
                        runtime.start(host)
                self.assertEqual(before, host.commands)

    def test_readonly_schema_delivery_is_accepted_by_actual_old_runtime_validator(self):
        """Контракт exact old/new проверяется на настоящей мигрированной SQLite."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._fixture(Path(temporary).resolve())
            migrations = host.checkout / "compadre_assistant/core/data/sqlite/migrations"
            migrations.mkdir(parents=True)
            (migrations / MANIFEST[0]).write_bytes(b"-- exact old SQL\n")
            target = Path(state["new"]["host_path"])
            name = "0002_extra.sql"
            with closing(sqlite3.connect(target)) as connection:
                connection.execute("CREATE TABLE extra(id INTEGER)")
                connection.execute("INSERT INTO schema_migrations VALUES(2, ?)", (name,))
                connection.commit()
            state["new"] = switch.identity(target, state["new"]["generation_id"], MANIFEST + [name])
            state["compatibility_evidence"].update(schema_fingerprint=state["new"]["fingerprint"], artifact_digest="sha256:" + "7" * 64)
            with patch.object(prep, "require_clean_checkout"):
                schema.prepare(host, state, record, prod._atomic_text)
            schema.require_files(host, state)
            with closing(sqlite3.connect(target)) as connection:
                self.assertEqual([], runtime_schema.validate_compatible_schema(connection,
                    state["runtime_schema"]["admission"], state["runtime_schema"]["launch"], migrations))
                self.assertEqual(2, connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
            item = {"Config": {"Env": [runtime_schema.ADMISSION_ENV + "=" + schema.CONTAINER_ADMISSION,
                                        runtime_schema.LAUNCH_ENV + "=" + schema.CONTAINER_LAUNCH]},
                    "Mounts": [{"Type": "bind", "Source": str(host.state_dir / filename),
                                "Destination": destination, "RW": False}
                               for filename, destination in ((schema.ADMISSION, schema.CONTAINER_ADMISSION),
                                                             (schema.LAUNCH, schema.CONTAINER_LAUNCH))]}
            schema.require_container(item, state, host.state_dir)
            item["Mounts"][0]["RW"] = True
            with self.assertRaises(ValueError):
                schema.require_container(item, state, host.state_dir)
            (host.state_dir / schema.ADMISSION).write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                schema.require_files(host, state)


if __name__ == "__main__":
    unittest.main()
