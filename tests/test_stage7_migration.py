"""Проверяет отдельный migration broker без Docker daemon и production файлов."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from scripts import stage7_migration as broker
from scripts.stage7_transport import IsolationError


class MigrationBrokerTests(unittest.TestCase):
    """Проверяет OCI/ENV, закрытое окружение, отказ миграции и cleanup timeout."""

    def test_broker_restricts_image_environment_and_cleans_exact_container(self):
        """Модель subprocess никогда не исполняется; отказы не раскрывают вывод."""
        for failure in (None, "revision", "credentials", "volume", "inspect", "migration", "timeout", "cleanup"):
            with self.subTest(failure=failure):
                root, database = MagicMock(), MagicMock()
                root.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o40700)
                database.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o100600)
                database.parent.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o40700)
                image_id, sha = "sha256:" + "b" * 64, "a" * 40
                image = {"Id": image_id, "Config": {"Labels": {"org.opencontainers.image.revision": sha},
                         "WorkingDir": "/app", "Env": ["PATH=/usr/bin:/bin"], "Volumes": None}}
                if failure == "revision":
                    image["Config"]["Labels"]["org.opencontainers.image.revision"] = "wrong"
                if failure == "credentials":
                    image["Config"]["Env"].append("TELEGRAM_BOT_TOKEN=synthetic-secret")
                if failure == "volume":
                    image["Config"]["Volumes"] = {"/unreviewed": {}}

                def run(args, **kwargs):
                    if args[1:3] == ("image", "inspect"):
                        return SimpleNamespace(returncode=int(failure == "inspect"), stdout=json.dumps([image]))
                    if args[1] == "run":
                        if failure == "timeout":
                            raise broker.subprocess.TimeoutExpired("docker", 300)
                        return SimpleNamespace(returncode=int(failure == "migration"), stdout=b"synthetic-secret")
                    if args[1] == "ps":
                        return SimpleNamespace(returncode=0, stdout=b"leftover" if failure == "cleanup" else b"")
                    return SimpleNamespace(returncode=0, stdout=b"")

                command = ("docker", "run", "--rm", "--network", "none", "image")
                with patch.object(broker, "migration_command", return_value=command), patch.object(
                        broker.sys, "platform", "linux"), patch.object(broker.os, "geteuid", return_value=0, create=True), patch.object(
                        broker.os.path, "lexists", return_value=False), patch.object(broker.subprocess, "run", side_effect=run) as external:
                    if failure:
                        with self.assertRaises(IsolationError) as raised:
                            broker.migrate(root, database, image_id, sha)
                        self.assertNotIn("synthetic-secret", str(raised.exception))
                    else:
                        broker.migrate(root, database, image_id, sha)
                    calls = external.call_args_list
                    starts = [call for call in calls if call.args[0][1] == "run"]
                    if failure in ("revision", "credentials", "volume", "inspect"):
                        self.assertEqual(starts, [])
                    else:
                        self.assertEqual(len(starts), 1)
                        start = starts[0]
                        name = start.args[0][3]
                        self.assertTrue(name.startswith("compadre-stage7-migrate-"))
                        self.assertEqual(calls[-2].args[0], ("/usr/bin/docker", "rm", "-f", name))
                        self.assertIn(f"name=^/{name}$", calls[-1].args[0])
                        self.assertEqual(set(start.kwargs["env"]), {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG"})
                        self.assertIs(start.kwargs["stdin"], broker.subprocess.DEVNULL)
