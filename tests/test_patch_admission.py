"""Проверяет подпись и привязку допуска CI на временных ключах."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deployment_acceptance import AcceptedBaseline
from scripts.patch_admission import PRINCIPAL, PatchAdmission, sign_admission, verify_admission


class PatchAdmissionTests(unittest.TestCase):
    """Проверяет реальную локальную подпись SSH без сетевого API."""

    def setUp(self) -> None:
        """Создаёт ключ на один тест и два независимо выбранных основания выпуска."""
        root = Path(self.enterContext(TemporaryDirectory()))
        self.key = root / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True)
        self.signers = root / "allowed_signers"
        self.signers.write_text(f"{PRINCIPAL} {self.key.with_suffix('.pub').read_text()}", encoding="ascii")
        baseline = AcceptedBaseline("b" * 40, "v4.1.7", 10, 20, 30)
        self.proof = PatchAdmission(EXPECTED_REPOSITORY, "release/4.2", "v4.2.1", "a" * 40,
                                    "v4.2.0", "c" * 40, baseline, "release-full.yml", 100, 1,
                                    "d" * 40, "0001_initial_schema.sql", "e" * 64, "f" * 64)
        self.expected = {key: getattr(self.proof, key) for key in
                         ("version", "candidate_sha", "baseline", "policy_sha", "run_id", "run_attempt")}

    def test_roundtrip_and_revocation(self) -> None:
        """Подпись работает с разрешённым ключом и отвергается после его отзыва."""
        data = sign_admission(self.proof, self.key)
        self.assertEqual(verify_admission(data, self.signers, **self.expected), self.proof)
        self.signers.write_text("", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "Подпись"):
            verify_admission(data, self.signers, **self.expected)

    def test_field_changes_and_replay(self) -> None:
        """Подмена поля и повторное предъявление для другой базы или CI запрещены."""
        data = sign_admission(self.proof, self.key)
        tampered = data.replace(b"0001_initial_schema.sql", b"0002_other.sql")
        with self.assertRaisesRegex(ValueError, "Подпись"):
            verify_admission(tampered, self.signers, **self.expected)
        for changes in ({"run_id": 101}, {"run_attempt": 2}, {"policy_sha": "7" * 40},
                        {"version": "v4.2.2"}, {"candidate_sha": "2" * 40},
                        {"baseline": replace(self.proof.baseline, status_id=31)}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "другой"):
                verify_admission(data, self.signers, **(self.expected | changes))

    def test_malformed_context(self) -> None:
        """Отклоняет разрыв последовательности и неоднозначный JSON."""
        with self.assertRaisesRegex(ValueError, "тег"):
            replace(self.proof, previous_tag="v4.1.7")
        data = sign_admission(self.proof, self.key)
        duplicate = b'{"signature":"",' + data[1:]
        self.assertEqual(json.loads(duplicate), json.loads(data))
        with self.assertRaisesRegex(ValueError, "Неканонический"):
            verify_admission(duplicate, self.signers, **self.expected)
