"""Проверяет v2 recovery на настоящих SQLite/WAL с fake Docker/systemd."""

from contextlib import closing, nullcontext
from dataclasses import asdict
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
from scripts.select_deploy_release import SelectedRelease
from tests.test_deploy_database_switch import SwitchHost, MANIFEST, IMAGE, SHA


OLD_SHA = "a" * 40
OLD_IMAGE = "sha256:" + "1" * 64
ZERO_TIME = "0001-01-01T00:00:00Z"


class Crash(BaseException):
    """Имитирует прекращение controller без обработки обычного исключения."""


class RecoveryHost(SwitchHost):
    """Подменяет системный транспорт, сохраняя реальные writers guards и pin."""

    def __init__(self, root):
        """Создаёт исходную БД, fake pin, Release и приватные policy файлы."""
        super().__init__(root)
        self.selected = SelectedRelease(prod.EXPECTED_REPOSITORY, "v1.2.3", "release/1.2", SHA,
                                        5, "main-full.yml", 8, 1)
        self.serial = 0
        self.starts = 0
        self.bad_image = False
        for item in self.found.values():
            item["Image"] = OLD_IMAGE
            item["State"]["StartedAt"] = "2026-11-04T00:00:00Z"
        prod._atomic_text(self.state_dir / "compatibility-policy.sha", "c" * 40)
        prod._atomic_text(self.state_dir / "github-read-token", "synthetic-test-token")

    def image(self, reference):
        """Возвращает проверяемые OCI revision обоих локальных образов."""
        old = reference in (OLD_IMAGE, prep.pinned_tag(OLD_SHA))
        return {"Id": OLD_IMAGE if old else IMAGE, "Config": {"Labels": {
            prep.OCI_REVISION: "f" * 40 if self.bad_image else OLD_SHA if old else SHA}}}

    def select(self, version):
        """Возвращает synthetic immutable Release для проверки точной записи."""
        return self.selected

    def restore_checkout(self, sha):
        """Фиксирует возврат checkout без действий над репозиторием тестов."""
        self.commands.append(("restore", sha))

    def timer_suspend(self):
        """Фиксирует запрет synthetic backup timer."""
        self.commands.append(("timer_suspend",))

    def verify_both(self, expected_id, expected_sha, **kwargs):
        """Проверяет оба fake runtime после действительного вызова start."""
        if any(item["Image"] != expected_id or not item["State"]["Running"] for item in self.found.values()):
            raise AssertionError("wrong runtime")
        if self.image(expected_id)["Config"]["Labels"][prep.OCI_REVISION] != expected_sha:
            raise AssertionError("wrong revision")

    def run(self, *args, env=None):
        """Эмулирует транспорт guard/create/start с уникальными ID каждого create."""
        self.commands.append(args)
        if args[:2] == ("systemctl", "stop"):
            return ""
        if args[:3] == ("docker", "ps", "--all"):
            channel = args[-1].split("=", 2)[-1]
            return self.found.get(channel, {}).get("Id", "")
        if args[:2] == ("docker", "inspect"):
            return json.dumps(next(item for item in self.found.values() if item["Id"] == args[2]))
        if args[:2] in (("docker", "stop"), ("docker", "update")):
            for item in self.found.values():
                if item["Id"] == args[-1]:
                    if args[1] == "update":
                        item["HostConfig"]["RestartPolicy"]["Name"] = "no"
                    else:
                        item["State"]["Running"] = False
                        if item["State"]["Status"] != "created":
                            item["State"]["Status"] = "exited"
            return ""
        if args[:3] == ("docker", "compose", "create"):
            if any(item["State"]["Running"] for item in self.found.values()):
                raise AssertionError("create with live writer")
            self.serial += 1
            self.found = self._containers(env["ASSISTANT_DATABASE_PATH"], "created")
            for name, item in self.found.items():
                item["Id"] = f"{name}-{self.serial}"
                item["Image"] = self.image(env["ASSISTANT_IMAGE"])["Id"]
                item["State"]["StartedAt"] = ZERO_TIME
            return ""
        if args[:2] == ("docker", "start"):
            state = prep.read_state(self.state_dir, self.checkout)
            if state.get("stage") != "recovery_prepared" and state.get("admission_may_have_started") is not True:
                raise AssertionError("start without durable permission")
            if any(item["State"]["Running"] for item in self.found.values()):
                raise AssertionError("double polling")
            if set(args[2:]) != {item["Id"] for item in self.found.values()}:
                raise AssertionError("wrong IDs")
            self.starts += 1
            for item in self.found.values():
                item["State"].update(Running=True, Status="running", StartedAt="2026-11-04T01:00:00Z")
            return ""
        return super().run(*args, env=env)


class DatabaseRecoveryTests(unittest.TestCase):
    """Проверяет границу, повтор, corruption и независимую проверку допуска."""

    def setUp(self):
        """Подменяет POSIX owner/mode check, сохраняя чтение настоящих файлов."""
        mocked = patch.object(prod.compatibility, "private_text", side_effect=lambda path: path.read_text().strip())
        mocked.start()
        self.addCleanup(mocked.stop)

    def _setup(self, root, stage="database_selected"):
        host = RecoveryHost(root)
        old = switch.identity(host.checkout / "data/app.db", "legacy", MANIFEST)
        evidence = {"artifact_id": 10, "artifact_digest": "sha256:" + "d" * 64,
                    "policy_sha": "c" * 40, "schema_fingerprint": old["fingerprint"]}
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST,
                 "controller_sha": "c" * 40, "old_sha": OLD_SHA,
                 "old_image_id": OLD_IMAGE, "old_tag": prep.pinned_tag(OLD_SHA),
                 "candidate": asdict(host.selected) | {"image_id": IMAGE, "tag": prep.pinned_tag(SHA)},
                 "compatibility_evidence": evidence}

        def record(name, **changes):
            state.update(changes, stage=name)
            prep.write_state(host.state_dir, host.checkout, state)

        switch.register_initial(host, state, record, prod._atomic_text)
        if stage != "writers_stopped":
            def stop(name, **changes):
                record(name, **changes)
                if name == stage:
                    raise Crash()
            with self.assertRaises(Crash):
                switch.transition(host, state, stop, prod._atomic_text)
        return host, state, record

    def _recover(self, host, state):
        with patch.object(prod.compatibility, "verify_evidence", return_value=state["compatibility_evidence"]) as verify:
            prod._recover(host, state)
        return verify

    def test_before_admission_recovers_old_at_every_prestart_stage(self):
        """Все pre-admission стадии возвращают old, сохраняя файлы и false."""
        for stage in ("writers_stopped", "snapshot_verified", "candidate_copy_verified", "switch_intent",
                      "database_selected", "runtime_prepared"):
            with self.subTest(stage=stage), TemporaryDirectory() as temporary:
                host, state, _ = self._setup(Path(temporary), stage)
                original = Path(state["old"]["host_path"]).read_bytes()
                verify = self._recover(host, state)
                self.assertEqual(switch.read_selected(host, state), "old")
                self.assertIs(state["admission_may_have_started"], False)
                self.assertEqual(Path(state["old"]["host_path"]).read_bytes(), original)
                self.assertEqual(state["stage"], "recovery_verified")
                self.assertTrue((host.state_dir / "deploy-in-progress").exists())
                verify.assert_not_called()
                if "new" in state:
                    self.assertTrue(Path(state["new"]["host_path"]).exists())
                self._recover(host, state)
                self.assertEqual(host.starts, 2)
                self.assertEqual(len(host.found), 2)

    def test_after_admission_preserves_wal_rows_and_rechecks_exact_evidence(self):
        """Записи нового WAL переживают recovery/repeat; artifact проверяется дважды."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary), "candidate_started")
            path = Path(state["new"]["host_path"])
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("INSERT INTO messages VALUES(2, 'accepted-after-admission')")
                db.commit()
                self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
                wal = Path(str(path) + "-wal").read_bytes()
                for _ in range(2):
                    verify = self._recover(host, state)
                    self.assertEqual(verify.call_count, 2)
                    for call in verify.call_args_list:
                        self.assertEqual(call.args[1:], (host.selected, "c" * 40, OLD_SHA))
                    self.assertEqual(switch.read_selected(host, state), "new")
                    self.assertTrue(state["admission_may_have_started"])
                    self.assertEqual(Path(str(path) + "-wal").read_bytes(), wal)
                    self.assertEqual(db.execute("SELECT text FROM messages ORDER BY id").fetchall(),
                                     [("original",), ("accepted-after-admission",)])
                self.assertEqual(host.starts, 3)

    def test_recovery_crash_boundaries_converge(self):
        """Повтор после durable recovery stage и start не создаёт второй polling."""
        for admitted in (False, True):
            for boundary in ("recovery_intent", "selector_before", "selector_after", "recovery_selected",
                             "create_after", "recovery_prepared", "start_after", "recovery_verified"):
                if admitted and boundary.startswith("selector"):
                    continue
                with self.subTest(admitted=admitted, boundary=boundary), TemporaryDirectory() as temporary:
                    host, state, record = self._setup(Path(temporary),
                        "admission_recorded" if admitted else "database_selected")
                    host.guard_known_writers()

                    def interrupted(name, **changes):
                        record(name, **changes)
                        if name == boundary:
                            raise Crash()

                    def publish(path, value):
                        if boundary == "selector_before":
                            raise Crash()
                        prod._atomic_text(path, value)
                        if boundary == "selector_after":
                            raise Crash()

                    original = host.run

                    def run(*args, env=None):
                        value = original(*args, env=env)
                        if ((boundary == "create_after" and args[:3] == ("docker", "compose", "create"))
                                or (boundary == "start_after" and args[:2] == ("docker", "start"))):
                            raise Crash()
                        return value

                    with patch.object(prod.compatibility, "verify_evidence", return_value=state["compatibility_evidence"]), \
                            patch.object(host, "run", side_effect=run), self.assertRaises(Crash):
                        switch.recover(host, state, interrupted, publish)
                    durable = prep.read_state(host.state_dir, host.checkout)
                    self._recover(host, durable)
                    self.assertEqual(switch.read_selected(host, durable), "new" if admitted else "old")
                    self.assertEqual(durable["stage"], "recovery_verified")

    def test_bad_evidence_pin_selector_database_or_boundary_stop_every_writer(self):
        """Неизвестный state и API outage оставляют обе БД и закрытый marker."""
        for damage in ("api", "artifact", "sha", "release", "oci", "selector", "missing_selector",
                       "missing_db", "inode", "false_started", "intent", "candidate_id", "snapshot"):
            with self.subTest(damage=damage), TemporaryDirectory() as temporary:
                host, state, record = self._setup(Path(temporary), "candidate_started")
                evidence = copy.deepcopy(state["compatibility_evidence"])
                if damage == "artifact":
                    evidence["artifact_id"] += 1
                elif damage == "sha":
                    state["old_sha"] = "d" * 40
                elif damage == "release":
                    state["candidate"]["run_attempt"] += 1
                elif damage == "oci":
                    host.bad_image = True
                elif damage == "selector":
                    prod._atomic_text(host.state_dir / "active-db.json", json.dumps(switch.selector(state, "old")))
                elif damage == "missing_selector":
                    (host.state_dir / "active-db.json").unlink()
                elif damage == "missing_db":
                    Path(state["new"]["host_path"]).unlink()
                elif damage == "inode":
                    state["new"]["inode"] += 1
                elif damage == "false_started":
                    state["admission_may_have_started"] = False
                elif damage == "intent":
                    state["recovery_intent"] = {"to": "legacy"}
                elif damage == "candidate_id":
                    state["prepared_containers"][prep.CHANNELS[0]] = "unknown-id"
                elif damage == "snapshot":
                    state["snapshot"]["sha256"] = "0" * 64
                record(state["stage"])
                with patch.object(prod.compatibility, "verify_evidence", return_value=evidence,
                                  side_effect=OSError("API unavailable") if damage == "api" else None):
                    with self.assertRaises(prod.DeployError):
                        prod._recover(host, state)
                self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                self.assertEqual(host.starts, 1)
                self.assertEqual(state["stage"], "degraded")
                self.assertTrue(Path(state["old"]["host_path"]).exists())
                self.assertTrue((host.state_dir / "deploy-in-progress").exists())

    def test_old_runtime_on_old_database_after_admission_fails_closed(self):
        """Даже один old/old writer после true запрещает start и повтор recovery."""
        for channel in prep.CHANNELS:
            with self.subTest(channel=channel), TemporaryDirectory() as temporary:
                host, state, _ = self._setup(Path(temporary), "candidate_started")
                item = host.found[channel]
                item["Image"] = OLD_IMAGE
                item["Config"]["Env"] = ["DATABASE_PATH=" + state["old"]["container_path"]]
                selector = (host.state_dir / "active-db.json").read_bytes()
                databases = {side: Path(state[side]["host_path"]).read_bytes() for side in ("old", "new")}
                for _ in range(2):
                    with self.assertRaises(prod.DeployError):
                        self._recover(host, state)
                    self.assertEqual(state["stage"], "degraded")
                    self.assertIs(state["admission_may_have_started"], True)
                    self.assertEqual(host.starts, 1)
                    self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                    self.assertEqual((host.state_dir / "active-db.json").read_bytes(), selector)
                    self.assertTrue((host.state_dir / "deploy-in-progress").exists())
                    for side, content in databases.items():
                        self.assertEqual(Path(state[side]["host_path"]).read_bytes(), content)

    def test_unknown_stage_does_not_authorize_recovery(self):
        """Неизвестная стадия не получает права запуска даже при целых файлах."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._setup(Path(temporary))
            record("unknown_stage")
            with self.assertRaises(prod.DeployError):
                self._recover(host, state)
            self.assertEqual(state["stage"], "degraded")
            self.assertEqual(host.starts, 0)
            self.assertEqual(switch.read_selected(host, state), "new")

    def test_partial_registration_and_transition_rebind(self):
        """Только durable registration/rebind intent позволяет завершить публикацию."""
        for rebind in (False, True):
            with self.subTest(rebind=rebind), TemporaryDirectory() as temporary:
                host, state, record = self._setup(Path(temporary), "writers_stopped")
                if rebind:
                    before = switch.selector(state, "old")
                    state["transition_id"] = "e" * 32
                    state["selector_rebind"] = {"from": before, "to": switch.selector(state, "old")}
                else:
                    (host.state_dir / "active-db.json").unlink()
                record("writers_stopped")
                self._recover(host, state)
                self.assertEqual(switch.read_selected(host, state), "old")

    def test_malformed_or_missing_state_preserved_and_both_entry_points_guard(self):
        """Ни JSON, ни его секреты не публикуются; повреждённый файл сохраняется."""
        for payload in (b'{"secret":"do-not-print",', b'[]', b'{"stage":"success","stage":"ready"}', None):
            for command in (prod.verify_pinned, lambda host: prod.deploy(host, "v1.2.3")):
                with self.subTest(payload=payload, command=command), TemporaryDirectory() as temporary:
                    host, _, _ = self._setup(Path(temporary), "candidate_started")
                    path = host.state_dir / "state.json"
                    if payload is None:
                        path.unlink()
                    else:
                        path.write_bytes(payload)
                    with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                        for _ in range(2):
                            with self.assertRaises(prod.DeployError) as error:
                                command(host)
                            self.assertNotIn("do-not-print", str(error.exception))
                    self.assertEqual(path.read_bytes() if path.exists() else None, payload)
                    self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                    self.assertTrue((host.state_dir / "recovery-blocked.json").exists())
                    self.assertEqual(host.starts, 1)

    def test_guard_attempts_other_writer_after_first_operation_fails(self):
        """Ошибка update первого канала не пропускает stop и защиту второго."""
        with TemporaryDirectory() as temporary:
            host, _, _ = self._setup(Path(temporary), "candidate_started")
            original = host.run

            def fail(*args, env=None):
                if args[:2] == ("docker", "update") and args[-1] == host.found[prep.CHANNELS[0]]["Id"]:
                    raise OSError("Docker failure")
                return original(*args, env=env)

            with patch.object(host, "run", side_effect=fail), self.assertRaises(prod.DeployError):
                host.guard_known_writers()
            self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))

    def test_evidence_recheck_before_start_can_revoke_recovery(self):
        """Отзыв artifact между подготовкой и start закрывает оба канала."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary), "candidate_started")
            with patch.object(prod.compatibility, "verify_evidence", side_effect=[
                    state["compatibility_evidence"], OSError("revoked")]), self.assertRaises(prod.DeployError):
                prod._recover(host, state)
            self.assertEqual(host.starts, 1)
            self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
            self.assertEqual(switch.read_selected(host, state), "new")

    def test_repeated_command_recovery_retains_gate_and_single_pair(self):
        """Обе команды выполняют recovery под lock, но не открывают следующий deploy."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary))
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                for command in (lambda: prod.deploy(host, "v1.2.3"), lambda: prod.verify_pinned(host)):
                    with self.assertRaisesRegex(prod.DeployError, "rollout закрыт"):
                        command()
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(durable["stage"], "recovery_verified")
            self.assertEqual(switch.read_selected(host, durable), "old")
            self.assertEqual(len(host.found), 2)
            self.assertEqual(host.starts, 2)

    def test_partial_create_missing_channel_recovers_only_before_possible_start(self):
        """Partial create восстанавливается при false; после start отсутствие неоднозначно."""
        for stage in ("runtime_prepared", "admission_recorded", "candidate_started"):
            with self.subTest(stage=stage), TemporaryDirectory() as temporary:
                host, state, _ = self._setup(Path(temporary), stage)
                host.found.pop(prep.CHANNELS[0])
                if stage == "runtime_prepared":
                    self._recover(host, state)
                    self.assertEqual(state["stage"], "recovery_verified")
                    self.assertEqual(host.starts, 1)
                    self.assertEqual(switch.read_selected(host, state), "old")
                else:
                    starts = host.starts
                    with self.assertRaises(prod.DeployError):
                        self._recover(host, state)
                    self.assertEqual(state["stage"], "degraded")
                    self.assertEqual(host.starts, starts)
                    self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
                self.assertTrue(Path(state["old"]["host_path"]).exists())
                self.assertTrue(Path(state["new"]["host_path"]).exists())

    def test_partial_create_failure_after_rollback_record_uses_saved_origin(self):
        """Штатный отказ create сохраняет pre-start origin перед rollback_started."""
        with TemporaryDirectory() as temporary:
            host, state, record = self._setup(Path(temporary), "runtime_prepared")
            host.found.pop(prep.CHANNELS[0])
            record("rollback_started", rollback_origin_stage="database_selected")
            self._recover(host, state)
            self.assertEqual(switch.read_selected(host, state), "old")

    def test_partial_create_with_started_survivor_is_not_recovered(self):
        """False не разрешает возврат, если оставшийся candidate уже запускался."""
        with TemporaryDirectory() as temporary:
            host, state, _ = self._setup(Path(temporary), "runtime_prepared")
            host.found.pop(prep.CHANNELS[0])
            host.found[prep.CHANNELS[1]]["State"]["StartedAt"] = "2026-11-04T01:00:00Z"
            with self.assertRaises(prod.DeployError):
                self._recover(host, state)
            self.assertEqual(host.starts, 0)
            self.assertEqual(state["stage"], "degraded")


if __name__ == "__main__":
    unittest.main()
