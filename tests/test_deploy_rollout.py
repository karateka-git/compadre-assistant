"""Проверяет проверяемый rollout gate и повторный допуск до остановки."""

from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from scripts import deploy_prod as prod, deploy_rollout as rollout
from scripts.select_deploy_release import SelectedRelease


class RolloutTests(unittest.TestCase):
    """Отвергает незагруженный unit, чужой backup и изменившееся evidence."""

    def _host(self):
        host = MagicMock()
        host.state_dir = Path("/private/state")
        host.recovery_inventory.return_value = {"tg": {"Id": "tg"}, "vk": {"Id": "vk"}}
        backup = [prod.SELECTOR_EXEC[0], prod.SELECTOR_EXEC[:-2] + ["scripts.deploy_backup"],
                  prod.PINNED_EXEC_FLAGS, 0, 0, 0, 0, 0, 0, 0]

        def run(*args):
            if "NeedDaemonReload" in " ".join(args):
                return "no"
            if "DropInPaths" in " ".join(args):
                return f"{prod.BACKUP_GUARD} {rollout.BACKUP_DROPIN}"
            if args[-1] == "ExecStartEx":
                return json.dumps({"type": "a(sasasttttuii)", "data": [backup]})
            if args[-1] == "Conditions":
                return json.dumps({"type": "a(sbbsi)", "data": [["ConditionPathExists", False, True,
                                   str(host.state_dir / "deploy-in-progress"), 0]]})
            return ""

        host.run.side_effect = run
        return host

    def test_infrastructure_and_each_fail_closed_boundary(self):
        """Проверяет реальный gate; только внешние filesystem/systemd ответы моделируются."""
        info = SimpleNamespace(st_mode=0o100644, st_uid=0, st_gid=0, st_nlink=1)
        for mutation in (None, "file", "reload", "backup", "guard", "snapshot", "writers", "boot"):
            host = self._host()
            original = host.run.side_effect

            def run(*args):
                if mutation == "reload" and "NeedDaemonReload" in " ".join(args):
                    return "yes"
                if mutation == "backup" and args[-1] == "ExecStartEx":
                    return json.dumps({"type": "a(sasasttttuii)", "data": []})
                if mutation == "guard" and args[-1] == "Conditions":
                    return json.dumps({"type": "a(sbbsi)", "data": []})
                return original(*args)

            host.run.side_effect = run
            if mutation == "snapshot":
                host.controller_sha.side_effect = ValueError("snapshot")
            if mutation == "writers":
                host.require_no_foreign_database_mounts.side_effect = ValueError("writers")
            if mutation == "boot":
                host.require_systemd_boot.side_effect = ValueError("boot")
            with self.subTest(mutation=mutation), patch.object(Path, "lstat", return_value=info), patch.object(
                    Path, "read_bytes", return_value=b"template" if mutation != "file" else b"unused") as read:
                if mutation == "file":
                    read.side_effect = [b"wrong", b"template"]
                if mutation:
                    with self.assertRaises(ValueError):
                        rollout.require_infrastructure(host)
                else:
                    rollout.require_infrastructure(host)
                    host.require_no_foreign_database_mounts.assert_called_once_with({"tg", "vk"})
                host.marker.assert_not_called()
                host.stop_both.assert_not_called()

    def test_exact_candidate_rechecked_and_changes_rejected(self):
        """Run/attempt, artifact, policy, OCI, tag и controller проверяются перед stop."""
        selected = SelectedRelease("karateka-git/compadre-assistant", "v3.2.0", "release/3.2",
                                   "b" * 40, 1, "main-full.yml", 2, 1)
        for mutation in (None, "selection", "evidence", "policy", "controller", "image", "baseline"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as temporary:
                root = Path(temporary)
                host = prod.Host(root / "checkout", root / "state")
                evidence = {"artifact_digest": "sha256:" + "e" * 64}
                state = {"candidate": asdict(selected) | {"image_id": "sha256:" + "c" * 64, "tag": "pin"},
                         "old_sha": "a" * 40, "old_image_id": "old", "last_good": {},
                         "controller_sha": "d" * 40, "compatibility_evidence": evidence}
                with patch.object(host, "require_switch_rollout"), patch.object(host, "select", return_value=(
                        selected if mutation != "selection" else None)), patch.object(host, "require_baseline",
                        side_effect=ValueError("baseline") if mutation == "baseline" else None), patch.object(
                        host, "image", return_value={"Id": "wrong" if mutation == "image" else state["candidate"]["image_id"]}), patch.object(
                        host, "controller_sha", return_value="wrong" if mutation == "controller" else "d" * 40), patch.object(
                        prod.prep, "check_candidate_image") as image_check, patch.object(prod.compatibility, "private_text",
                        side_effect=["p", "token", "changed" if mutation == "policy" else "p"]), patch.object(
                        prod.compatibility, "EvidenceAPI"), patch.object(prod.compatibility, "verify_evidence",
                        return_value={} if mutation == "evidence" else evidence) as verify:
                    if mutation:
                        with self.assertRaises((ValueError, prod.DeployError)):
                            host.require_switch_candidate(state)
                    else:
                        host.require_switch_candidate(state)
                        self.assertEqual(verify.call_args.args[1:], (selected, "p", "a" * 40))
                        image_check.assert_called_once_with(state["candidate"]["image_id"], selected.sha)
