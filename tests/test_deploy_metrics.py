"""Проверяет историю времени выпуска и независимость наблюдения от recovery."""

from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from scripts import deploy_metrics as metrics
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from tests import test_deploy_prod as fixtures
from tests.test_deploy_prod import FakeHost, NEW_SHA


class DeployMetricsTests(unittest.TestCase):
    """Проверяет реальные ветви контроллера с изолированными внешними системами."""

    def setUp(self) -> None:
        """Создаёт fake host и подменяет Git, Docker и Linux lock."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.host = FakeHost(Path(self.stack.enter_context(TemporaryDirectory())))
        for replacement in fixtures.DeployProdTests()._patches():
            self.stack.enter_context(replacement)

    def report(self) -> dict:
        """Читает единственный приватный отчёт текущего вызова."""
        paths = list(self.host.state_dir.glob("metrics-*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def test_success_records_each_reached_stage_and_identity(self) -> None:
        """Отчёт связывает точный SHA/attempt с обоими фактическими verify."""
        prod.deploy(self.host, "v1.0.0")
        report = self.report()
        self.assertEqual((report["sha"], report["run_id"], report["run_attempt"]), (NEW_SHA, 8, 1))
        self.assertEqual((report["stage"], report["result"]), ("success", "success"))
        self.assertEqual([item["stage"] for item in report["stages"]], [
            "build", "deploy.stop_both", "backup", "migrations",
            "deploy.verify_both_1", "deploy.verify_both_2",
        ])
        for item in report["stages"]:
            self.assertEqual(item["result"], "success")
            self.assertIsNotNone(item["started_at"])
            self.assertIsNotNone(item["ended_at"])
            self.assertGreaterEqual(item["seconds"], 0)
        self.assertFalse(report["downtime_open"])
        self.assertGreaterEqual(report["wall_seconds"], report["downtime_seconds"])

    def test_first_verify_failure_does_not_invent_second_candidate_verify(self) -> None:
        """Пропущенный verify отсутствует, возврат имеет два отдельных измерения."""
        self.host.fail_candidate = True
        with self.assertRaises(prod.DeployError):
            prod.deploy(self.host, "v1.0.0")
        report = self.report()
        self.assertEqual((report["stage"], report["result"]), ("rolled_back", "failure"))
        stages = {item["stage"]: item for item in report["stages"]}
        self.assertEqual(stages["deploy.verify_both_1"]["result"], "failure")
        self.assertNotIn("deploy.verify_both_2", stages)
        self.assertEqual(stages["recovery.verify_both_2"]["result"], "success")
        self.assertFalse(report["downtime_open"])

    def test_build_failure_is_saved_without_secret_or_downtime(self) -> None:
        """Отказ до stop сохраняется без текста исключения и ложного простоя."""
        with patch.object(prep, "build_pinned_image", side_effect=RuntimeError("SECRET token")):
            with self.assertRaises(RuntimeError):
                prod.deploy(self.host, "v1.0.0")
        report = self.report()
        self.assertEqual(report["stage"], "failed_preflight")
        self.assertEqual(report["stages"][0]["reason"], "RuntimeError")
        self.assertEqual(report["downtime_seconds"], 0)
        self.assertNotIn("SECRET", json.dumps(report))

    def test_degraded_downtime_remains_open(self) -> None:
        """Неуспех обоих verify не выдаёт восстановление доступности за факт."""
        with patch.object(self.host, "verify_both", side_effect=prod.DeployError("failure")):
            with self.assertRaises(prod.DeployError):
                prod.deploy(self.host, "v1.0.0")
        report = self.report()
        self.assertEqual(report["stage"], "degraded")
        self.assertTrue(report["downtime_open"])
        self.assertFalse(self.host.running)

    def test_backup_failure_records_failure_before_rollback(self) -> None:
        """Неудачный backup остаётся в истории даже после успешного возврата."""
        with patch.object(prep, "backup_sqlite", side_effect=OSError("SECRET path")):
            with self.assertRaises(prod.DeployError):
                prod.deploy(self.host, "v1.0.0")
        stages = {item["stage"]: item for item in self.report()["stages"]}
        self.assertEqual(stages["backup"]["reason"], "OSError")
        self.assertNotIn("migrations", stages)
        self.assertEqual(stages["recovery.verify_both_2"]["result"], "success")

    def test_migration_failure_records_failure_before_rollback(self) -> None:
        """Ошибка мигратора фиксируется без ложных candidate verify."""
        with patch.object(self.host, "migrate", side_effect=RuntimeError("SECRET SQL")):
            with self.assertRaises(prod.DeployError):
                prod.deploy(self.host, "v1.0.0")
        stages = {item["stage"]: item for item in self.report()["stages"]}
        self.assertEqual(stages["migrations"]["reason"], "RuntimeError")
        self.assertNotIn("deploy.verify_both_1", stages)
        self.assertNotIn("SECRET", json.dumps(self.report()))

    def test_second_verify_failure_is_a_separate_reached_stage(self) -> None:
        """Смена image при activate обнаруживается именно вторым verify."""
        self.host.replace_on_activate = True
        with self.assertRaises(prod.DeployError):
            prod.deploy(self.host, "v1.0.0")
        stages = {item["stage"]: item for item in self.report()["stages"]}
        self.assertEqual(stages["deploy.verify_both_1"]["result"], "success")
        self.assertEqual(stages["deploy.verify_both_2"]["result"], "failure")

    def test_report_write_failure_never_triggers_rollback(self) -> None:
        """Отказ диска телеметрии не отменяет успешную установку."""
        with patch.object(prod, "_atomic_text", side_effect=OSError("full")):
            prod.deploy(self.host, "v1.0.0")
        self.assertEqual(prep.read_state(self.host.state_dir, self.host.checkout)["stage"], "success")
        self.assertEqual(self.host.actions.count("stop_both"), 1)

    def test_warning_is_emitted_while_build_still_waits(self) -> None:
        """Долгий build остаётся в ожидании после предупреждения и завершается штатно."""
        warned = Event()
        original_emit = metrics._emit

        def emit(message: str) -> None:
            """Отмечает предупреждение до завершения искусственно занятого build."""
            if "stage=build result=waiting" in message:
                warned.set()
            original_emit(message)

        def build(*args: object) -> tuple[str, str]:
            """Держит build до реального сигнала фонового наблюдателя."""
            self.assertTrue(warned.wait(3), "Предупреждение не появилось во время build")
            in_progress = self.report()
            self.assertEqual(in_progress["stages"][0]["result"], "running")
            from tests.test_deploy_prod import NEW_IMAGE
            return prep.pinned_tag(NEW_SHA), NEW_IMAGE

        with patch.object(metrics, "WARNING_SECONDS", 0.01), patch.object(metrics, "_emit", emit), patch.object(
            prep, "build_pinned_image", side_effect=build,
        ):
            prod.deploy(self.host, "v1.0.0")
        self.assertEqual(self.report()["result"], "success")
        self.assertEqual(self.host.actions.count("stop_both"), 1)

    def test_downtime_excludes_build_and_closes_at_final_verification(self) -> None:
        """Монотонные часы дают проверяемую границу простоя при переводе wall clock."""
        tick = [0.0]
        snapshots = []
        report = metrics.Report(Path("unused"), lambda path, value: snapshots.append(json.loads(value)), "deploy")
        token = metrics._current.set(report)
        self.addCleanup(metrics._current.reset, token)
        with patch.object(metrics.time, "monotonic", side_effect=lambda: tick[0]):
            report.started = 0
            with metrics.stage("build"):
                tick[0] = 30
            with metrics.stage("deploy.stop_both"):
                tick[0] = 35
            with metrics.stage("deploy.verify_both_2"):
                tick[0] = 60
            metrics.available()
            tick[0] = 65
            report.save()
        self.assertEqual(snapshots[-1]["wall_seconds"], 65)
        self.assertEqual(snapshots[-1]["downtime_seconds"], 30)
        self.assertEqual(snapshots[-1]["stages"][0]["seconds"], 30)
