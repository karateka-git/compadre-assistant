"""Проверяет Git-байты snapshot, безопасный staging и автономные imports."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import tarfile
import unittest

from scripts import controller_snapshot as snapshot


class ControllerSnapshotTests(unittest.TestCase):
    """Проверяет переносимый архив независимо от production и рабочего checkout."""

    def _fixture(self, root):
        repository = root / "repository"
        repository.mkdir()
        for name in (*snapshot.DEPENDENCIES, "deploy/deploy-prod", "scripts/deploy_backup.py",
                     "scripts/controller_snapshot.py", "scripts/deploy_prod.py", "scripts/deploy_runtime.py",
                     "scripts/deploy_schema_admission.py", "scripts/deploy_forward_recovery.py"):
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# fixture\n")
        (repository / "scripts/deploy_prod.py").write_text(
            "from pathlib import Path\nSOURCE_ROOT = Path(__file__).resolve().parents[1]\n",
            encoding="utf-8", newline="\n")
        def git(*args):
            return subprocess.run(["git", *args], cwd=repository, capture_output=True,
                                  check=True).stdout.decode().strip()
        git("init", "-q")
        git("config", "core.autocrlf", "false")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        return repository, git("rev-parse", "HEAD")

    def test_git_bytes_are_deterministic_and_ignore_worktree(self):
        """CRLF и посторонние untracked файлы не меняют Git-снимок."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, sha = self._fixture(root)
            first, second = root / "first.tar", root / "second.tar"
            digest = snapshot.build(repository, sha, first)
            (repository / "scripts/deploy_prod.py").write_bytes(b"broken\r\n")
            (repository / "scripts/secret.py").write_bytes(b"not tracked\n")
            self.assertEqual(digest, snapshot.build(repository, sha, second))
            manifest = snapshot.unpack(first, sha, digest, root / "staging")
            self.assertEqual(sha, manifest["controller_sha"])
            self.assertNotIn("scripts/secret.py", manifest["files"])
            snapshot.check_imports(root / "staging")

    def test_digest_sha_existing_target_and_local_output_fail_closed(self):
        """Неверный digest/SHA и существующий staging не допускают запись."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, sha = self._fixture(root)
            archive = root / "snapshot.tar"
            digest = snapshot.build(repository, sha, archive)
            for wrong_sha, wrong_digest in (("f" * 40, digest), (sha, "0" * 64)):
                with self.subTest(sha=wrong_sha, digest=wrong_digest), self.assertRaises(ValueError):
                    snapshot.unpack(archive, wrong_sha, wrong_digest, root / "staging")
                self.assertFalse((root / "staging").exists())
            with self.assertRaises(FileExistsError):
                snapshot.build(repository, sha, archive)
            with self.assertRaises(ValueError):
                snapshot.build(repository, sha, repository / "bad.tar")
            (root / "staging").mkdir()
            with self.assertRaises(FileExistsError):
                snapshot.unpack(archive, sha, digest, root / "staging")

    def test_malicious_tar_records_rejected_before_extraction(self):
        """Traversal, дубликаты, links и подменённые байты не попадают в staging."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, sha = self._fixture(root)
            original = root / "original.tar"
            snapshot.build(repository, sha, original)
            for variant in ("traversal", "symlink", "hardlink", "duplicate", "tamper", "missing"):
                with self.subTest(variant=variant):
                    archive = root / (variant + ".tar")
                    with tarfile.open(original) as source, tarfile.open(archive, "w") as output:
                        for item in source:
                            if variant == "missing" and item.name == snapshot.DEPENDENCIES[-1]:
                                continue
                            data = source.extractfile(item).read()
                            if variant == "tamper" and item.name == "scripts/deploy_prod.py":
                                data = b"# changed\n"
                                item.size = len(data)
                            output.addfile(item, io.BytesIO(data))
                        extra = tarfile.TarInfo("scripts/extra.py")
                        if variant == "traversal":
                            extra.name = "scripts/../../escaped"
                        elif variant in ("symlink", "hardlink"):
                            extra.type = tarfile.SYMTYPE if variant == "symlink" else tarfile.LNKTYPE
                            extra.linkname = "/etc/passwd"
                        elif variant == "duplicate":
                            extra.name = "scripts/deploy_prod.py"
                        if variant not in ("tamper", "missing"):
                            output.addfile(extra)
                    with self.assertRaises(ValueError):
                        snapshot.unpack(archive, sha, hashlib.sha256(archive.read_bytes()).hexdigest(), root / variant)
                    self.assertFalse((root / variant).exists())

    def test_installed_drift_extra_files_and_missing_import_dependency(self):
        """Проверка после установки обнаруживает drift и зависимость от checkout."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, sha = self._fixture(root)
            archive = root / "snapshot.tar"
            digest = snapshot.build(repository, sha, archive)
            staging = root / "staging"
            manifest = snapshot.unpack(archive, sha, digest, staging)
            target = staging / "scripts/deploy_prod.py"
            target.write_text("import missing_controller_dependency\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                snapshot.verify_directory(staging, manifest)
            with self.assertRaises(ValueError):
                snapshot.check_imports(staging)


if __name__ == "__main__":
    unittest.main()
