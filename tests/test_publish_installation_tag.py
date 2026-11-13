"""Проверяет приёмку и тег установки без SSH и запросов к GitHub."""

from contextlib import nullcontext, redirect_stdout
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import deploy_acceptance as acceptance
from scripts import deploy_prepare as prep
from scripts import deploy_prod as prod
from scripts import publish_installation_tag as installation
from scripts.ci_repository_compatibility import ReleaseTagEvidence
from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.full_ci_controller import ControllerError
from scripts.github_publishing import PublishingError
from scripts.release_sqlite_summary import MIGRATIONS_PREFIX, InstallationSchemaEvidence
from tests import test_deploy_acceptance as acceptance_tests


VERSION = "v3.2.0"
SHA = "1" * 40
RUNTIME_ID = "2" * 64
TAG_OBJECT = "3" * 40
INSTALLATION_OBJECT = "8" * 40
TAG_PATH = f"/git/ref/tags/production/{VERSION}"
RECEIPT = installation.InstallationReceipt(
    1, VERSION, SHA, "ordinary", 1, 2, 3, "4" * 32, "0007_schema.sql", RUNTIME_ID,
)


def tag_ref(version, kind, sha):
    """Формирует минимальный ответ GitHub о точном ref."""
    return {"ref": f"refs/tags/{version}", "object": {"type": kind, "sha": sha}}


class FakeGitHub:
    """Имитирует read-back и коллизии атомарного создания Git ref."""

    repository = EXPECTED_REPOSITORY

    def __init__(self):
        self.owner = {"id": 42288695, "login": "karateka-git"}
        self.release = {
            "id": RECEIPT.release_id, "tag_name": VERSION, "name": VERSION, "draft": False,
            "prerelease": False, "immutable": True, "published_at": "2026-01-01T01:00:00Z",
            "target_commitish": SHA,
            "body": (
                f"Тип выпуска: обычный\nCommit: `{SHA}`\nВетка: `release/3.2`\n"
                f"Полный CI: https://github.com/{EXPECTED_REPOSITORY}/actions/runs/2/attempts/3"
            ),
        }
        self.tag = tag_ref(VERSION, "tag", TAG_OBJECT)
        self.annotation = {
            "sha": TAG_OBJECT, "tag": VERSION, "object": {"type": "commit", "sha": SHA},
            "message": ReleaseTagEvidence("5" * 40, "6" * 40, 2, 3, "7" * 64).message(),
        }
        self.ref = None
        self.installed_annotation = None
        self.posts = []
        self.post_error = False
        self.created_sha = INSTALLATION_OBJECT

    def authenticated_user(self):
        """Возвращает GitHub identity оператора."""
        return self.owner

    def get(self, path, **parameters):
        """Читает Release, объекты тега и точный installation ref."""
        if path == f"/releases/tags/{VERSION}":
            return self.release
        if path == f"/git/ref/tags/{VERSION}":
            return self.tag
        if path == f"/git/tags/{TAG_OBJECT}":
            return self.annotation
        if path == f"/git/tags/{INSTALLATION_OBJECT}":
            return self.installed_annotation
        if path == f"/contents/{MIGRATIONS_PREFIX.rstrip('/')}":
            if parameters != {"ref": SHA}:
                raise AssertionError(parameters)
            names = [f"{number:04}_schema.sql" for number in range(1, 8)]
            return [{"type": "file", "name": name, "path": MIGRATIONS_PREFIX + name} for name in names]
        if path == TAG_PATH:
            if self.ref is None:
                raise ControllerError("missing") from HTTPError(path, 404, "missing", {}, None)
            return self.ref
        raise AssertionError(path)

    def post(self, path, payload):
        """Записывает выбранный ref и при необходимости теряет ответ."""
        self.posts.append((path, payload))
        if path == "/git/tags":
            self.installed_annotation = {
                "tag": payload["tag"], "sha": INSTALLATION_OBJECT, "message": payload["message"],
                "object": {"type": payload["type"], "sha": payload["object"]},
            }
            return self.installed_annotation
        if path != "/git/refs":
            raise AssertionError(path)
        if self.created_sha is not None:
            kind = "tag" if self.created_sha == INSTALLATION_OBJECT else "commit"
            self.ref = tag_ref(f"production/{VERSION}", kind, self.created_sha)
        if self.post_error:
            raise PublishingError("create response lost")
        return self.ref


class PublishInstallationTagTests(unittest.TestCase):
    """Проверяет привязку решения владельца и отказ без перезаписи."""

    def test_new_release_cannot_publish_installation_tag(self):
        """Для следующего выпуска тег не подменяет запись Deployments."""
        api = FakeGitHub()
        newer = replace(RECEIPT, version="v3.4.1")
        with self.assertRaisesRegex(installation.InstallationTagError, "GitHub Deployments"):
            installation.publish(api, lambda: newer, RUNTIME_ID)
        self.assertEqual(api.posts, [])

    def test_reads_the_machine_receipt_from_deploy_prod(self):
        """Принимает настоящий вывод повторной приёмки на тестовой SQLite v2."""
        version, sha = acceptance_tests.VERSION, acceptance_tests.SHA
        with TemporaryDirectory() as temporary:
            host, state = acceptance_tests.AcceptanceTests()._setup(Path(temporary).resolve())
            with (patch.object(acceptance, "_read_lock", return_value=nullcontext()),
                  patch.object(prep, "require_exact_tag"), redirect_stdout(io.StringIO()) as output):
                prod._report_acceptance(host, version, sha, machine=True)
            host_keys = Path(temporary) / "known_hosts"
            host_keys.touch()
            result = subprocess.CompletedProcess(["ssh"], 0, output.getvalue(), "")
            with patch.object(installation.subprocess, "run", return_value=result):
                receipt = installation.read_receipt(version, sha, ssh_host="example.invalid", known_hosts=host_keys)
            self.assertEqual(receipt.generation_id, state["new"]["generation_id"])
            self.assertEqual(receipt.migration, state["new"]["manifest"][-1])
            self.assertEqual((receipt.release_id, receipt.run_id, receipt.run_attempt), (41, 107, 2))

    def test_ssh_checks_host_key_and_drops_github_token(self):
        """Проверка отправляет единственную безопасную команду на закреплённый VPS."""
        stdout = json.dumps(asdict(RECEIPT)) + "\n"
        result = subprocess.CompletedProcess(["ssh"], 0, stdout, "")
        with TemporaryDirectory(prefix="known hosts ") as root, patch.dict(
            os.environ, {"GITHUB_TOKEN": "private-write", "GH_TOKEN": "private-other"},
        ), patch.object(installation.subprocess, "run", return_value=result) as run:
            host_keys = Path(root) / "known_hosts"
            host_keys.touch()
            actual = installation.read_receipt(VERSION, SHA, ssh_host="ops@example.invalid", known_hosts=host_keys)
        self.assertEqual(actual, RECEIPT)
        command = run.call_args.args[0]
        self.assertEqual(command[-8:], [
            "--", "ops@example.invalid", "sudo", "-n", "deploy-prod", VERSION, "--verify-installed", SHA,
        ])
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("GlobalKnownHostsFile=none", command)
        self.assertIn(f'UserKnownHostsFile="{host_keys.as_posix()}"', command)
        self.assertNotIn("GITHUB_TOKEN", run.call_args.kwargs["env"])
        self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])

    def test_ssh_refuses_invalid_address_and_invalid_receipt(self):
        """Неканонические аргументы и неподтверждённое поколение закрывают допуск."""
        output = json.dumps(asdict(RECEIPT))
        variants = [
            output + "\n" + output,
            '{"schema_version":1,' + output[1:],
            json.dumps(asdict(replace(RECEIPT, generation_id="legacy"))),
            json.dumps(asdict(replace(RECEIPT, release_id=True))),
            json.dumps(asdict(replace(RECEIPT, sha="8" * 40))),
        ]
        with TemporaryDirectory() as root:
            host_keys = Path(root) / "known_hosts"
            host_keys.touch()
            with patch.object(installation.subprocess, "run") as run:
                with self.assertRaises(installation.InstallationTagError):
                    installation.read_receipt(VERSION, SHA, ssh_host="-oProxyCommand=cmd", known_hosts=host_keys)
                run.assert_not_called()
                for stdout in variants:
                    with self.subTest(stdout=stdout), self.assertRaises(installation.InstallationTagError):
                        run.return_value = subprocess.CompletedProcess(["ssh"], 0, stdout, "")
                        installation.read_receipt(VERSION, SHA, ssh_host="example.invalid", known_hosts=host_keys)

    def test_publish_creates_once_and_checks_readback(self):
        """Первое создание пишет аннотацию, а повтор только сверяет её."""
        api = FakeGitHub()
        self.assertEqual(installation.summarize(api, VERSION, SHA)["installation_status"], "pending")
        self.assertEqual(installation.publish(api, lambda: RECEIPT, RUNTIME_ID), RECEIPT)
        self.assertEqual(installation.publish(api, lambda: RECEIPT, RUNTIME_ID), RECEIPT)
        evidence = InstallationSchemaEvidence.from_receipt(asdict(RECEIPT), VERSION, SHA)
        self.assertEqual(api.posts, [
            ("/git/tags", {"tag": f"production/{VERSION}", "message": evidence.message(),
                           "object": SHA, "type": "commit"}),
            ("/git/refs", {"ref": f"refs/tags/production/{VERSION}", "sha": INSTALLATION_OBJECT}),
        ])
        self.assertTrue(installation.installation_tag_exists(api, VERSION, SHA))
        summary = installation.summarize(api, VERSION, SHA)
        self.assertEqual(summary["installation_status"], "accepted")
        self.assertEqual(summary["installation_tag_object_sha"], INSTALLATION_OBJECT)
        self.assertEqual(summary["generation_id"], RECEIPT.generation_id)
        self.assertEqual(summary["expected_migration"], RECEIPT.migration)
        self.assertEqual(summary["applied_migration"], RECEIPT.migration)

    def test_existing_direct_tag_stays_legacy(self):
        """Прямой тег подтверждается без дописывания сведений о старой SQLite."""
        api = FakeGitHub()
        api.ref = tag_ref(f"production/{VERSION}", "commit", SHA)
        self.assertEqual(installation.publish(api, lambda: RECEIPT, RUNTIME_ID), RECEIPT)
        summary = installation.summarize(api, VERSION, SHA)
        self.assertEqual(summary["installation_status"], "legacy_tag")
        self.assertIsNone(summary["applied_migration"])
        self.assertEqual(api.posts, [])

    def test_existing_annotation_conflict_fails_without_rewrite(self):
        """Принятое поколение и прогон нельзя заменить новым POST."""
        api = FakeGitHub()
        installation.publish(api, lambda: RECEIPT, RUNTIME_ID)
        self.assertTrue(installation.installation_tag_exists(api, VERSION, SHA, receipt=RECEIPT))
        with self.assertRaisesRegex(installation.InstallationTagError, "расходятся"):
            installation.installation_tag_exists(api, VERSION, SHA, receipt=replace(RECEIPT, generation_id="f" * 32))
        with self.assertRaisesRegex(installation.InstallationTagError, "расходятся"):
            installation.publish(api, lambda: replace(RECEIPT, generation_id="f" * 32), RUNTIME_ID)
        conflicting = replace(InstallationSchemaEvidence.from_receipt(asdict(RECEIPT), VERSION, SHA), run_attempt=5)
        api.installed_annotation["message"] = conflicting.message()
        with self.assertRaisesRegex(installation.InstallationTagError, "попыткой CI"):
            installation.summarize(api, VERSION, SHA)
        self.assertEqual(len(api.posts), 2)

    def test_rejects_drift_wrong_owner_or_existing_conflict(self):
        """Несовпадение приёмки, Release или существующего тега запрещает запись."""
        cases = (
            ("receipt", None, replace(RECEIPT, runtime_id="9" * 64)),
            ("owner", {"id": 1, "login": "operator"}, RECEIPT),
            ("release", {"id": 11}, RECEIPT),
            ("tag", tag_ref(VERSION, "commit", "a" * 40), RECEIPT),
            ("ref", tag_ref(f"production/{VERSION}", "tag", INSTALLATION_OBJECT), RECEIPT),
            ("ref", tag_ref(f"production/{VERSION}", "commit", "b" * 40), RECEIPT),
            ("receipt", None, replace(RECEIPT, migration="0006_schema.sql")),
        )
        for attribute, value, receipt in cases:
            with self.subTest(attribute=attribute, value=value):
                api = FakeGitHub()
                if attribute == "release":
                    api.release.update(value)
                elif attribute != "receipt":
                    setattr(api, attribute, value)
                with self.assertRaises(installation.InstallationTagError):
                    installation.publish(api, lambda: receipt, RUNTIME_ID)
                self.assertEqual(api.posts, [])

    def test_lost_or_conflicting_create_is_resolved_by_exact_ref(self):
        """Потеря ответа допустима только при точном read-back после POST."""
        for created_sha in (INSTALLATION_OBJECT, SHA, "c" * 40, None):
            with self.subTest(created_sha=created_sha):
                api = FakeGitHub()
                api.created_sha = created_sha
                api.post_error = True
                if created_sha in (INSTALLATION_OBJECT, SHA):
                    self.assertEqual(installation.publish(api, lambda: RECEIPT, RUNTIME_ID), RECEIPT)
                else:
                    with self.assertRaises(installation.InstallationTagError):
                        installation.publish(api, lambda: RECEIPT, RUNTIME_ID)
                self.assertEqual(len(api.posts), 2)

    def test_emergency_requires_a_matching_published_manifest(self):
        """Аварийная приёмка с SQLite v2 принимает только свой прямой релизный тег."""
        api = FakeGitHub()
        api.tag = tag_ref(VERSION, "commit", SHA)
        api.release["body"] = (
            f"Тип выпуска: аварийный\nCommit: `{SHA}`\nВетка: `release/3.2`\n"
            "Причина: исправление\n"
            f"Полный CI: main-full.yml failure — https://github.com/{EXPECTED_REPOSITORY}/actions/runs/1/attempts/1\n"
            f"PR и состояние на момент merge:\n- PR #4 -> release/3.2; merge `{SHA}` "
            "(release); pr-check=success [ci]; bypass=no\nНеподтверждённые или неуспешные проверки:"
        )
        receipt = replace(RECEIPT, kind="emergency")
        self.assertIsNone(installation.summarize(api, VERSION, SHA)["run_id"])
        self.assertEqual(installation.publish(api, lambda: receipt, RUNTIME_ID), receipt)
        self.assertEqual(installation.summarize(api, VERSION, SHA)["run_id"], receipt.run_id)

    def test_check_cli_has_no_write_and_publish_requires_live_confirmation(self):
        """Режим чтения выводит приёмку, а явное решение обязательно для записи."""
        api = FakeGitHub()
        arguments = [
            "publish_installation_tag", VERSION, SHA, "--ssh-host", "example.invalid",
            "--known-hosts", "known_hosts",
        ]
        with patch.object(installation, "repository_from_origin", return_value=EXPECTED_REPOSITORY), patch.object(
            installation, "PublishingGitHub", return_value=api,
        ), patch.object(installation, "read_receipt", return_value=RECEIPT) as read, patch(
            "sys.stdout", new_callable=io.StringIO,
        ) as stdout, patch("sys.stderr", new_callable=io.StringIO):
            with patch("sys.argv", arguments + ["--check"]):
                installation.main()
            self.assertEqual(json.loads(stdout.getvalue()), asdict(RECEIPT))
            stdout.truncate(0)
            stdout.seek(0)
            with patch("sys.argv", arguments[:3] + ["--summary"]):
                installation.main()
            self.assertEqual(json.loads(stdout.getvalue())["expected_migration"], RECEIPT.migration)
            with patch("sys.argv", arguments + ["--publish"]), self.assertRaises(SystemExit) as error:
                installation.main()
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(read.call_count, 1)
        self.assertEqual(api.posts, [])
