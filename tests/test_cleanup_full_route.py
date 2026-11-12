"""Проверяет локальный путь от перехода SQLite до следующего выпуска."""

from contextlib import closing, contextmanager, nullcontext, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from scripts import deploy_acceptance as acceptance
from scripts import deploy_cleanup_old as cleanup
from scripts import deploy_database_switch as switch
from scripts import deploy_forward_recovery as forward
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deploy_runtime as runtime
from scripts import publish_deployment_acceptance as publisher
from scripts import release_production as production
from scripts.publish_installation_tag import InstallationReceipt
from tests import test_deployment_acceptance as deployment_fixture
from tests import test_publish_deployment_acceptance as publishing_fixture
from tests.test_deploy_acceptance import SHA, TAG_OBJECT, VERSION
from tests import test_deploy_cleanup_contract as contract_fixture
from tests.test_deploy_database_switch import IMAGE
from tests.test_deploy_forward_recovery import ForwardHost


MANIFEST = contract_fixture.MANIFEST


class ReceiptForwardHost(ForwardHost):
    """Добавляет к fake transport признаки для приёмки и актуальную SQLite."""

    def run(self, *args: str, env=None) -> str:
        """Возвращает чистый detached checkout только для технической приёмки."""
        if args == ("git", "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args == ("git", "rev-parse", "HEAD"):
            return SHA
        if args == ("git", "rev-parse", "--abbrev-ref", "HEAD"):
            return "HEAD"
        return super().run(*args, env=env)

    def migrate_copy(self, image_id: str, directory: Path, *, basename: str = "app.db") -> None:
        """Проверяет копию с текущими миграциями, не меняя её."""
        with closing(sqlite3.connect(directory / basename)) as connection:
            if connection.execute("SELECT value FROM cleanup_probe").fetchall() != [("before",)]:
                raise AssertionError("Копия потеряла запись")


class CleanupFullRouteTests(unittest.TestCase):
    """Испытывает приёмку, очистку, публикацию и следующий selector rebind."""

    def setUp(self) -> None:
        """Изолирует `/proc` gate в проверках сценариев перехода SQLite."""
        fd_gate = patch.object(cleanup, "_require_no_open_old")
        fd_gate.start()
        self.addCleanup(fd_gate.stop)

    def _forward_fixture(self, root: Path) -> ReceiptForwardHost:
        """Строит degraded финализацию на отдельной SQLite с миграциями."""
        host = ReceiptForwardHost(root)
        database = host.checkout / "data/app.db"
        database.unlink()
        with closing(sqlite3.connect(database)) as connection:
            apply_migrations(connection)
            connection.execute("CREATE TABLE cleanup_probe(id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO cleanup_probe VALUES(1, 'before')")
            connection.commit()
        old = switch.identity(database, "legacy", MANIFEST)
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST,
                 "controller_sha": "c" * 40, "old_sha": SHA, "old_image_id": IMAGE,
                 "old_tag": prep.pinned_tag(SHA),
                 "candidate": {"sha": SHA, "image_id": IMAGE, "tag": prep.pinned_tag(SHA),
                               "version": VERSION, "release_id": 41, "run_id": 107,
                               "run_attempt": 2, "tag_object_sha": TAG_OBJECT, "kind": "ordinary"},
                 "timer_before": {"enabled": False, "active": False},
                 "last_good": {"version": "v3.2.0", "sha": SHA,
                               "image_id": IMAGE, "tag": prep.pinned_tag(SHA)},
                 "compatibility_evidence": {"schema_fingerprint": old["fingerprint"]}}

        def record(stage: str, **changes: object) -> None:
            """Сохраняет каждую стадию настоящим atomic writer."""
            state.update(changes, stage=stage)
            prep.write_state(host.state_dir, host.checkout, state)

        switch.register_initial(host, state, record, prod._atomic_text)
        switch.transition(host, state, record, prod._atomic_text)
        for item in host.found.values():
            item["State"] = {"Running": False, "Status": "exited"}
        state.update(stage="degraded", migration_attempted=False,
                     rollback_origin_stage="finalization_intent",
                     boot=runtime.boot_record(state, "success"))
        prep.write_state(host.state_dir, host.checkout, state)
        host.main_failed = True
        with (patch.object(prep, "deploy_lock", return_value=nullcontext()),
              patch.object(prep, "require_clean_checkout")):
            forward.recover(host)
        for item in host.found.values():
            item["State"].update(StartedAt="2026-11-09T12:00:00Z", Health={"Status": "healthy"})
        return host

    def _receipt(self, host) -> dict:
        """Получает свежую техническую приёмку без внешнего Git и lock."""
        with (patch.object(acceptance, "_read_lock", return_value=nullcontext()),
              patch.object(prep, "require_exact_tag")):
            return acceptance.verify_installed(host, VERSION, SHA)

    def _cleanup(self, host, runtime_id: str) -> dict:
        """Выполняет серверную очистку для точной технической приёмки."""
        with (patch.object(prep, "deploy_lock", return_value=nullcontext()),
              patch.object(prep, "require_exact_tag")):
            return cleanup.cleanup_old(host, VERSION, SHA, runtime_id)

    @contextmanager
    def _github(self, host):
        """Предоставляет локальную модель GitHub для текущего выпуска и SQLite."""
        receipt = InstallationReceipt(**self._receipt(host))
        api = publishing_fixture.WritableGitHub()
        original_get = api.get

        def get(path: str, **parameters):
            """Подменяет только Release, CI и миграции для данного принятого SHA."""
            if path == f"/releases/tags/{VERSION}":
                result = original_get(f"/releases/tags/{deployment_fixture.VERSION}", **parameters)
                if receipt.kind == "emergency":
                    body = (f"Тип выпуска: аварийный\nCommit: `{SHA}`\nВетка: `release/3.3`\n"
                            "Причина: срочный сбой\nПолный CI: main-full.yml success — "
                            f"https://github.com/{api.repository}/actions/runs/107/attempts/2\n"
                            "PR и состояние на момент merge:\n"
                            f"- PR #42 -> main; merge `{SHA}` (2026-10-15T00:00:00Z); "
                            "pr-check=success [GitHub Actions]; bypass=yes\n"
                            "Все наблюдаемые проверки успешны; аварийный допуск всё равно явный.\n")
                else:
                    body = (f"Тип выпуска: обычный\nCommit: `{SHA}`\n"
                            "Ветка: `release/3.3`\nПолный CI: "
                            f"https://github.com/{api.repository}/actions/runs/107/attempts/2")
                return dict(result, tag_name=VERSION, name=VERSION, target_commitish=SHA,
                            body=body)
            if path == f"/git/ref/tags/{VERSION}" and receipt.kind == "emergency":
                return {"ref": f"refs/tags/{VERSION}", "object": {"type": "commit", "sha": SHA}}
            if path == "/actions/runs/107/attempts/2":
                return {"id": 107, "run_attempt": 2, "head_sha": SHA,
                        "status": "completed", "conclusion": "success", "html_url": api.run_url}
            if path == f"/contents/{deployment_fixture.MIGRATIONS_PREFIX.rstrip('/')}":
                return [{"name": name, "path": deployment_fixture.MIGRATIONS_PREFIX + name,
                         "type": "file"} for name in MANIFEST]
            return original_get(path, **parameters)

        with (patch.object(deployment_fixture, "VERSION", VERSION),
              patch.object(deployment_fixture, "SHA", SHA),
              patch.object(deployment_fixture, "RECEIPT", receipt),
              patch.object(publishing_fixture, "SHA", SHA),
              patch.object(api, "get", side_effect=get)):
            yield api, receipt

    def _publish(self, host) -> None:
        """Публикует только в памяти fake GitHub с повторной приёмкой SQLite."""
        with (self._github(host) as (api, receipt),
              patch.object(publisher, "repository_from_origin", return_value=api.repository),
              patch.object(publisher, "PublishingGitHub", return_value=api),
              patch.object(publisher, "run_vps_command") as deploy,
              patch.object(publisher, "read_receipt", side_effect=lambda *args, **kwargs:
                           InstallationReceipt(**self._receipt(host))) as read_receipt,
              patch.object(sys, "argv", ["publish_deployment_acceptance", VERSION, SHA, "--publish",
                                         "--ssh-host", "operator@vps.example.invalid",
                                         "--known-hosts", "unused-by-local-vps-fixture"])):
            with redirect_stdout(StringIO()) as output:
                publisher.main()
            record = json.loads(output.getvalue())
            self.assertEqual(record, {"deployment_id": 7, "status_id": 8, "state": "success"})
            self.assertEqual([path for path, _ in api.posts],
                             ["/deployments", "/deployments/7/statuses"])
            self.assertEqual(api.posts[0][1]["ref"], SHA)
            self.assertEqual(api.posts[0][1]["payload"],
                             {key: value for key, value in asdict(receipt).items() if key != "runtime_id"})
            with redirect_stdout(StringIO()) as repeated_output:
                publisher.main()
            self.assertEqual(json.loads(repeated_output.getvalue()), record)
            self.assertEqual(len(api.posts), 2)
            self.assertEqual(read_receipt.call_count, 5)
            deploy.assert_not_called()

    def test_production_resume_cleans_old_sqlite_before_finishing_deployment(self) -> None:
        """Проверяет очистку до публикации с реальной временной SQLite."""
        with TemporaryDirectory() as temporary:
            host, state = contract_fixture.CleanupContractTests()._fixture(Path(temporary).resolve())
            old = Path(state["old"]["host_path"])
            active = Path(state["new"]["host_path"])
            backup = Path(state["snapshot"]["path"])
            selector = host.state_dir / "active-db.json"
            protected = {path: path.read_bytes() for path in (active, backup, selector)}
            old_sidecar = Path(str(old) + "-shm")
            old_sidecar.write_bytes(b"closed sidecar")
            command = ("sudo", "-n", "/usr/local/sbin/deploy-prod", VERSION)
            remote_actions = []

            with self._github(host) as (api, receipt):
                def run_remote(args, **options):
                    """Связывает модель SSH со штатной приёмкой и удалением на диске."""
                    if args == command + ("--verify-installed", SHA):
                        remote_actions.append("verify")
                        result = self._receipt(host)
                    else:
                        self.assertEqual(args, command + ("--cleanup-old", SHA, receipt.runtime_id))
                        remote_actions.append("cleanup")
                        result = self._cleanup(host, receipt.runtime_id)
                        # Модель SSH передаёт формат пути Linux и проверяет файлы локально.
                        result = result | {"old_host_path": "/opt/obs-chat-bot/data/" + old.name}
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps(result))

                original_post = api.post

                def post_after_cleanup(path, body):
                    """Проверяет состояние файлов при записи в GitHub."""
                    self.assertFalse(old.exists())
                    self.assertFalse(old_sidecar.exists())
                    self.assertEqual({path: path.read_bytes() for path in protected}, protected)
                    return original_post(path, body)

                selected = production.SelectedRelease(api.repository, VERSION, "release/3.3", SHA,
                                                      receipt.release_id, "main-full.yml", receipt.run_id,
                                                      receipt.run_attempt)
                argv = ["release_production", VERSION, SHA, "--resume", "--ssh-host", "runner@vps.invalid",
                        "--known-hosts", "unused-by-local-vps-fixture"]
                with (patch.object(production, "repository_from_origin", return_value=api.repository),
                      patch.object(production, "PublishingGitHub", return_value=api),
                      patch.object(production, "select_release", return_value=selected),
                      patch.object(production, "run_vps_command", side_effect=run_remote),
                      patch.object(api, "post", side_effect=post_after_cleanup),
                      patch.object(sys, "argv", argv)):
                    for _ in range(2):
                        with redirect_stdout(StringIO()) as output:
                            production.main()
                        self.assertEqual(json.loads(output.getvalue().splitlines()[-1]),
                                         {"deployment_id": 7, "status_id": 8, "state": "success",
                                          "cleanup": "completed"})
                        self.assertNotIn(receipt.runtime_id, output.getvalue())
                self.assertEqual([path for path, _ in api.posts], ["/deployments", "/deployments/7/statuses"])

            self.assertEqual(remote_actions.count("cleanup"), 2)
            self.assertEqual(remote_actions[:3], ["verify", "cleanup", "verify"])
            self.assertFalse(old.exists())
            self.assertFalse(old_sidecar.exists())
            self.assertEqual({path: path.read_bytes() for path in protected}, protected)
            self.assertEqual(self._receipt(host), asdict(receipt))

    def test_ordinary_emergency_and_forward_route(self) -> None:
        """Все пути сохраняют runtime, новую БД и rebind следующего перехода."""
        for route in ("ordinary", "emergency", "forward"):
            with self.subTest(route=route), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                if route == "forward":
                    host = self._forward_fixture(root)
                else:
                    host, state = contract_fixture.CleanupContractTests()._fixture(
                        root, kind="emergency" if route == "emergency" else "ordinary")
                state = prep.read_state(host.state_dir, host.checkout)
                self.assertEqual(state["stage"], "success")
                old = Path(state["old"]["host_path"])
                active = Path(state["new"]["host_path"])
                backup = Path(state["snapshot"]["path"])
                selector_path = host.state_dir / "active-db.json"
                old_sidecar = Path(str(old) + "-shm")
                old_sidecar.write_bytes(b"closed sidecar")
                protected = {path: path.read_bytes() for path in (active, backup, selector_path)}
                receipt_before = self._receipt(host)
                self.assertEqual(receipt_before["kind"], "emergency" if route == "emergency" else "ordinary")
                self.assertEqual(switch.read_selected(host, state), "new")
                with self.assertRaises(cleanup.CleanupError):
                    self._cleanup(host, "f" * 64)
                self.assertTrue(old.is_file())
                self._cleanup(host, receipt_before["runtime_id"])
                self.assertEqual(sum(not path.exists() for path in (old, old_sidecar)), 2)
                self.assertEqual({path: path.read_bytes() for path in protected}, protected)
                self.assertEqual(self._receipt(host), receipt_before)
                self._cleanup(host, receipt_before["runtime_id"])
                self._publish(host)
                durable = prep.read_state(host.state_dir, host.checkout)
                prod._recover(host, durable)
                self.assertEqual(switch.read_selected(host, durable), "new")
                with self.assertRaises((ValueError, FileNotFoundError)):
                    prod._atomic_text(selector_path, json.dumps(switch.selector(durable, "old")))
                    switch.read_selected(host, durable)
                prod._atomic_text(selector_path, json.dumps(switch.selector(durable, "new")))
                side = switch.read_selected(host, durable)
                next_state = {"protocol_version": switch.PROTOCOL_VERSION,
                              "transition_id": "f" * 32, "old": durable[side]}
                next_state["selector_rebind"] = {
                    "from": switch.selector(durable, side),
                    "to": switch.selector(next_state, "old"),
                }
                self.assertEqual(next_state["old"]["host_path"], str(active))
                self.assertEqual(next_state["selector_rebind"]["from"], switch.selector(durable, "new"))
                self.assertTrue(active.is_file())
                self.assertTrue(backup.is_file())
                with closing(sqlite3.connect(active)) as connection:
                    self.assertEqual(connection.execute("SELECT value FROM cleanup_probe").fetchall(),
                                     [("before",)])


if __name__ == "__main__":
    unittest.main()
