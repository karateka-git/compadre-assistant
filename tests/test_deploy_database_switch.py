"""Проверяет реальную SQLite и durable порядок переключения без Docker daemon."""

from contextlib import closing, ExitStack, nullcontext
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts.select_deploy_release import SelectedRelease


IMAGE = "sha256:" + "2" * 64
SHA = "b" * 40
MANIFEST = ["0001_initial_schema.sql"]


class SwitchHost(prod.Host):
    """Подменяет только Docker/systemd транспорт и сохраняет реальные файлы."""

    def __init__(self, root: Path):
        """Создаёт отдельную SQLite и остановленные каналы для каждого теста."""
        super().__init__(root / "checkout", root / "state")
        (self.checkout / "data").mkdir(parents=True)
        self.state_dir.mkdir()
        self.marker(True)
        with closing(sqlite3.connect(self.checkout / "data/app.db")) as db:
            db.executescript("CREATE TABLE schema_migrations(version INTEGER, name TEXT);"
                             "INSERT INTO schema_migrations VALUES(1, '0001_initial_schema.sql');"
                             "CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT);"
                             "INSERT INTO messages VALUES(1, 'original');")
        self.commands = []
        self.found = self._containers("/app/data/app.db", "exited")

    def _containers(self, path, status):
        return {name: {"Id": name + "-id", "Image": IMAGE,
                       "State": {"Running": False, "Status": status}, "RestartCount": 0,
                       "Config": {"Env": ["DATABASE_PATH=" + path]},
                       "HostConfig": {"RestartPolicy": {"Name": "no"}},
                       "Mounts": [{"Type": "bind", "Source": str(self.checkout / "data"),
                                   "Destination": "/app/data", "RW": True}]}
                for name in prep.CHANNELS}

    def image(self, reference):
        """Возвращает неизменяемый image ID синтетического runtime."""
        return {"Id": IMAGE}

    def containers(self):
        """Возвращает inspect двух каналов тестового Docker транспорта."""
        return self.found

    def unit_state(self, unit):
        """Подтверждает остановленные systemd unit и backup."""
        return "inactive"

    def unit_enabled(self, unit):
        """Подтверждает выключенный backup timer."""
        return False

    def require_no_duplicate_polling(self):
        """Синтетический транспорт не содержит сторонних контейнеров."""

    def require_no_foreign_database_mounts(self, own_ids):
        """Синтетический транспорт не содержит сторонних Docker writers."""

    def controller_sha(self):
        """Возвращает SHA синтетического snapshot независимо от candidate."""
        return "c" * 40

    def migrate_copy(self, image_id, directory, *, basename="app.db"):
        """Открывает только отдельную копию; схема этого кандидата неизменна."""
        self.commands.append(("migrate", str(directory / basename)))
        with closing(sqlite3.connect(directory / basename)) as db:
            self.asserted_copy_rows = db.execute("SELECT text FROM messages").fetchall()

    def run(self, *args, env=None):
        """Эмулирует config/create/start, проверяя durable файл при первом start."""
        self.commands.append(args)
        if args[:3] == ("docker", "compose", "config"):
            return json.dumps({"services": {name: {"image": env["ASSISTANT_IMAGE"],
                "pull_policy": "never", "restart": "no",
                "environment": {"DATABASE_PATH": env["ASSISTANT_DATABASE_PATH"]}}
                for name in prep.CHANNELS}})
        if args[:3] == ("docker", "compose", "create"):
            self.found = self._containers(env["ASSISTANT_DATABASE_PATH"], "created")
            return ""
        if args[:2] == ("docker", "start"):
            state = prep.read_state(self.state_dir, self.checkout)
            if state["admission_may_have_started"] is not True or switch.read_selected(self, state) != "new":
                raise AssertionError("start before durable admission")
            for item in self.found.values():
                item["State"] = {"Running": True, "Status": "running"}
            return ""
        raise AssertionError(args)


class DatabaseSwitchTests(unittest.TestCase):
    """Проверяет selector, отказные окна и фактические параметры обоих каналов."""

    def _setup(self, root):
        host = SwitchHost(root)
        old = switch.identity(host.checkout / "data/app.db", "legacy", MANIFEST)
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST,
                 "controller_sha": "c" * 40, "old_sha": "a" * 40,
                 "old_image_id": "sha256:" + "1" * 64, "old_tag": "old-pin",
                 "candidate": {"image_id": IMAGE, "sha": SHA, "tag": prep.pinned_tag(SHA)},
                 "compatibility_evidence": {"schema_fingerprint": old["fingerprint"]}}
        stages = []

        def record(stage, **changes):
            state.update(changes, stage=stage)
            prep.write_state(host.state_dir, host.checkout, state)
            stages.append(stage)

        switch.register_initial(host, state, record, prod._atomic_text)
        return host, state, record, stages

    def test_snapshot_selector_runtime_and_admission_order(self):
        """Мигратор получает копию; start видит durable true и выбранное поколение."""
        with TemporaryDirectory() as temporary:
            host, state, record, stages = self._setup(Path(temporary))
            original = (host.checkout / "data/app.db").read_bytes()
            switch.transition(host, state, record, prod._atomic_text)
            self.assertEqual(stages, ["writers_stopped", "snapshot_verified", "candidate_copy_verified",
                "switch_intent", "database_selected", "runtime_prepared", "admission_recorded", "candidate_started"])
            self.assertEqual(switch.read_selected(host, state), "new")
            self.assertEqual((host.checkout / "data/app.db").read_bytes(), original)
            self.assertNotEqual(state["old"]["host_path"], state["new"]["host_path"])
            self.assertNotEqual(state["snapshot"]["path"], state["new"]["host_path"])
            self.assertEqual(host.runtime_tag(), state["candidate"]["tag"])
            self.assertEqual(host.asserted_copy_rows, [("original",)])
            starts = [command for command in host.commands if command[:2] == ("docker", "start")]
            self.assertEqual(len(starts), 1)
            self.assertEqual(set(starts[0][2:]), set(state["prepared_containers"].values()))

    def test_every_record_failure_prevents_polling(self):
        """Ошибка каждой записи до допуска не вызывает ни одного start."""
        for failure in ("snapshot_verified", "candidate_copy_verified", "switch_intent",
                        "database_selected", "runtime_prepared", "admission_recorded"):
            with self.subTest(stage=failure), TemporaryDirectory() as temporary:
                host, state, record, _ = self._setup(Path(temporary))

                def failing(stage, **changes):
                    if stage == failure:
                        raise OSError("state storage failure")
                    record(stage, **changes)

                with self.assertRaises((OSError, ValueError)):
                    switch.transition(host, state, failing, prod._atomic_text)
                self.assertFalse(any(command[:2] == ("docker", "start") for command in host.commands))

    def test_fsync_failure_after_admission_replace_never_starts(self):
        """Даже опубликованный true не разрешает start при ошибке durable writer."""
        with TemporaryDirectory() as temporary:
            host, state, record, _ = self._setup(Path(temporary))

            def failing(stage, **changes):
                record(stage, **changes)
                if stage == "admission_recorded":
                    raise OSError("directory fsync failure")

            with self.assertRaises(OSError):
                switch.transition(host, state, failing, prod._atomic_text)
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertTrue(durable["admission_may_have_started"])
            self.assertEqual(switch.read_selected(host, durable), "new")
            self.assertFalse(any(command[:2] == ("docker", "start") for command in host.commands))

    def test_actual_admission_file_sync_error_never_starts(self):
        """Ошибка fsync настоящего state writer блокирует start до replace."""
        with TemporaryDirectory() as temporary:
            host, state, record, _ = self._setup(Path(temporary))

            def failing(stage, **changes):
                if stage == "admission_recorded":
                    with patch.object(prep.os, "fsync", side_effect=OSError("disk failure")):
                        record(stage, **changes)
                else:
                    record(stage, **changes)

            with self.assertRaises(OSError):
                switch.transition(host, state, failing, prod._atomic_text)
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertIs(durable["admission_may_have_started"], False)
            self.assertEqual(switch.read_selected(host, durable), "new")
            self.assertFalse(any(command[:2] == ("docker", "start") for command in host.commands))

    def test_selector_publication_failure_never_starts(self):
        """Отказ до и после atomic replace selector закрывает polling."""
        for after in (False, True):
            with self.subTest(after=after), TemporaryDirectory() as temporary:
                host, state, record, _ = self._setup(Path(temporary))

                def failing(path, value):
                    if after:
                        prod._atomic_text(path, value)
                    raise OSError("selector sync failure")

                with self.assertRaises(OSError):
                    switch.transition(host, state, record, failing)
                self.assertEqual(switch.read_selected(host, state), "new" if after else "old")
                self.assertFalse(any(command[:2] == ("docker", "start") for command in host.commands))

    def test_tampered_boundary_intent_identity_or_selector_is_rejected(self):
        """Чужой selector, ложная граница и подмена inode никогда не угадываются."""
        with TemporaryDirectory() as temporary:
            host, state, record, _ = self._setup(Path(temporary))
            switch.transition(host, state, record, prod._atomic_text)
            for key, value in (("switch_intent", None), ("protocol_version", None),
                               ("transition_id", "f" * 32), ("admission_may_have_started", None)):
                changed = copy.deepcopy(state)
                changed[key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    switch.read_selected(host, changed)
            changed = copy.deepcopy(state)
            changed["new"]["inode"] += 1
            with self.assertRaises(ValueError):
                switch.read_selected(host, changed)
            prod._atomic_text(host.state_dir / "active-db.json", json.dumps(switch.selector(state, "old")))
            with self.assertRaises(ValueError):
                switch.read_selected(host, state)

    def test_inspect_rejects_each_channel_before_start(self):
        """Неверные path/mount/image/restart/status любого канала закрывают start."""
        for channel in prep.CHANNELS:
            for field in ("path", "mount", "image", "restart", "status", "id"):
                with self.subTest(channel=channel, field=field), TemporaryDirectory() as temporary:
                    host, state, record, _ = self._setup(Path(temporary))
                    original = host.require_database_containers

                    def tampered(ids, image_id, path):
                        item = host.found[channel]
                        if field == "path":
                            item["Config"]["Env"] = ["DATABASE_PATH=/app/data/app.db"]
                        elif field == "mount":
                            item["Mounts"][0]["Source"] += "-other"
                        elif field == "image":
                            item["Image"] = "wrong"
                        elif field == "restart":
                            item["HostConfig"]["RestartPolicy"]["Name"] = "always"
                        elif field == "status":
                            item["State"]["Status"] = "exited"
                        else:
                            item["Id"] += "-replaced"
                        original(ids, image_id, path)

                    with patch.object(host, "require_database_containers", side_effect=tampered):
                        with self.assertRaises(prod.DeployError):
                            switch.transition(host, state, record, prod._atomic_text)
                    self.assertFalse(any(command[:2] == ("docker", "start") for command in host.commands))

    def test_start_requires_durable_boundary_even_if_called_directly(self):
        """Host отвергает прямой start без сохранённого допуска."""
        with TemporaryDirectory() as temporary:
            host, state, _, _ = self._setup(Path(temporary))
            with self.assertRaises(prod.DeployError):
                host.start_database_containers({}, IMAGE, "/app/data/app.db")

    def test_missing_or_extra_inspected_channel_is_rejected(self):
        """Неполный inspect не разрешает запуск второго непроверенного ID."""
        for extra in (False, True):
            with self.subTest(extra=extra), TemporaryDirectory() as temporary:
                host, _, _, _ = self._setup(Path(temporary))
                host.found = host._containers("/app/data/app.db", "created")
                ids = {name: item["Id"] for name, item in host.found.items()}
                if extra:
                    host.found["unknown"] = copy.deepcopy(next(iter(host.found.values())))
                else:
                    host.found.pop(prep.CHANNELS[0])
                with self.assertRaises(prod.DeployError):
                    host.require_database_containers(ids, IMAGE, "/app/data/app.db")

    def test_interrupted_protocol_never_enters_legacy_recovery(self):
        """Повтор оставляет оба writers остановленными и сохраняет true/new."""
        with TemporaryDirectory() as temporary:
            host, state, record, _ = self._setup(Path(temporary))
            switch.transition(host, state, record, prod._atomic_text)
            with patch.object(prod, "_degrade") as degrade:
                with self.assertRaises(prod.DeployError):
                    prod._recover(host, state)
            degrade.assert_called_once()
            self.assertTrue(state["admission_may_have_started"])
            self.assertEqual(switch.read_selected(host, state), "new")

    def test_production_entry_points_reject_v2_before_mutation(self):
        """Обычная команда и verify-pinned не обходят закрытый rollout."""
        with TemporaryDirectory() as temporary:
            host, state, record, _ = self._setup(Path(temporary))
            record("success")
            before = (host.state_dir / "state.json").read_bytes()
            for operation in (lambda: prod.deploy(host, "v1.2.3"), lambda: prod.verify_pinned(host)):
                with patch.object(prep, "deploy_lock") as lock:
                    lock.return_value.__enter__.return_value = None
                    with self.assertRaisesRegex(prod.DeployError, "rollout закрыт"):
                        operation()
                self.assertEqual((host.state_dir / "state.json").read_bytes(), before)
                self.assertEqual(host.commands, [])

    def test_deploy_wires_copy_and_switch_and_retains_new_database_on_health_failure(self):
        """За тестовым обходом gate отказ health сохраняет новую SQLite и true."""
        with TemporaryDirectory() as temporary:
            host, state, record, _ = self._setup(Path(temporary))
            record("success")
            host.compatibility_evidence = state["compatibility_evidence"]
            selected = SelectedRelease("karateka-git/compadre-assistant", "v1.2.3",
                                       "release/1.2", SHA, 5, "main-full.yml", 8, 1)
            original_run = host.run

            def run(*args, env=None):
                if args == ("git", "rev-parse", "HEAD"):
                    return "a" * 40
                return original_run(*args, env=env)

            def stop():
                for item in host.found.values():
                    item["State"]["Running"] = False

            with ExitStack() as stack:
                for name in ("require_switch_rollout", "installed", "require_baseline",
                             "timer_suspend", "restart_policy", "restore_checkout"):
                    stack.enter_context(patch.object(host, name))
                stack.enter_context(patch.object(host, "run", side_effect=run))
                stack.enter_context(patch.object(host, "stop_both", side_effect=stop))
                stack.enter_context(patch.object(host, "verify_both", side_effect=prod.DeployError("health")))
                stack.enter_context(patch.object(host, "timer_snapshot", return_value={"enabled": False, "active": False}))
                stack.enter_context(patch.object(prep, "deploy_lock", return_value=nullcontext()))
                stack.enter_context(patch.object(prep, "require_clean_checkout"))
                stack.enter_context(patch.object(prod, "_current_image", return_value=IMAGE))
                stack.enter_context(patch.object(prod.compatibility, "private_text", return_value="c" * 40))
                stack.enter_context(patch.object(prod, "_prepare_candidate", return_value=(
                    selected, prep.pinned_tag(SHA), IMAGE, MANIFEST, MANIFEST)))
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, selected.version)
            durable = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(durable["stage"], "degraded")
            self.assertIs(durable["admission_may_have_started"], True)
            self.assertEqual(switch.read_selected(host, durable), "new")
            self.assertFalse(any(item["State"]["Running"] for item in host.found.values()))
            self.assertEqual(len([command for command in host.commands if command[:2] == ("docker", "start")]), 1)

    def test_rollout_gate_and_repeat_registration_fail_closed(self):
        """Нет внешнего флага обхода gate; начальная регистрация не повторяется."""
        with TemporaryDirectory() as temporary:
            host, state, record, _ = self._setup(Path(temporary))
            with self.assertRaises(prod.DeployError):
                host.require_switch_rollout()
            with self.assertRaises(ValueError):
                switch.register_initial(host, state, record, prod._atomic_text)

    @unittest.skipUnless(shutil.which("docker"), "Нужен Docker Compose CLI")
    def test_real_compose_requires_same_selected_path_for_both_channels(self):
        """Настоящий config перекрывает legacy env_file и требует явный путь."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copyfile(prod.SOURCE_ROOT / "docker-compose.yml", root / "docker-compose.yml")
            (root / ".env").write_text("DATABASE_PATH=/app/data/app.db\n", encoding="utf-8")
            env = os.environ.copy()
            env.pop("COMPOSE_FILE", None)
            env["ASSISTANT_IMAGE"] = prep.pinned_tag(SHA)
            env["ASSISTANT_DATABASE_PATH"] = "/app/data/app-" + "d" * 32 + ".db"
            args = ["docker", "compose", "-f", str(root / "docker-compose.yml"),
                    "-f", str(prod.SOURCE_ROOT / "deploy/pinned-compose.yml"),
                    "-f", str(prod.SOURCE_ROOT / "deploy/critical-compose.yml"),
                    "-f", str(prod.SOURCE_ROOT / "deploy/database-compose.yml"),
                    "config", "--format", "json"]
            result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(result.stdout)
            for name in prep.CHANNELS:
                service = config["services"][name]
                self.assertEqual(service["environment"]["DATABASE_PATH"], env["ASSISTANT_DATABASE_PATH"])
                self.assertEqual(service["restart"], "no")
                self.assertEqual(service["image"], prep.pinned_tag(SHA))
                mounts = [item for item in service["volumes"] if item["target"] == "/app/data"]
                self.assertEqual(len(mounts), 1)
                self.assertEqual(Path(mounts[0]["source"]), root / "data")
            env.pop("ASSISTANT_DATABASE_PATH")
            result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
