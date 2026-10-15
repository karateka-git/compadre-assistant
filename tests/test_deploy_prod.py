"""Проверяет переключение и возврат без Docker, GitHub и production VPS."""

from __future__ import annotations

from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts.select_deploy_release import SelectedRelease


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
OLD_IMAGE = "sha256:" + "1" * 64
NEW_IMAGE = "sha256:" + "2" * 64


class FakeHost(prod.Host):
    """Хранит наблюдаемые изменения канала и unit только в памяти."""

    def __init__(self, root: Path) -> None:
        """Создаёт искусственный checkout и SQLite с одной миграцией."""
        super().__init__(root / "checkout", root / "state")
        self.checkout.mkdir()
        self.state_dir.mkdir()
        migrations = self.checkout / "compadre_assistant/core/data/sqlite/migrations"
        migrations.mkdir(parents=True)
        (migrations / "0001_initial_schema.sql").write_text("-- test", encoding="utf-8")
        data = self.checkout / "data"
        data.mkdir()
        with closing(sqlite3.connect(data / "app.db")) as connection:
            connection.execute("CREATE TABLE schema_migrations (version INTEGER, name TEXT)")
            connection.execute("INSERT INTO schema_migrations VALUES (1, '0001_initial_schema.sql')")
            connection.commit()
        self.head = OLD_SHA
        self.current_image = OLD_IMAGE
        self.running = True
        self.policy = "unless-stopped"
        self.pin = prep.pinned_tag(OLD_SHA)
        self.timer_state = {"enabled": True, "active": True}
        self.actions: list[str] = []
        self.fail_candidate = False
        self.replace_on_activate = False

    def run(self, *args: str, env: dict[str, str] | None = None) -> str:
        """Возвращает только нужный фиктивный Git HEAD."""
        if args == ("git", "rev-parse", "HEAD"):
            return self.head
        raise AssertionError(args)

    def installed(self, old_id: str, old_sha: str) -> None:
        """Подтверждает согласованность старого pin в fake host."""
        assert old_id == OLD_IMAGE and old_sha == OLD_SHA

    def require_compose_pin(self, tag: str, *, critical: bool = False) -> None:
        """Фиксирует проверку временного restart=no override."""
        self.actions.append("compose_critical" if critical else "compose_normal")

    def require_no_duplicate_polling(self) -> None:
        """Подтверждает отсутствие другого fake Docker polling."""
        self.actions.append("no_duplicate")

    def select(self, version: str) -> SelectedRelease:
        """Возвращает один проверенный fake Release и CI run."""
        return SelectedRelease("karateka-git/compadre-assistant", version, "release/1.0",
                               NEW_SHA, 5, "main-full.yml", 8, 1)

    def fetch(self, selected: SelectedRelease) -> None:
        """Переключает только фиктивный checkout."""
        self.head = selected.sha
        self.actions.append("fetch")

    def restore_checkout(self, sha: str) -> None:
        """Возвращает fake checkout к старому commit."""
        self.head = sha
        self.actions.append("checkout_old")

    def image(self, reference: str) -> dict:
        """Возвращает два неизменяемых fake image ID."""
        image_id = NEW_IMAGE if reference in (NEW_IMAGE, prep.pinned_tag(NEW_SHA)) else OLD_IMAGE
        return {"Id": image_id, "Config": {"Labels": {prep.OCI_REVISION: NEW_SHA}}}

    def containers(self) -> dict:
        """Представляет два канала на одном текущем образе."""
        return {name: {"Id": name, "Image": self.current_image,
                       "State": {"Running": self.running, "Health": {"Status": "healthy"}},
                       "RestartCount": 0,
                       "HostConfig": {"RestartPolicy": {"Name": self.policy}}}
                for name in prep.CHANNELS}

    def runtime_tag(self) -> str:
        """Читает фиктивный pin."""
        return self.pin

    def set_runtime(self, tag: str, expected_id: str) -> None:
        """Фиксирует смену pin только на известный ID."""
        assert self.image(tag)["Id"] == expected_id
        self.pin = tag
        self.actions.append("pin")

    def marker(self, enabled: bool) -> None:
        """Сохраняет marker во внешнем временном каталоге."""
        path = self.state_dir / "deploy-in-progress"
        if enabled:
            path.write_text("critical", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        self.actions.append("marker_on" if enabled else "marker_off")

    def timer_snapshot(self) -> dict[str, bool]:
        """Возвращает копию текущих параметров таймера."""
        return self.timer_state.copy()

    def timer_suspend(self) -> None:
        """Отключает таймер в fake host."""
        self.timer_state = {"enabled": False, "active": False}
        self.actions.append("timer_off")

    def timer_restore(self, snapshot: dict[str, bool]) -> None:
        """Восстанавливает запомненное состояние таймера."""
        self.timer_state = snapshot.copy()
        self.actions.append("timer_on")

    def restart_policy(self, value: str) -> None:
        """Фиксирует запрет автозапуска до остановки."""
        self.policy = value
        self.actions.append("restart_" + value)

    def stop_both(self) -> None:
        """Останавливает оба fake канала."""
        self.running = False
        self.actions.append("stop_both")

    def start_both(self, tag: str, expected_id: str) -> None:
        """Запускает оба fake канала с проверенным pin."""
        self.require_compose_pin(tag, critical=True)
        self.current_image = expected_id
        self.running = True
        self.policy = "no"
        self.actions.append("start_both")

    def activate_unit(self) -> None:
        """Имитирует штатный запуск service после завершения critical override."""
        self.actions.append("unit_active")
        if self.replace_on_activate and self.current_image == NEW_IMAGE:
            self.current_image = OLD_IMAGE

    def verify_both(self, expected_id: str, expected_sha: str | None, *, timeout: int = 120) -> None:
        """Имитирует отказ одного candidate канала или общий успех."""
        self.actions.append("verify_both")
        if self.current_image != expected_id or not self.running:
            raise prod.DeployError("Неверный образ")
        if self.fail_candidate and expected_id == NEW_IMAGE:
            raise prod.DeployError("VK не подтвердил Long Poll")

    def verify_installed(self, good: dict) -> None:
        """Проверяет fake pin и оба канала без нового запуска."""
        assert good["image_id"] == self.current_image and self.running

    def migrate(self, image_id: str) -> None:
        """Фиксирует отдельный no-op migrate вызов."""
        assert image_id == NEW_IMAGE
        self.actions.append("migrate")


class DeployProdTests(unittest.TestCase):
    """Проверяет отказные границы и успешное состояние контроллера."""

    def _patches(self):
        """Подменяет внешние Git/Docker операции при тесте state machine."""
        return (
            patch.object(prep, "deploy_lock", return_value=nullcontext()),
            patch.object(prep, "require_clean_checkout"),
            patch.object(prep, "require_exact_tag"),
            patch.object(prep, "build_pinned_image", return_value=(prep.pinned_tag(NEW_SHA), NEW_IMAGE)),
            patch.object(prep, "check_candidate_image"),
            patch.object(prep, "require_disk_space"),
        )

    def test_success_then_same_version_does_not_recreate(self) -> None:
        """Успех фиксируется после двух каналов, повтор не вызывает stop/up."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            patches = self._patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                prod.deploy(host, "v1.0.0")
                state = prep.read_state(host.state_dir, host.checkout)
                self.assertEqual(state["stage"], "success")
                self.assertEqual(state["last_good"]["image_id"], NEW_IMAGE)
                self.assertLess(host.actions.index("restart_no"), host.actions.index("stop_both"))
                self.assertLess(host.actions.index("migrate"), host.actions.index("start_both"))
                self.assertEqual(host.policy, "unless-stopped")
                self.assertLess(host.actions.index("compose_critical"), host.actions.index("start_both"))
                self.assertLess(host.actions.index("verify_both"), host.actions.index("unit_active"))
                before = host.actions.copy()
                prod.deploy(host, "v1.0.0")
                self.assertEqual(host.actions, before)

    def test_one_channel_failure_rolls_both_back_without_database_restore(self) -> None:
        """Отказ candidate возвращает оба канала и сохраняет backup отдельно."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            host.fail_candidate = True
            patches = self._patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.0")
            state = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(state["stage"], "rolled_back")
            self.assertEqual(host.current_image, OLD_IMAGE)
            self.assertEqual(host.head, OLD_SHA)
            self.assertTrue(host.running)
            self.assertEqual(host.timer_state, {"enabled": True, "active": True})
            self.assertFalse((host.state_dir / "deploy-in-progress").exists())
            self.assertTrue(Path(state["backup_id"]).is_file())

    def test_final_systemd_recreation_is_checked_again(self) -> None:
        """Подмена image при normal Compose start вызывает rollback до success."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            host.replace_on_activate = True
            patches = self._patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.0")
            state = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(state["stage"], "rolled_back")
            self.assertEqual(host.current_image, OLD_IMAGE)
            self.assertEqual(host.actions.count("verify_both"), 4)

    def test_pending_migration_fails_before_stop(self) -> None:
        """Неизвестный SQL не допускается к автоматической миграции."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            migration = (host.checkout / "compadre_assistant/core/data/sqlite/migrations"
                         / "0002_new.sql")
            migration.write_text("CREATE TABLE new_table (id INTEGER);", encoding="utf-8")
            patches = self._patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.0")
            self.assertNotIn("stop_both", host.actions)
            self.assertEqual(host.head, OLD_SHA)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "failed_preflight")

    def test_incomplete_switch_rolls_back_before_new_attempt(self) -> None:
        """После reboot незавершённый переход не объявляется успешным."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            host.head = NEW_SHA
            host.current_image = NEW_IMAGE
            host.pin = prep.pinned_tag(NEW_SHA)
            host.marker(True)
            host.timer_suspend()
            prep.write_state(host.state_dir, host.checkout, {
                "stage": "candidate_started", "old_sha": OLD_SHA, "old_image_id": OLD_IMAGE,
                "old_tag": prep.pinned_tag(OLD_SHA),
                "schema_before": ["0001_initial_schema.sql"],
                "timer_before": {"enabled": True, "active": True},
                "failed_version": "v1.0.1",
            })
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.1")
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "rolled_back")
            self.assertEqual(host.current_image, OLD_IMAGE)
            self.assertTrue(prep.read_state(host.state_dir, host.checkout)["retry_after"])

    def test_changed_schema_after_reboot_stays_degraded(self) -> None:
        """Частичная миграция не вызывает restore backup или запуск старого кода."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            host.head = NEW_SHA
            host.current_image = NEW_IMAGE
            host.marker(True)
            with closing(sqlite3.connect(host.checkout / "data/app.db")) as connection:
                connection.execute("INSERT INTO schema_migrations VALUES (2, '0002_partial.sql')")
                connection.commit()
            prep.write_state(host.state_dir, host.checkout, {
                "stage": "migration_started", "old_sha": OLD_SHA, "old_image_id": OLD_IMAGE,
                "old_tag": prep.pinned_tag(OLD_SHA),
                "schema_before": ["0001_initial_schema.sql"],
                "timer_before": {"enabled": True, "active": True},
            })
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.1")
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "degraded")
            self.assertFalse(host.running)
            self.assertEqual(host.current_image, NEW_IMAGE)
            self.assertTrue((host.state_dir / "deploy-in-progress").exists())

    def test_interrupted_preflight_restores_checkout_without_stopping_channels(self) -> None:
        """Reboot перед marker возвращает checkout, сохраняя работающие каналы."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            host.head = NEW_SHA
            prep.write_state(host.state_dir, host.checkout, {
                "stage": "ready", "old_sha": OLD_SHA, "old_image_id": OLD_IMAGE,
                "old_tag": prep.pinned_tag(OLD_SHA),
                "schema_before": ["0001_initial_schema.sql"],
                "timer_before": {"enabled": True, "active": True},
            })
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.1")
            self.assertEqual(host.head, OLD_SHA)
            self.assertTrue(host.running)
            self.assertNotIn("stop_both", host.actions)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "failed_preflight")

    def test_process_death_during_build_recovers_original_checkout(self) -> None:
        """Аварийное завершение после fetch оставляет rollback manifest до смены checkout."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            patches = self._patches()
            with patches[0], patches[1], patches[2], patch.object(
                prep, "build_pinned_image", side_effect=SystemExit(77),
            ), patches[4], patches[5]:
                with self.assertRaises(SystemExit):
                    prod.deploy(host, "v1.0.0")
            state = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(state["stage"], "preparing")
            self.assertEqual(state["old_sha"], OLD_SHA)
            self.assertEqual(host.head, NEW_SHA)
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.0")
            state = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(state["stage"], "failed_preflight")
            self.assertEqual(state["failed_version"], "v1.0.0")
            self.assertEqual(host.head, OLD_SHA)
            self.assertTrue(host.running)
            self.assertNotIn("stop_both", host.actions)
            before = host.actions.copy()
            with patch.object(prep, "deploy_lock", return_value=nullcontext()):
                with self.assertRaisesRegex(prod.DeployError, "временно закрыт"):
                    prod.deploy(host, "v1.0.0")
            self.assertEqual(host.actions, before)

    def test_failed_checkout_restore_keeps_recovery_manifest(self) -> None:
        """Ошибка возврата checkout не стирает manifest для следующего recovery."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            patches = self._patches()
            with patches[0], patches[1] as clean, patches[2], patch.object(
                prep, "build_pinned_image", side_effect=prod.DeployError("build"),
            ), patches[4], patches[5], patch.object(
                host, "restore_checkout", side_effect=prod.DeployError("checkout"),
            ):
                clean.side_effect = lambda _checkout, sha: (
                    None if host.head == sha else (_ for _ in ()).throw(prod.DeployError("HEAD"))
                )
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.0")
            state = prep.read_state(host.state_dir, host.checkout)
            self.assertEqual(state["stage"], "preparing")
            self.assertEqual(state["old_sha"], OLD_SHA)
            self.assertEqual(host.head, NEW_SHA)

    def test_backup_and_migration_faults_restore_both_channels(self) -> None:
        """Отказы после stop возвращают два старых канала и не теряют timer."""
        for fault in ("backup", "migrate"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                host = FakeHost(Path(temporary))
                patches = self._patches()
                with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patch.object(
                    prep if fault == "backup" else host,
                    "backup_sqlite" if fault == "backup" else "migrate",
                    side_effect=prod.DeployError(fault),
                ):
                    with self.assertRaises(prod.DeployError):
                        prod.deploy(host, "v1.0.0")
                self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "rolled_back")
                self.assertEqual(host.current_image, OLD_IMAGE)
                self.assertEqual(host.timer_state, {"enabled": True, "active": True})
                self.assertFalse((host.state_dir / "deploy-in-progress").exists())

    def test_state_write_failure_after_marker_uses_rollback(self) -> None:
        """Ошибка записи стадии после marker не оставляет один работающий канал."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            patches = self._patches()
            real_write = prep.write_state
            failed = False

            def flaky_write(state_dir, checkout, state):
                """Один раз имитирует потерю записи на границе critical."""
                nonlocal failed
                if state.get("stage") == "critical" and not failed:
                    failed = True
                    raise OSError("temporary state failure")
                real_write(state_dir, checkout, state)

            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patch.object(
                prep, "write_state", side_effect=flaky_write,
            ):
                with self.assertRaises(prod.DeployError):
                    prod.deploy(host, "v1.0.0")
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "rolled_back")
            self.assertEqual(host.current_image, OLD_IMAGE)
            self.assertTrue(host.running)
            self.assertFalse((host.state_dir / "deploy-in-progress").exists())

    def test_verify_pinned_exercises_two_channels_without_new_release(self) -> None:
        """Контролируемая проверка старого pin использует полный recovery path."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            with patch.object(prep, "deploy_lock", return_value=nullcontext()), patch.object(
                prep, "require_clean_checkout",
            ):
                prod.verify_pinned(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "pinned_verified")
            self.assertEqual(host.head, OLD_SHA)
            self.assertEqual(host.current_image, OLD_IMAGE)
            self.assertTrue(host.running)
            self.assertLess(host.actions.index("marker_on"), host.actions.index("stop_both"))
            self.assertEqual(host.actions.count("verify_both"), 2)
            self.assertEqual(host.timer_state, {"enabled": True, "active": True})

    def test_verify_pinned_failure_restores_old_channels(self) -> None:
        """Отказ при пробном старте старого pin приводит к полному возврату."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            original_verify = host.verify_both
            calls = 0

            def fail_once(expected_id, expected_sha, *, timeout=120):
                """Первый healthcheck падает, а rollback снова проверяет оба канала."""
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise prod.DeployError("failed health")
                original_verify(expected_id, expected_sha, timeout=timeout)

            with patch.object(prep, "deploy_lock", return_value=nullcontext()), patch.object(
                prep, "require_clean_checkout",
            ), patch.object(host, "verify_both", side_effect=fail_once):
                with self.assertRaises(prod.DeployError):
                    prod.verify_pinned(host)
            self.assertEqual(prep.read_state(host.state_dir, host.checkout)["stage"], "rolled_back")
            self.assertEqual(host.current_image, OLD_IMAGE)
            self.assertTrue(host.running)
            self.assertEqual(host.timer_state, {"enabled": True, "active": True})

    def test_restores_original_disabled_timer_state(self) -> None:
        """После выпуска таймер остаётся выключенным, если таким был до него."""
        with TemporaryDirectory() as temporary:
            host = FakeHost(Path(temporary))
            host.timer_state = {"enabled": False, "active": False}
            patches = self._patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                prod.deploy(host, "v1.0.0")
            self.assertEqual(host.timer_state, {"enabled": False, "active": False})

    def test_duplicate_polling_detected_without_logging_token(self) -> None:
        """Чужой контейнер с тем же токеном отклоняется до остановки."""
        with TemporaryDirectory() as temporary:
            host = prod.Host(Path(temporary) / "checkout", Path(temporary) / "state")
            own = {
                "tg_assistant": {"Id": "tg", "Config": {"Env": ["TELEGRAM_BOT_TOKEN=private-tg"]}},
                "vk_assistant": {"Id": "vk", "Config": {"Env": ["VK_BOT_TOKEN=private-vk"]}},
            }
            foreign = {"Id": "other", "Config": {"Env": ["VK_BOT_TOKEN=private-vk"]}}
            def fake_run(*args: str, env=None) -> str:
                """Возвращает синтетический Docker inspect без внешнего процесса."""
                if args == ("docker", "ps", "--no-trunc", "-q"):
                    return "tg\nvk\nother"
                if args[:2] == ("docker", "inspect"):
                    return json.dumps(foreign)
                raise AssertionError(args)
            with patch.object(host, "containers", return_value=own), patch.object(host, "run", side_effect=fake_run):
                with self.assertRaisesRegex(prod.DeployError, "Другой Docker-контейнер") as error:
                    host.require_no_duplicate_polling()
            self.assertNotIn("private-vk", str(error.exception))

    def test_migration_command_uses_candidate_image_without_env_file(self) -> None:
        """CLI миграции отделён от Compose и не получает production .env."""
        with TemporaryDirectory() as temporary:
            host = prod.Host(Path(temporary) / "checkout", Path(temporary) / "state")
            with patch.object(host, "run", return_value="") as run:
                host.migrate(NEW_IMAGE)
            args = run.call_args.args
            self.assertIn(NEW_IMAGE, args)
            self.assertIn("--network", args)
            self.assertIn("none", args)
            self.assertIn("--read-only", args)
            self.assertIn("--migrate", args)
            self.assertNotIn("--env-file", args)

    def test_real_compose_merge_sets_no_restart_during_critical(self) -> None:
        """Docker Compose действительно применяет critical override к обоим каналам."""
        if shutil.which("docker") is None:
            self.skipTest("Docker Compose недоступен")
        source = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("docker-compose.yml", "deploy/pinned-compose.yml",
                         "deploy/critical-compose.yml"):
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((source / name).read_bytes())
            (root / ".env").write_text("APP_DEBUG=false\n", encoding="utf-8")
            environment = {key: value for key, value in os.environ.items()
                           if key.upper() in ("PATH", "SYSTEMROOT", "COMSPEC", "HOME", "USERPROFILE",
                                              "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH",
                                              "TEMP", "TMP", "PROGRAMFILES", "PROGRAMDATA",
                                              "COMMONPROGRAMFILES", "DOCKER_CONFIG")}
            environment["ASSISTANT_IMAGE"] = prep.pinned_tag(NEW_SHA)
            command = ["docker", "compose", "-f", str(root / "docker-compose.yml"),
                       "-f", str(root / "deploy/pinned-compose.yml"),
                       "-f", str(root / "deploy/critical-compose.yml"),
                       "config", "--format", "json"]
            result = subprocess.run(command, cwd=root, env=environment, text=True,
                                    capture_output=True, check=False)
            if result.returncode:
                self.fail(f"Docker Compose config завершился с кодом {result.returncode}: {result.stderr}")
            config = json.loads(result.stdout)
            for name in prep.CHANNELS:
                service = config["services"][name]
                self.assertEqual(service["image"], prep.pinned_tag(NEW_SHA))
                self.assertEqual(service["pull_policy"], "never")
                self.assertEqual(service["restart"], "no")

            # Внешний COMPOSE_FILE обязан сохранять имя проекта, mounts и build context.
            environment["COMPOSE_FILE"] = os.pathsep.join((
                str(root / "docker-compose.yml"), str(root / "deploy/pinned-compose.yml"),
            ))
            pinned = subprocess.run(["docker", "compose", "config", "--format", "json"],
                                    cwd=root, env=environment, text=True, capture_output=True)
            self.assertEqual(pinned.returncode, 0, pinned.stderr)
            pinned_config = json.loads(pinned.stdout)
            base = subprocess.run(["docker", "compose", "-f", str(root / "docker-compose.yml"),
                                   "config", "--format", "json"], cwd=root, env=environment | {
                                       "COMPOSE_FILE": str(root / "docker-compose.yml"),
                                   }, text=True, capture_output=True)
            self.assertEqual(base.returncode, 0, base.stderr)
            base_config = json.loads(base.stdout)
            self.assertEqual(pinned_config["name"], base_config["name"])
            for name in prep.CHANNELS:
                self.assertEqual(pinned_config["services"][name]["volumes"],
                                 base_config["services"][name]["volumes"])
                self.assertEqual(pinned_config["services"][name]["build"],
                                 base_config["services"][name]["build"])


if __name__ == "__main__":
    unittest.main()
