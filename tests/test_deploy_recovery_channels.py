"""Проверяет раздельную готовность каналов при возврате SQLite v2."""

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
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
from tests.test_deploy_database_recovery import OLD_IMAGE, OLD_SHA, RecoveryHost, annotated_release
from tests.test_deploy_database_switch import IMAGE, MANIFEST


TG, VK = prep.CHANNELS
TIMER_BEFORE = {"enabled": True, "active": True}


@dataclass
class PollingClock:
    """Продвигает время только при ожидании проверкой готовности."""

    elapsed: int = 0

    def monotonic(self) -> float:
        """Возвращает монотонное время с ненулевым началом отсчёта."""
        return 100.0 + self.elapsed

    def sleep(self, seconds: int) -> None:
        """Продвигает часы на интервал опроса без реальной паузы."""
        self.elapsed += seconds


class ChannelRecoveryHost(RecoveryHost):
    """Моделирует Docker, systemd и готовность каналов для реального recovery."""

    verify_both = prod.Host.verify_both
    require_no_duplicate_polling = prod.Host.require_no_duplicate_polling

    def __init__(self, root: Path, clock: PollingClock, pending: str, ready_after: int | None) -> None:
        """Настраивает задержку health или polling VK; Telegram готов сразу."""
        super().__init__(root)
        self.clock = clock
        self.pending = pending
        self.ready_after = ready_after
        self.foreign_polling = False
        self.timer = {"enabled": False, "active": False}
        self.unit_adopted = False
        self.health_samples = []
        self.log_samples = []

    def _containers(self, path, status):
        found = super()._containers(path, status)
        found[TG]["Config"]["Env"].append("TELEGRAM_BOT_TOKEN=fixture-tg")
        found[VK]["Config"]["Env"].append("VK_BOT_TOKEN=fixture-vk")
        return found

    def _vk_ready(self, check: str) -> bool:
        return (self.pending != check or
                self.ready_after is not None and self.clock.elapsed >= self.ready_after)

    def containers(self) -> dict:
        """Возвращает ту же пару ID с health, соответствующим модельному времени."""
        found = super().containers()
        if all(item["State"]["Running"] for item in found.values()):
            for name, item in found.items():
                healthy = name == TG or self._vk_ready("health")
                item["State"]["Health"] = {"Status": "healthy" if healthy else "starting"}
            self.health_samples.append((self.clock.elapsed, self._vk_ready("health")))
        return found

    def logs(self, container_id: str) -> str:
        """Возвращает сигнал polling только после готовности канала."""
        channel = next(name for name, item in self.found.items() if item["Id"] == container_id)
        ready = channel == TG or self._vk_ready("polling")
        self.log_samples.append((self.clock.elapsed, channel, ready))
        return prod.LOG_SIGNALS[channel] if ready else ""

    def run(self, *args: str, env=None) -> str:
        """Моделирует инвентарь активных контейнеров для проверки credentials."""
        if args == ("docker", "ps", "--no-trunc", "-q"):
            ids = [item["Id"] for item in self.found.values() if item["State"]["Running"]]
            if self.foreign_polling:
                ids.append("foreign-poller")
            return "\n".join(ids)
        if args[:3] == ("docker", "inspect", "foreign-poller"):
            return json.dumps({"Config": {"Env": ["VK_BOT_TOKEN=fixture-vk"]}})
        if args == ("systemctl", "stop", prod.SERVICE):
            self.unit_adopted = False
        if args[:2] == ("docker", "stop") and len(args) > 3:
            for container_id in args[2:]:
                super().run(*args[:2], container_id, env=env)
            return ""
        return super().run(*args, env=env)

    def require_switch_rollout(self) -> None:
        """Разрешает финализацию в тестовом окружении без системных файлов."""

    def activate_unit(self) -> None:
        """Проверяет реальное принятие работающей пары при занятом deploy lock."""
        with patch.object(prep, "deploy_lock", side_effect=prep.PreparationError("locked")):
            runtime.start(self)
        self.unit_adopted = True

    def timer_restore(self, snapshot: dict[str, bool]) -> None:
        """Сохраняет восстановленное состояние backup timer."""
        self.timer = snapshot.copy()

    def timer_suspend(self) -> None:
        """Останавливает модельный backup timer и сохраняет вызов защиты."""
        self.timer = {"enabled": False, "active": False}
        super().timer_suspend()


class RecoveryChannelTests(unittest.TestCase):
    """Сравнивает задержку готовности, устойчивый отказ и чужой polling."""

    def test_recovery_waits_for_both_channels_on_the_same_pair(self) -> None:
        """За 24 модельные секунды подтверждает пару без повторного запуска."""
        for pending in ("health", "polling"):
            with self.subTest(pending=pending), TemporaryDirectory() as temporary:
                self._assert_recovery(Path(temporary).resolve(), pending, ready_after=12)

    def test_recovery_stops_both_if_one_channel_stays_unready(self) -> None:
        """По исчерпании 120 секунд сохраняет записи и останавливает пару."""
        for pending in ("health", "polling"):
            with self.subTest(pending=pending), TemporaryDirectory() as temporary:
                self._assert_recovery(Path(temporary).resolve(), pending, ready_after=None)

    def test_recovery_refuses_duplicate_polling_before_restart(self) -> None:
        """Совпадающий VK credential чужого контейнера закрывает запуск пары."""
        with TemporaryDirectory() as temporary:
            self._assert_recovery(Path(temporary).resolve(), "health", ready_after=12, foreign=True)

    def _assert_recovery(self, root: Path, pending: str, ready_after: int | None, *, foreign=False) -> None:
        clock = PollingClock()
        host = ChannelRecoveryHost(root, clock, pending, ready_after)
        api = annotated_release(host)
        selected = host.selected
        old = {"sha": OLD_SHA, "tag": prep.pinned_tag(OLD_SHA), "image_id": OLD_IMAGE}
        candidate = asdict(selected) | {"tag": prep.pinned_tag(selected.sha), "image_id": IMAGE}
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST, "controller_sha": "c" * 40,
                 "old_sha": old["sha"], "old_tag": old["tag"], "old_image_id": old["image_id"],
                 "candidate": candidate, "last_good": old | {"version": "v1.2.2"}, "timer_before": TIMER_BEFORE}

        def record(stage, **changes):
            prod._state(host, state, stage, **changes)

        with (patch.object(prod.compatibility, "private_text", side_effect=lambda path: path.read_text().strip()),
              patch.object(prod, "GitHub", return_value=api), patch.object(prod, "time", clock)):
            state["compatibility_evidence"] = prod.compatibility.verify_tag_evidence(
                api, host.checkout, selected, selected.compatibility.policy_sha, OLD_SHA)
            switch.register_initial(host, state, record, prod._atomic_text)
            switch.transition(host, state, record, prod._atomic_text)
            path = host.selected_database()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO messages VALUES(2, 'accepted-after-admission')")
            record("rollback_started", rollback_origin_stage="candidate_started")
            host.foreign_polling = foreign
            if ready_after is None or foreign:
                with self.assertRaisesRegex(prod.DeployError, "SQLite recovery не подтверждён") as failure:
                    prod._recover(host, state)
                reason = "Другой Docker-контейнер" if foreign else "Оба канала не подтвердили"
                self.assertIn(reason, str(failure.exception.__cause__))
                self.assertEqual(clock.elapsed, 0 if foreign else 120)
            else:
                prod._recover(host, state)
                self.assertEqual(state["stage"], "recovery_verified")
                self.assertEqual(clock.elapsed, 24)
                host.finalize_database(state, "rolled_back")
                self.assertEqual(clock.elapsed, 36)

        durable = prep.read_state(host.state_dir, host.checkout)
        failed = ready_after is None or foreign
        self.assertEqual(durable["stage"], "degraded" if failed else "rolled_back")
        self.assertEqual(durable["last_good"]["sha"], OLD_SHA)
        self.assertEqual(switch.read_selected(host, durable), "new")
        self.assertEqual(host.selected_database(), path)
        snapshot = Path(durable["snapshot"]["path"])
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), durable["snapshot"]["sha256"])
        prep.validate_schema(snapshot, MANIFEST)
        with closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(db.execute("SELECT text FROM messages").fetchall(), [("original",)])
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT text FROM messages ORDER BY id").fetchall(),
                             [("original",), ("accepted-after-admission",)])
        self.assertEqual((host.state_dir / "deploy-in-progress").exists(), failed)
        self.assertEqual(host.timer, {"enabled": False, "active": False} if failed else TIMER_BEFORE)
        self.assertIs(host.unit_adopted, not failed)
        self.assertEqual(host.starts, 1 if foreign else 2)
        self.assertEqual(host.serial, 1 if foreign else 2)
        self.assertEqual({item["State"]["Running"] for item in host.found.values()}, {not failed})
        if not foreign:
            ids = {name: item["Id"] for name, item in host.found.items()}
            self.assertEqual(durable["recovery_containers"], ids)
            self.assertEqual({item["Image"] for item in host.found.values()}, {OLD_IMAGE})
            if not failed:
                self.assertEqual(durable["boot"]["containers"], ids)
                self.assertEqual(durable["boot"]["sha"], OLD_SHA)
            self.assertEqual([command for command in host.commands if command[:2] == ("docker", "start")][-1],
                             ("docker", "start", ids[TG], ids[VK]))
            if pending == "health":
                self.assertIn((0, False), host.health_samples)
                self.assertEqual(host.found[TG]["State"]["Health"]["Status"], "healthy")
                if failed:
                    self.assertEqual(host.log_samples, [])
                else:
                    self.assertEqual(host.log_samples[0], (12, TG, True))
            else:
                self.assertIn((0, TG, True), host.log_samples)
                self.assertIn((0, VK, False), host.log_samples)
                self.assertEqual(any(channel == VK and ready for _, channel, ready in host.log_samples), not failed)
