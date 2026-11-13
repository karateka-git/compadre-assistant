"""Проверяет команду установки и публикации на локальных моделях SSH и GitHub."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import publish_deployment_acceptance as publisher
from scripts.deployment_acceptance import DeploymentAcceptanceError, require_accepted_deployment
from tests.test_deployment_acceptance import OWNER, RECEIPT, SHA, VERSION
from tests.test_publish_deployment_acceptance import WritableGitHub


HOST = "operator@vps.example.invalid"
REASON = "Сбой 'worker'; $(echo test) \"urgent\""
EMERGENCY = ("--emergency", f"--reason={REASON}")
POSTS = ["/deployments", "/deployments/7/statuses"]
TOKENS = {name: f"fixture-{name}" for name in (
    "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
)}


class DeployAndPublishTests(unittest.TestCase):
    """Запускает CLI с реальной сборкой команды SSH, подменяя только внешние вызовы."""

    def _invoke(self, api, run_ssh, *flags, error=None, sha=SHA):
        """Выполняет CLI и проверяет результат или ожидаемую диагностику."""
        stdout, stderr = StringIO(), StringIO()
        with TemporaryDirectory() as temporary:
            known_hosts = Path(temporary).resolve() / "known hosts"
            known_hosts.touch()
            argv = ["publish_deployment_acceptance", VERSION, sha,
                    "--ssh-host", HOST, "--known-hosts", str(known_hosts), *flags]
            with (patch.object(sys, "argv", argv), patch.dict(os.environ, TOKENS),
                  patch.object(publisher, "repository_from_origin", return_value=api.repository),
                  patch.object(publisher, "PublishingGitHub", return_value=api) as github,
                  patch("scripts.publish_installation_tag.subprocess.run", side_effect=run_ssh) as ssh,
                  redirect_stdout(stdout), redirect_stderr(stderr)):
                if error is None:
                    publisher.main()
                    self.assertEqual(json.loads(stdout.getvalue()),
                                     {"deployment_id": 7, "status_id": 8, "state": "success"})
                else:
                    with self.assertRaises(SystemExit) as raised:
                        publisher.main()
                    self.assertEqual(raised.exception.code, 1)
                    self.assertIn(error, stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), "")
                github.assert_called_once_with(api.repository, TOKENS["GITHUB_TOKEN"])
                for call in ssh.call_args_list:
                    command = call.args[0]
                    self.assertIn(f'UserKnownHostsFile="{known_hosts.as_posix()}"', command)
                    self.assertIn("StrictHostKeyChecking=yes", command)
                    self.assertIn("ForwardAgent=no", command)
                    self.assertEqual(command[:3], ["ssh", "-F", "none"])
                    self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)
                    self.assertTrue(call.kwargs["check"])
                    self.assertTrue(TOKENS.keys().isdisjoint(call.kwargs["env"]))
                    for token in TOKENS.values():
                        self.assertNotIn(token, " ".join(command))
                        self.assertNotIn(token, call.kwargs["env"].values())
                return ssh.call_count

    def _run_ssh(self, api, receipts, *deploy_flags):
        """Имитирует POSIX-разбор SSH и возвращает заданные снимки VPS."""
        remote_commands = []

        def run(command, *, capture_output, timeout, **kwargs):
            separator = command.index("--")
            self.assertEqual(command[separator + 1], HOST)
            remote = shlex.split(" ".join(command[separator + 2:]))
            remote_commands.append(remote)
            if not capture_output:
                self.assertEqual(remote, ["sudo", "-n", "deploy-prod", VERSION, *deploy_flags])
                self.assertEqual(timeout, 3600)
                self.assertIn(f"/releases/tags/{VERSION}", [path for path, _ in api.calls])
                self.assertIn(f"/git/ref/tags/{VERSION}", [path for path, _ in api.calls])
                self.assertEqual(api.posts, [])
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(remote, ["sudo", "-n", "deploy-prod", VERSION, "--verify-installed", SHA])
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(asdict(next(receipts))))

        return run, remote_commands

    def _emergency_api(self, *, ci_conclusion="success", **run_overrides):
        """Подставляет аварийный Release с lightweight-тегом и заданную попытку CI."""
        api = WritableGitHub()
        original_get = api.get
        ci_url = api.run_url + "/attempts/2"
        summary = ("Все наблюдаемые проверки успешны; аварийный допуск всё равно явный."
                   if ci_conclusion == "success" else "Неподтверждённые или неуспешные проверки:\n- Полный CI")

        def get(path, **parameters):
            if path == f"/releases/tags/{VERSION}":
                return dict(original_get(path, **parameters), body=(
                    f"Тип выпуска: аварийный\nCommit: `{SHA}`\nВетка: `release/3.4`\n"
                    f"Причина: срочный сбой\nПолный CI: release-full.yml {ci_conclusion} — {ci_url}\n"
                    "PR и состояние на момент merge:\n"
                    f"- PR #42 -> release/3.4; merge `{SHA}` (2026-10-15T00:00:00Z); "
                    "pr-check=success [GitHub Actions]; bypass=yes\n"
                    f"{summary}\n"
                ))
            if path == f"/git/ref/tags/{VERSION}":
                return dict(original_get(path, **parameters), object={"type": "commit", "sha": SHA})
            if path == "/actions/runs/107/attempts/2":
                return dict(original_get(path, **parameters), conclusion=ci_conclusion, **run_overrides)
            return original_get(path, **parameters)

        api.get = get
        return api

    def test_installs_and_publishes_ordinary_and_emergency_release(self) -> None:
        """Оба режима устанавливают выпуск один раз и открывают ротацию после success."""
        for kind, flags in (("ordinary", ()), ("emergency", EMERGENCY)):
            with self.subTest(kind=kind):
                api = WritableGitHub() if kind == "ordinary" else self._emergency_api()
                receipt = replace(RECEIPT, kind=kind)
                run, commands = self._run_ssh(api, iter([receipt] * 3), *flags)
                self.assertEqual(self._invoke(api, run, "--deploy", *flags), 4)
                self.assertEqual(commands[0], ["sudo", "-n", "deploy-prod", VERSION, *flags])
                self.assertEqual([path for path, _ in api.posts], POSTS)
                self.assertEqual(api.posts[0][1]["payload"]["kind"], kind)
                require_accepted_deployment(api, receipt)

    def test_preflight_rejects_wrong_version_sha_kind_and_author_before_ssh(self) -> None:
        """Расхождение с GitHub останавливает операцию до первой команды на VPS."""
        for error, fields, sha, actor in (
            ("immutable Release", {"name": "v3.4.0"}, SHA, OWNER),
            ("другому SHA", {}, "f" * 40, OWNER),
            ("доверенный владелец", {}, SHA, {**OWNER, "id": 1}),
        ):
            with self.subTest(error=error):
                api = WritableGitHub()
                api.actor = actor
                original_get = api.get

                def get(path, **parameters):
                    response = original_get(path, **parameters)
                    return dict(response, **fields) if path == f"/releases/tags/{VERSION}" else response

                with patch.object(api, "get", side_effect=get):
                    self.assertEqual(self._invoke(api, Mock(), "--deploy", error=error, sha=sha), 0)
                self.assertEqual(api.posts, [])
        api = self._emergency_api()
        self.assertEqual(self._invoke(api, Mock(), "--deploy", error="манифест обычного выпуска"), 0)
        self.assertEqual(api.posts, [])

    def test_failed_deploy_or_ssh_stops_before_publication(self) -> None:
        """И ошибка контроллера, и разрыв SSH останавливают операцию без POST."""
        for exit_code in (1, 255):
            with self.subTest(exit_code=exit_code):
                api = WritableGitHub()
                failure = subprocess.CalledProcessError(exit_code, ["ssh"])
                calls = self._invoke(api, failure, "--deploy", error="Успех deploy-prod не подтверждён")
                self.assertEqual(calls, 1)
                self.assertEqual(api.posts, [])

    def test_recovers_publication_after_github_read_failure_without_deploy(self) -> None:
        """После успешной установки и отказа GitHub повторяет только публикацию."""
        api = WritableGitHub()
        api.fail_path = "/deployments"
        run, _ = self._run_ssh(api, iter([RECEIPT]))
        error = "Повторите только --publish для этого выпуска. Причина: API unavailable"
        self.assertEqual(self._invoke(api, run, "--deploy", error=error), 2)
        self.assertEqual(api.posts, [])

        api.fail_path = None
        run, _ = self._run_ssh(api, iter([RECEIPT] * 3))
        self.assertEqual(self._invoke(api, run, "--publish"), 3)
        self.assertEqual([path for path, _ in api.posts], POSTS)
        require_accepted_deployment(api, RECEIPT)

    def test_runtime_change_stops_before_next_write(self) -> None:
        """Смена runtime блокирует создание записи или присвоение статуса."""
        for stable_reads, expected_posts in ((1, []), (2, POSTS[:1])):
            with self.subTest(stable_reads=stable_reads):
                api = WritableGitHub()
                changed = replace(RECEIPT, runtime_id="0" * 64)
                run, _ = self._run_ssh(api, iter([RECEIPT] * stable_reads + [changed]))
                self._invoke(api, run, "--deploy", error="runtime VPS изменились")
                self.assertEqual([path for path, _ in api.posts], expected_posts)
                with self.assertRaises(DeploymentAcceptanceError):
                    require_accepted_deployment(api, changed)
                if expected_posts:
                    run, _ = self._run_ssh(api, iter([changed] * 2))
                    self.assertEqual(self._invoke(api, run, "--publish"), 2)
                    self.assertEqual([path for path, _ in api.posts], POSTS)
                    require_accepted_deployment(api, changed)

    def test_emergency_without_exact_success_does_not_open_rotation(self) -> None:
        """Техническая приёмка аварийной установки не заменяет успешный точный CI."""
        receipt = replace(RECEIPT, kind="emergency")
        for ci in ({"ci_conclusion": "failure"}, {"run_attempt": 3}, {"head_sha": "f" * 40}):
            with self.subTest(ci=ci):
                api = self._emergency_api(**ci)
                run, _ = self._run_ssh(api, iter([receipt]), *EMERGENCY)
                error = ("Повторите только --publish для этого выпуска. "
                         "Причина: Не подтверждена точная успешная попытка CI")
                self.assertEqual(self._invoke(api, run, "--deploy", *EMERGENCY, error=error), 2)
                self.assertEqual(api.posts, [])
                with self.assertRaises(DeploymentAcceptanceError):
                    require_accepted_deployment(api, receipt)
