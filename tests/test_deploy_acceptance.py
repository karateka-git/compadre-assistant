"""Проверяет техническую приёмку с настоящими файлами SQLite и selector."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import deploy_acceptance as acceptance
from scripts import deploy_database_switch as switch
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import deploy_runtime as runtime
from tests.test_deploy_database_switch import IMAGE, MANIFEST, SHA, SwitchHost


VERSION = "v3.3.0"
TAG_OBJECT = "d" * 40


class AcceptanceHost(SwitchHost):
    """Имитирует читаемые системные признаки на временной базе SQLite."""

    def run(self, *args, env=None):
        """Возвращает признаки чистого detached checkout на точном SHA."""
        if args == ("git", "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args == ("git", "rev-parse", "HEAD"):
            return SHA
        if args == ("git", "rev-parse", "--abbrev-ref", "HEAD"):
            return "HEAD"
        return super().run(*args, env=env)

    def image(self, reference):
        """Подтверждает локальный pin и OCI ревизию."""
        return {"Id": IMAGE, "Config": {"Labels": {prep.OCI_REVISION: SHA}}}

    def unit_active(self, unit):
        """Отражает активный основной сервис и отключённый таймер."""
        return unit == prod.SERVICE

class AcceptanceTests(unittest.TestCase):
    """Проверяет результат приёмки и отказ при drift без изменения VPS."""

    def _setup(self, root):
        host = AcceptanceHost(root)
        old = switch.identity(host.checkout / "data/app.db", "legacy", MANIFEST)
        state = {"schema_before": MANIFEST, "schema_after": MANIFEST, "controller_sha": "c" * 40,
                 "old_sha": "a" * 40, "old_image_id": "sha256:" + "1" * 64, "old_tag": "old-pin",
                 "compatibility_evidence": {"schema_fingerprint": old["fingerprint"]},
                 "timer_before": {"enabled": False, "active": False},
                 "candidate": {"image_id": IMAGE, "sha": SHA, "tag": prep.pinned_tag(SHA),
                               "version": VERSION, "release_id": 41, "run_id": 107, "run_attempt": 2,
                               "tag_object_sha": TAG_OBJECT, "kind": "ordinary"}}

        def record(stage, **changes):
            state.update(changes, stage=stage)
            prep.write_state(host.state_dir, host.checkout, state)

        switch.register_initial(host, state, record, prod._atomic_text)
        switch.transition(host, state, record, prod._atomic_text)
        for item in host.found.values():
            item["State"].update(StartedAt="2026-11-09T12:00:00Z", Health={"Status": "healthy"})
        boot = runtime.boot_record(state, "success")
        host.marker(False)
        record("success", boot=boot, last_good={
            "version": VERSION, "sha": SHA, "image_id": IMAGE, "tag": prep.pinned_tag(SHA),
            "ci_run_id": 107, "ci_run_attempt": 2, "kind": "ordinary",
        })
        host.commands.clear()
        return host, state

    def _verify(self, host, sha=SHA):
        with (patch.object(acceptance, "_read_lock", return_value=nullcontext()),
              patch.object(prep, "require_exact_tag") as tag):
            result = acceptance.verify_installed(host, VERSION, sha)
            tag.assert_called_once_with(host.checkout, VERSION, sha, tag_object_sha=TAG_OBJECT)
            return result

    def test_acceptance_reads_v2_database_and_produces_stable_receipt(self):
        """Снимок связывает Release с БД и парой процессов, не меняя файлы."""
        with TemporaryDirectory() as temporary:
            host, state = self._setup(Path(temporary).resolve())
            paths = [host.state_dir / "state.json", host.state_dir / "active-db.json",
                     Path(state["old"]["host_path"]), Path(state["new"]["host_path"])]
            before = [path.read_bytes() for path in paths]
            receipt = self._verify(host)
            self.assertEqual(receipt, {
                "schema_version": 1, "version": VERSION, "sha": SHA, "kind": "ordinary",
                "release_id": 41, "run_id": 107, "run_attempt": 2,
                "generation_id": state["new"]["generation_id"], "migration": MANIFEST[-1],
                "runtime_id": receipt["runtime_id"],
            })
            self.assertRegex(receipt["runtime_id"], r"^[0-9a-f]{64}$")
            self.assertEqual(receipt, self._verify(host))
            self.assertEqual([path.read_bytes() for path in paths], before)
            self.assertEqual(host.commands, [])
            self.assertNotIn("original", json.dumps(receipt))

    def test_acceptance_rejects_release_timer_or_marker_mismatch(self):
        """Отказ не создаёт selector или state и не запускает восстановление."""
        with TemporaryDirectory() as temporary:
            host, _ = self._setup(Path(temporary).resolve())
            state = host.state_dir / "state.json"
            before = state.read_bytes()
            with self.assertRaisesRegex(prod.DeployError, "Release, SHA"):
                self._verify(host, "a" * 40)
            with patch.object(host, "timer_snapshot", return_value={"enabled": True, "active": False}):
                with self.assertRaisesRegex(prod.DeployError, "таймер"):
                    self._verify(host)
            host.marker(True)
            with self.assertRaisesRegex(prod.DeployError, "маркер"):
                self._verify(host)
            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(host.commands, [])

    def test_acceptance_rejects_sqlite_and_runtime_drift(self):
        """Проверка обнаруживает изменение активной схемы и рестарт между осмотрами."""
        with TemporaryDirectory() as temporary:
            host, state = self._setup(Path(temporary).resolve())

            def restart_between_reads():
                host.found[prep.CHANNELS[0]]["State"]["StartedAt"] = "2026-11-09T12:01:00Z"
                return {"enabled": False, "active": False}

            with patch.object(host, "timer_snapshot", side_effect=restart_between_reads):
                with self.assertRaisesRegex(prod.DeployError, "устойчивость"):
                    self._verify(host)
            database = sqlite3.connect(state["new"]["host_path"])
            try:
                database.execute("CREATE TABLE unexpected (id INTEGER PRIMARY KEY)")
                database.commit()
            finally:
                database.close()
            with self.assertRaisesRegex(ValueError, "Идентичность поколения"):
                self._verify(host)
            self.assertEqual(host.commands, [])

    def test_acceptance_supports_legacy_sqlite(self):
        """Штатная legacy SQLite проверяется без selector и смены runtime."""
        with TemporaryDirectory() as temporary:
            host, state = self._setup(Path(temporary).resolve())
            (host.state_dir / "active-db.json").unlink()
            state.pop("protocol_version")
            state["candidate"].pop("tag_object_sha")
            prep.write_state(host.state_dir, host.checkout, state)
            for item in host.found.values():
                item["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
                item["Config"]["Env"] = ["DATABASE_PATH=/app/data/app.db"]
            database = host.checkout / "data/app.db"
            before = database.read_bytes()
            with (patch.object(acceptance, "_read_lock", return_value=nullcontext()),
                  patch.object(prep, "require_exact_tag") as tag,
                  patch.object(host, "runtime_tag", return_value=prep.pinned_tag(SHA))):
                receipt = acceptance.verify_installed(host, VERSION, SHA)
                tag.assert_called_once_with(host.checkout, VERSION, SHA, tag_object_sha=None)
                host.found[prep.CHANNELS[0]]["Config"]["Env"] = ["DATABASE_PATH=/app/data/other.db"]
                with self.assertRaisesRegex(prod.DeployError, "legacy mount"):
                    acceptance.verify_installed(host, VERSION, SHA)
            self.assertEqual(receipt["generation_id"], "legacy")
            self.assertEqual(receipt["migration"], MANIFEST[-1])
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((host.state_dir / "active-db.json").exists())
            self.assertEqual(host.commands, [])

    def test_cli_automatically_verifies_new_installation_and_supports_read_only_repeat(self):
        """CLI передаёт точный SHA установки, а отдельный повтор только читает."""
        host = object()
        receipt = {"sha": SHA}
        with (patch.object(prod, "os", SimpleNamespace(name="posix", geteuid=lambda: 0)),
              patch.object(prod, "Host", return_value=host),
              patch.object(prod, "deploy", return_value=SHA) as deploy,
              patch.object(acceptance, "verify_installed", return_value=receipt) as verify):
            automatic = StringIO()
            with patch.object(prod.sys, "argv", ["deploy-prod", VERSION]), redirect_stdout(automatic):
                prod.main()
            deploy.assert_called_once_with(host, VERSION, emergency=False, reason=None)
            verify.assert_called_once_with(host, VERSION, SHA)
            self.assertEqual(automatic.getvalue(), f'deploy-prod technical_acceptance={{"sha":"{SHA}"}}\n')
            deploy.reset_mock()
            verify.reset_mock()
            machine = StringIO()
            with (patch.object(prod.sys, "argv", ["deploy-prod", VERSION, "--verify-installed", SHA]),
                  redirect_stdout(machine)):
                prod.main()
            deploy.assert_not_called()
            verify.assert_called_once_with(host, VERSION, SHA)
            self.assertEqual(json.loads(machine.getvalue()), receipt)

    def test_cli_reports_acceptance_failure_separately_from_installation(self):
        """Ошибку из отдельного модуля выводит без traceback или повторной установки."""
        class ExternalDeployError(Exception):
            """Моделирует другой экземпляр DeployError при запуске через python -m."""

        with (patch.object(prod, "os", SimpleNamespace(name="posix", geteuid=lambda: 0)),
              patch.object(prod, "Host"),
              patch.object(prod, "deploy", return_value=SHA) as deploy,
              patch.object(acceptance, "DeployError", ExternalDeployError),
              patch.object(acceptance, "verify_installed", side_effect=ExternalDeployError("таймер")),
              patch.object(prod.sys, "argv", ["deploy-prod", VERSION]),
              redirect_stderr(StringIO()) as errors, redirect_stdout(StringIO()) as output):
            with self.assertRaises(SystemExit) as exit_status:
                prod.main()
            self.assertEqual(exit_status.exception.code, 1)
            deploy.assert_called_once()
            self.assertIn("Техническая приёмка не пройдена (таймер)", errors.getvalue())
            self.assertEqual(output.getvalue(), "")
