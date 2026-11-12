"""Проверяет оркестрацию выпуска на локальных моделях GitHub и VPS."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import release_production as production
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError
from scripts.select_deploy_release import SelectedRelease
from tests.test_deployment_acceptance import RECEIPT, SHA, VERSION
from tests.test_publish_deployment_acceptance import WritableGitHub


_RELEASE = SelectedRelease(production.EXPECTED_REPOSITORY, VERSION, "release/3.4", SHA,
                           RECEIPT.release_id, "release-full.yml", RECEIPT.run_id, RECEIPT.run_attempt)
_CLEANUP = {"version": 1, "transition_id": "a" * 32, "old_generation_id": "legacy",
            "old_host_path": "/private/data/app.db", "old_device": 1, "old_inode": 2,
            "new_generation_id": RECEIPT.generation_id}
_DEPLOY = ("sudo", "-n", "/usr/local/sbin/deploy-prod", VERSION)
_VERIFY = _DEPLOY + ("--verify-installed", SHA)
_REMOVE = _DEPLOY + ("--cleanup-old", SHA, RECEIPT.runtime_id)


class ProductionReleaseTests(unittest.TestCase):
    """Проверяет порядок установки, удаления и записи через CLI."""

    def setUp(self) -> None:
        """Подставляет модели API и SSH перед запуском основной функции."""
        self.api = WritableGitHub()
        self.notes = Path(self.enterContext(TemporaryDirectory())) / "release notes.txt"
        self.notes.write_text("Исправление", encoding="utf-8")
        self.selected = self.enterContext(patch.object(production, "select_release", return_value=_RELEASE))
        self.prepare = self.enterContext(patch.object(production.releases, "prepare"))
        self.publish = self.enterContext(patch.object(production.releases, "publish"))
        self.enterContext(patch.object(production, "repository_from_origin", return_value=self.api.repository))
        self.client = self.enterContext(patch.object(production, "PublishingGitHub", return_value=self.api))
        self.enterContext(patch.dict(os.environ, {"GITHUB_TOKEN": "production-test-token"}))
        self.remote: list[tuple[str, ...]] = []
        self.ssh = self.enterContext(patch.object(production, "run_vps_command", side_effect=self._vps))

    def _vps(self, args, *, capture_output, **options):
        """Возвращает машинные данные установки, приёмки и очистки."""
        self.assertTrue(capture_output)
        self.remote.append(args)
        if args == _DEPLOY:
            return subprocess.CompletedProcess(args, 0, stdout=RECEIPT.runtime_id)
        if args == _VERIFY:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(asdict(RECEIPT)))
        self.assertEqual(args, _REMOVE)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(_CLEANUP))

    def _run(self, *flags, error=None, version=VERSION, sha=SHA):
        """Запускает CLI и сверяет итог, сохраняя выход закрытых операций."""
        stdout, stderr = StringIO(), StringIO()
        argv = ["release_production", version, sha, "--ssh-host", "operator@vps.example.invalid",
                "--known-hosts", str(self.notes), *flags]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            if error is None:
                production.main()
                self.assertEqual(json.loads(stdout.getvalue().splitlines()[-1]),
                                 {"deployment_id": 7, "status_id": 8, "state": "success",
                                  "cleanup": "completed"})
            else:
                with self.assertRaises(SystemExit) as raised:
                    production.main()
                self.assertEqual(raised.exception.code, 1)
                self.assertIn(error, stderr.getvalue())
        self.assertNotIn(RECEIPT.runtime_id, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(_CLEANUP["old_host_path"], stdout.getvalue() + stderr.getvalue())

    def _release(self, *, error=None, version=VERSION, sha=SHA):
        """Запускает CLI с параметрами обычной публикации."""
        self._run("--release", "--change", "patch", "--notes-file", str(self.notes),
                  error=error, version=version, sha=sha)

    def test_creates_missing_release_before_install_and_cleanup(self) -> None:
        """Только 404 открывает публикацию, после которой читается точный Release."""
        original_get = self.api.get
        missing = True

        def get(path, **options):
            nonlocal missing
            if path == f"/releases/tags/{VERSION}" and missing:
                missing = False
                raise ControllerError("Release отсутствует") from HTTPError(path, 404, "Not found", None, None)
            return original_get(path, **options)

        with patch.object(self.api, "get", side_effect=get):
            self._release()
        self.prepare.assert_called_once_with(self.api, VERSION, SHA, "patch", "Исправление")
        self.publish.assert_called_once_with(self.api, self.prepare.return_value)
        self.client.assert_called_once_with(self.api.repository, "production-test-token")
        self.assertEqual([path for path, _ in self.api.posts], ["/deployments", "/deployments/7/statuses"])
        self.assertEqual(self.remote[:4], [_DEPLOY, _VERIFY, _REMOVE, _VERIFY])

    def test_reuses_exact_release_and_resumes_without_installing(self) -> None:
        """Точный Release и уже записанный success повторно не создаются."""
        self._release()
        self.remote.clear()
        self._run("--resume")
        self.prepare.assert_not_called()
        self.publish.assert_not_called()
        self.assertNotIn(_DEPLOY, self.remote)
        self.assertEqual(self.remote[:3], [_VERIFY, _REMOVE, _VERIFY])
        self.assertEqual(len(self.api.posts), 2)

    def test_fails_closed_on_api_error_or_release_mismatch(self) -> None:
        """403 и чужой SHA останавливают выпуск до публикации и SSH."""
        request = f"/releases/tags/{VERSION}"
        failure = ControllerError("GitHub 403")
        failure.__cause__ = HTTPError(request, 403, "Forbidden", None, None)
        with patch.object(self.api, "get", side_effect=failure):
            self._release(error="GitHub 403")
        self.selected.return_value = replace(_RELEASE, sha="8" * 40)
        self._release(error="другому SHA")
        self.prepare.assert_not_called()
        self.publish.assert_not_called()
        self.ssh.assert_not_called()

    def test_invalid_request_or_untrusted_author_stops_before_mutation(self) -> None:
        """Исторический выпуск, неверный SHA и чужой токен закрывают запись."""
        self._release(version="v3.2.0", error="Исторический v3.2.0")
        self._release(sha="short", error="полный 40-символьный SHA")
        self.api.actor = {"id": 1, "login": "other"}
        self._release(error="доверенный владелец")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.posts, [])
        self.ssh.assert_not_called()

    def test_finishes_cleanup_before_github_failure_and_resumes(self) -> None:
        """Отказ GitHub при записи не блокирует очистку завершённой установки."""
        original_post = self.api.post

        def post(path, body):
            self.assertIn(_REMOVE, self.remote)
            self.assertEqual(self.remote[self.remote.index(_REMOVE) + 1], _VERIFY)
            return original_post(path, body)

        with patch.object(self.api, "post", side_effect=post):
            self.api.lose_status = True
            self.api.apply_status = False
            self._release(error="Очистка SQLite подтверждена, Deployment не подтверждён")
            self.assertEqual(self.remote[:4], [_DEPLOY, _VERIFY, _REMOVE, _VERIFY])
            self.api.lose_status = False
            self.api.apply_status = True
            self.remote.clear()
            self._run("--resume")
        self.assertNotIn(_DEPLOY, self.remote)
        self.assertEqual([path for path, _ in self.api.posts],
                         ["/deployments", "/deployments/7/statuses", "/deployments/7/statuses"])

    def test_github_authentication_failure_after_cleanup_points_to_resume(self) -> None:
        """Отказ чтения автора после очистки предлагает повтор без установки."""
        authenticated_user = self.api.authenticated_user

        def github_user():
            if _REMOVE in self.remote:
                raise PublishingError("GitHub недоступен")
            return authenticated_user()

        with patch.object(self.api, "authenticated_user", side_effect=github_user):
            self._release(error="Очистка SQLite подтверждена, Deployment не подтверждён. Для записи повторите --resume.")
        self.assertEqual(self.remote, [_DEPLOY, _VERIFY, _REMOVE])
        self.assertEqual(self.api.posts, [])

    def test_refuses_changed_runtime_after_cleanup(self) -> None:
        """Смена runtime после очистки блокирует запись в GitHub."""
        original = self.ssh.side_effect

        def changed_runtime(args, **options):
            result = original(args, **options)
            if args == _VERIFY and _REMOVE in self.remote:
                changed = replace(RECEIPT, runtime_id="8" * 64)
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(asdict(changed)))
            return result

        self.ssh.side_effect = changed_runtime
        self._release(error="Очистка SQLite подтверждена, Deployment не подтверждён")
        self.assertEqual(self.remote, [_DEPLOY, _VERIFY, _REMOVE, _VERIFY])
        self.assertEqual(self.api.posts, [])

    def test_rejects_cleanup_record_for_another_generation(self) -> None:
        """Ответ об очистке должен относиться к тому же принятому поколению."""
        original = self.ssh.side_effect

        def wrong_cleanup(args, **options):
            result = original(args, **options)
            if args == _REMOVE:
                wrong = _CLEANUP | {"new_generation_id": "8" * 32}
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(wrong))
            return result

        self.ssh.side_effect = wrong_cleanup
        self._release(error="Очистка SQLite не подтверждена")
        self.assertEqual(self.remote[-1], _REMOVE)
        self.assertEqual(self.api.posts, [])


if __name__ == "__main__":
    unittest.main()
