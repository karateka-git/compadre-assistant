"""Проверяет подписанный PATCH через штатный CLI, SSH-парсер и общий журнал VPS."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import AbstractHTTPHandler

from scripts import deploy_attempt
from scripts.full_ci_controller import HttpLocation, collect_http_metrics, immutable_git_reads
from scripts.issue_patch_admission import prepare_admission
from scripts.predeploy_compatibility import EvidenceAPI
from tests import test_patch_http_baseline as fixture
from tests.test_issue_patch_admission import completed_event
from tests.test_signed_patch_runner import SignedPatchAPI


class Controller(fixture._V2ControllerFixture):
    """Соединяет подписанный runner с ограниченным SSH и CLI контроллера на стенде."""

    def execute(self, arguments, **options):
        """Передаёт реальные аргументы и байты через парсер root-dispatcher."""
        parser = Path(__file__).resolve().parents[1] / "deploy" / "attempt-ssh.py"
        frame = " ".join(arguments).encode("ascii") + b"\0" + (options.get("input_text") or "").encode("ascii")
        reply = []

        def run(argv, *, input, env):
            if tuple(argv) != arguments[2:] or env != {"PATH": "/usr/bin:/bin"}:
                raise AssertionError("argv или env SSH-диспетчера отличаются")
            response = super(Controller, self).execute(arguments, input_text=input.decode("ascii"))
            reply.append(response)
            return response

        dispatch = runpy.run_path(str(parser))["dispatch"]
        with patch.dict(dispatch.__globals__, subprocess=SimpleNamespace(run=run)):
            if dispatch(frame) != 0:
                raise AssertionError("Ошибка SSH-диспетчера")
        return reply[0]


def measure_signed_patch() -> list[dict]:
    """Измеряет полный подписанный PATCH на двух объёмах прежней истории v2.

    Returns:
        Две трассы от запуска runner до закрытия журнала контроллера.

    Raises:
        AssertionError: Расход превысил бюджет, учёт разошёлся с транспортом
            или приёмка зафиксировала другой выпуск.
    """
    scenarios = []
    for history_size in (fixture.HISTORY_SIZE, 1000):
        with tempfile.TemporaryDirectory(prefix="signed-patch-budget-") as directory:
            root = Path(directory)
            graph, runner, fingerprint = fixture._fixture(root)
            api = SignedPatchAPI(graph, fingerprint, history_size)
            ci_api = fixture._V2RestFixture(graph, fingerprint, history_size)
            with (patch.object(AbstractHTTPHandler, "do_open", side_effect=ci_api.transport),
                  immutable_git_reads(), collect_http_metrics(HttpLocation.CI) as ci_metrics):
                proof = prepare_admission(EvidenceAPI(fixture.REPOSITORY, fixture.RUNNER_TOKEN),
                                          completed_event(ci_api), graph.checkout, fixture.POLICY_SHA)
            keys = api.configure_signer(root, fingerprint, proof=proof)
            if len(api.releases) != 16 or len(graph.pairs) != 2 or graph.main == graph.candidate:
                raise AssertionError("Изменились условия сравнения PATCH")
            with (patch.object(fixture.system, "OLD_SHA", graph.base),
                  patch.object(fixture.system, "NEW_SHA", graph.candidate),
                  patch.dict(os.environ, RELEASE_SIGNER_ATTEMPT="99:1", PRODUCTION_SIGNED_PATCH="true",
                             PATCH_ALLOWED_SIGNERS_FILE=str(keys), COMPATIBILITY_POLICY_SHA=fixture.POLICY_SHA)):
                controller = Controller(root / "vps", api)
                (controller.host.state_dir / "patch-allowed-signers").write_bytes(keys.read_bytes())
                scenario = fixture._measure(root, f"signed_patch_{history_size}", api, runner, controller=controller)
            scenario["prior_signer_http"] = ci_metrics.rows()
            journal = json.loads((controller.host.state_dir / deploy_attempt.JOURNAL).read_bytes())
            receipt = journal["result"]["receipt"]
            deployment_id = journal["deployment_id"]
            release = api.releases[fixture.VERSION]
            tag = api.tags[api.refs[fixture.VERSION]["object"]["sha"]]
            observed_shas = {tag["object"]["sha"], release["target_commitish"], api.deployments[deployment_id]["sha"],
                             receipt["sha"], controller.host.head}
            if observed_shas != {graph.candidate} or receipt["release_id"] != release["id"]:
                raise AssertionError("Тег, Release, Deployment и runtime относятся к разным объектам")
            if ((receipt["version"], receipt["migration"]) != (fixture.VERSION, fixture.MIGRATION)
                    or (controller.installed, controller.switches, controller.cleanup_count) != (True, 1, 1)):
                raise AssertionError("Установка и очистка не завершили однократное переключение")
            if (not journal["closed"] or journal["result"]["outcome"] != "success"
                    or journal["patch"]["terminal"] != api.statuses[deployment_id][0]["id"]
                    or api.posts.count("/deployments") != 1 or api.status_writes != ["in_progress", "success"]):
                raise AssertionError("Журнал VPS и итоговый статус Deployment расходятся")
            scenario["git_fetch_operations"] = controller.host.actions.count("fetch")
            scenario["github_api"] = sum(count for (_, target, _), count in api.calls.items()
                                         if target in {"github_api", "github_graphql_query"})
            if scenario["github_api"] > 20:
                raise AssertionError(f"Превышен бюджет PATCH: {scenario['github_api']}")
            scenarios.append(scenario)
    if scenarios[0]["routes"] != scenarios[1]["routes"]:
        raise AssertionError("Чтение истории зависит от числа старых Deployment")
    return scenarios


class PatchControllerTests(unittest.TestCase):
    """Проверяет переключение и фиксацию принятой базы под контролем VPS."""

    def setUp(self):
        """Готовит отдельные Git, SQLite и одноразовый тестовый ключ."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.summary_path = self.root / "deployment-summary.json"
        self.enterContext(patch.dict(os.environ, DEPLOY_SUMMARY_PATH=str(self.summary_path)))
        graph, self.runner, fingerprint = fixture._fixture(self.root)
        self.api = SignedPatchAPI(graph, fingerprint, fixture.HISTORY_SIZE)
        self.keys = self.api.configure_signer(self.root, fingerprint)
        self.enterContext(patch.dict(os.environ, RELEASE_SIGNER_ATTEMPT="99:1", PRODUCTION_SIGNED_PATCH="true",
                                     PATCH_ALLOWED_SIGNERS_FILE=str(self.keys), COMPATIBILITY_POLICY_SHA=fixture.POLICY_SHA))
        self.enterContext(patch.object(fixture.system, "OLD_SHA", graph.base))
        self.enterContext(patch.object(fixture.system, "NEW_SHA", graph.candidate))
        self.controller = Controller(self.root / "vps", self.api)
        (self.controller.host.state_dir / "patch-allowed-signers").write_bytes(self.keys.read_bytes())

    def seed_acceptance(self):
        """Восстанавливает локальную запись закрытия предыдущего принятого PATCH."""
        base = self.api._baseline()
        receipt = self.controller._verify(self.controller.host, base.version, base.sha)
        previous = self.api.deployments[base.deployment_id]["payload"]
        record = {"protocol": 2, "closed": True, "started": True, "deployment_id": base.deployment_id,
                  "claims": ["create", "in_progress", "finish"], "attempt": previous, "owner": previous["attempt_key"],
                  "snapshot": fixture.SNAPSHOT, "base_receipt": receipt, "result": {"outcome": "success", "receipt": receipt},
                  "patch": {"terminal": base.status_id, "accepted": {"baseline": asdict(base), "receipt": receipt}}}
        self.journal_path.write_text(json.dumps(record), encoding="utf8")

    @property
    def journal_path(self):
        """Возвращает путь временному журналу контроллера."""
        return self.controller.host.state_dir / deploy_attempt.JOURNAL

    def measure(self, *, resume=False, error=None):
        """Запускает Production release с привязкой к тому же тестовому VPS."""
        return fixture._measure(self.root, "signed", self.api, self.runner, installed=resume,
                                controller=self.controller, expected_error=error)

    def test_normal_patch(self):
        """Реальный CLI подтверждает новый runtime, очистку и ID принятого Deployment."""
        self.seed_acceptance()
        self.measure()
        summary = json.loads(self.summary_path.read_bytes())
        self.assertEqual((summary["api"], summary["outcome"], summary["sha"]),
                         (15, "success", self.api.graph.candidate))
        self.assertEqual(summary["source"], summary["attempt"])
        self.assertIsNotNone(summary["installed_from"])
        self.assertIsNotNone(summary["downtime_ms"])
        journal = json.loads(self.journal_path.read_bytes())
        self.assertTrue(journal["closed"])
        self.assertEqual(journal["patch"]["accepted"]["baseline"]["status_id"], journal["patch"]["terminal"])
        self.assertEqual(self.api.status_writes, ["in_progress", "success"])
        self.assertEqual((self.controller.switches, self.controller.cleanup_count), (1, 1))
        self.assertEqual(sum(n for (loc, _, _), n in self.api.calls.items() if loc == "vps"), 4)

    def test_first_patch_after_legacy_close(self):
        """На первой подписанной выкладке подтверждает прежний success в том же бюджете."""
        self.seed_acceptance()
        previous = json.loads(self.journal_path.read_bytes())
        del previous["patch"]
        self.journal_path.write_text(json.dumps(previous), encoding="utf8")
        self.measure()
        self.assertEqual(sum(n for (loc, _, _), n in self.api.calls.items() if loc == "vps"), 7)
        self.assertEqual(self.api.posts.count("/deployments"), 1)

    def test_first_patch_without_saved_ids(self):
        """Действующая приёмка v2 подтверждается и без локальной копии D17."""
        self.measure()
        self.assertEqual(sum(n for (loc, _, _), n in self.api.calls.items() if loc == "vps"), 7)
        self.assertEqual(self.controller.switches, 1)

    def test_open_legacy_attempt_is_preserved(self):
        """Первый подписанный PATCH не вытесняет прежнего владельца."""
        self.seed_acceptance()
        old = json.loads(self.journal_path.read_bytes())
        del old["patch"]
        old["closed"] = False
        self.journal_path.write_text(json.dumps(old), encoding="utf8")
        self.measure(error="Синтетическая команда VPS остановилась")
        self.assertEqual(json.loads(self.journal_path.read_bytes()), old)
        self.assertNotIn("/deployments", self.api.posts)

    def test_lost_post_responses(self):
        """После потери ответа на каждую из трёх записей runner читает созданный объект."""
        self.seed_acceptance()
        write = self.api._write

        def lose_after_write(path, body):
            response = write(path, body)
            if path == "/deployments" or path.endswith("/statuses"):
                raise URLError("Стенд потерял ответ POST")
            return response

        with patch.object(self.api, "_write", side_effect=lose_after_write):
            self.measure()
        self.assertEqual(self.api.posts.count("/deployments"), 1)
        self.assertEqual(self.api.status_writes, ["in_progress", "success"])
        self.assertEqual((self.controller.switches, self.controller.cleanup_count), (1, 1))

    def test_resume_after_cleanup(self):
        """Один Deployment закрывается после обрыва SSH и завершения исходного владельца."""
        self.seed_acceptance()
        self.controller.lose_completion_response = True
        self.measure(error="Фикстура потеряла ответ SSH")
        interrupted = json.loads(self.summary_path.read_bytes())
        self.assertEqual(interrupted["outcome"], "unconfirmed")
        self.assertIsNotNone(interrupted["installed_from"])
        self.controller.lose_completion_response = False
        self.api.calls.clear()
        self.controller.calls.clear()
        self.measure(resume=True, error="Синтетическая команда VPS остановилась")
        self.assertIn("Не подтверждён владелец", self.controller.errors)
        self.api.source_finished = True
        self.api.calls.clear()
        self.controller.calls.clear()
        self.measure(resume=True)
        completed = json.loads(self.summary_path.read_bytes())
        self.assertEqual((completed["deployment"], completed["source"], completed["outcome"]),
                         (interrupted["deployment"], interrupted["attempt"], "success"))
        self.assertNotEqual(completed["attempt"], completed["source"])
        self.assertIsNone(completed["installed_from"])
        self.assertEqual(self.controller.calls, ["resume", "complete", "finish", "close"])
        self.assertEqual(self.api.posts.count("/deployments"), 1)
        self.assertEqual(self.api.status_writes, ["in_progress", "success"])
        self.assertEqual((self.controller.switches, self.controller.cleanup_count), (1, 1))

    def test_rollback_preserves_accepted_version(self):
        """Подтверждённый возврат закрывает D18 с failure и прежней принятой базой."""
        self.seed_acceptance()
        self.controller.restore = True
        self.measure(error="возвратом на")
        journal = json.loads(self.journal_path.read_bytes())
        self.assertTrue(journal["closed"])
        self.assertEqual(journal["patch"]["accepted"]["baseline"], asdict(self.api._baseline()))
        self.assertEqual(self.api.status_writes, ["in_progress", "failure"])
        self.assertEqual(self.controller.cleanup_count, 0)

    def test_foreign_vps_key(self):
        """VPS самостоятельно отклоняет подпись при несовпадающем локальном доверии."""
        (self.controller.host.state_dir / "patch-allowed-signers").write_text("", encoding="ascii")
        self.measure(error="Синтетическая команда VPS остановилась")
        self.assertFalse(self.journal_path.exists())
        self.assertNotIn("/deployments", self.api.posts)

    def test_unconfirmed_create_cannot_be_repeated(self):
        """Потерянная отправка без созданного D18 требует операторской сверки."""
        self.seed_acceptance()
        write = self.api._write
        sends = []

        def drop_create(path, body):
            if path == "/deployments":
                sends.append(path)
                raise URLError("Стенд прервал отправку до GitHub")
            return write(path, body)

        with patch.object(self.api, "_write", side_effect=drop_create):
            self.measure(error="требуется сверка")
            self.api.source_finished = True
            self.api.calls.clear()
            self.controller.calls.clear()
            self.measure(resume=True, error="требуется сверка")
        self.assertEqual(sends, ["/deployments"])
        self.assertEqual((self.api.status_writes, self.controller.switches), ([], 0))

    def test_resume_rejects_changed_github_status(self):
        """Новый владелец сравнивает текущий статус с состоянием после очистки."""
        self.seed_acceptance()
        self.controller.lose_completion_response = True
        self.measure(error="Фикстура потеряла ответ SSH")
        current = self.api.statuses[self.api.history_size + 1][0]
        current["state"] = "success"
        self.controller.lose_completion_response = False
        self.api.source_finished = True
        self.api.calls.clear()
        self.controller.calls.clear()
        self.measure(resume=True, error="Синтетическая команда VPS остановилась")
        self.assertIn("Терминальный статус", self.controller.errors)
        self.assertEqual((self.controller.switches, self.controller.cleanup_count), (1, 1))
        self.assertFalse(json.loads(self.journal_path.read_bytes())["closed"])

    def test_old_release_still_uses_legacy_v2_route(self):
        """Установка старого допуска использует прежний протокол после обновления контроллера."""
        graph, fingerprint = self.api.graph, self.api.proof.schema_fingerprint
        first = fixture._V2RestFixture(graph, fingerprint, fixture.HISTORY_SIZE)
        old = fixture._V2ControllerFixture(self.root / "old-publisher", first)
        with patch.dict(os.environ, PRODUCTION_SIGNED_PATCH="false", RELEASE_SIGNER_ATTEMPT=""):
            fixture._measure(self.root, "old-publish", first, self.runner, controller=old)
        ready = fixture._V2RestFixture(graph, fingerprint, fixture.HISTORY_SIZE)
        ready.seed_release(first)
        control = Controller(self.root / "old-ready", ready)
        with patch.dict(os.environ, PRODUCTION_SIGNED_PATCH="true", RELEASE_SIGNER_ATTEMPT=""):
            result = fixture._measure(self.root, "old-ready", ready, self.runner, controller=control)
        self.assertEqual(result["posts"].count("/releases"), 0)
        self.assertEqual((control.switches, control.cleanup_count), (1, 1))
        self.assertNotIn("patch", json.loads((control.host.state_dir / deploy_attempt.JOURNAL).read_bytes()))

    def test_changed_local_baseline(self):
        """Расхождение принятого локального статуса останавливает следующую попытку."""
        self.seed_acceptance()
        journal = json.loads(self.journal_path.read_bytes())
        journal["patch"]["terminal"] += 1
        self.journal_path.write_text(json.dumps(journal), encoding="utf8")
        self.measure(error="Синтетическая команда VPS остановилась")
        self.assertNotIn("/deployments", self.api.posts)
        self.assertEqual(self.controller.switches, 0)
