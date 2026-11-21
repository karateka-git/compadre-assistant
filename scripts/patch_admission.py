"""Подписывает допуск PATCH и проверяет его относительно локальных реквизитов."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deploy_prepare import SHA_PATTERN, VERSION_PATTERN
from scripts.deployment_acceptance import AcceptedBaseline, DeploymentAcceptanceError
from scripts.full_ci_admission import RELEASE_WORKFLOW


# Принципал и пространство имён отделяют допуск PATCH от иных подписей SSH.
PRINCIPAL = "compadre-patch-ci"
NAMESPACE = "compadre-patch-admission-v1"


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


@dataclass(frozen=True)
class PatchAdmission:
    """Связывает проверенный диапазон PATCH с попыткой CI и базой установки."""

    repository: str
    branch: str
    version: str
    candidate_sha: str
    previous_tag: str
    previous_tag_sha: str
    baseline: AcceptedBaseline
    workflow: str
    run_id: int
    run_attempt: int
    policy_sha: str
    migration: str
    schema_fingerprint: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        """Отклоняет чужую линию, пропуск номера и некорректные реквизиты."""
        if self.repository != EXPECTED_REPOSITORY or self.workflow != RELEASE_WORKFLOW:
            raise ValueError("Чужой репозиторий или workflow допуска PATCH")
        if not isinstance(self.version, str) or not VERSION_PATTERN.fullmatch(self.version):
            raise ValueError("Некорректная версия PATCH")
        line, _, number = self.version[1:].rpartition(".")
        if (self.branch != f"release/{line}" or int(number) == 0
                or self.previous_tag != f"v{line}.{int(number) - 1}"):
            raise ValueError("Линия или предыдущий тег расходятся с версией PATCH")
        for sha in (self.candidate_sha, self.previous_tag_sha, self.policy_sha):
            if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
                raise ValueError("Некорректный SHA допуска PATCH")
        for digest in (self.evidence_sha256, self.schema_fingerprint):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Некорректный отпечаток допуска PATCH")
        if (not isinstance(self.baseline, AcceptedBaseline)
                or any(type(value) is not int or value <= 0 for value in (self.run_id, self.run_attempt))
                or not isinstance(self.migration, str)
                or not re.fullmatch(r"(?!0000)[0-9]{4}_[a-z0-9_]+\.sql", self.migration)):
            raise ValueError("Некорректная база, попытка CI или миграция допуска PATCH")


def sign_admission(admission: PatchAdmission, key: Path) -> bytes:
    """Подписывает реквизиты после проверки успешной попытки полным CI.

    Args:
        admission: Данные выпуска, проверенные изолированным подписантом.
        key: Путь к выделенному закрытому ключу в окружении подписания.

    Returns:
        Канонические байты допуска с подписью SSH.

    Raises:
        subprocess.CalledProcessError: Подписание завершилось ошибкой.
    """
    record = asdict(admission)
    signed = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", NAMESPACE, "-f", str(key)],
                            input=_canonical(record), capture_output=True, check=True)
    return _canonical({"admission": record, "signature": signed.stdout.decode("ascii")})


def verify_admission(data: bytes, allowed_signers: Path, *, version: str, candidate_sha: str,
                     baseline: AcceptedBaseline, policy_sha: str, run_id: int, run_attempt: int) -> PatchAdmission:
    """Сверяет подпись SSH и привязку к ожидаемому выпуску и базе.

    Args:
        data: Переданные runner байты подписанного допуска.
        allowed_signers: Закреплённый локальный файл доверенных открытых ключей.
        version: Запрошенный номер PATCH.
        candidate_sha: SHA кандидата выбранного выпуска.
        baseline: Независимо подтверждённая приёмка текущей установки.
        policy_sha: SHA доверенной ревизии политики.
        run_id: Номер выбранного полного CI.
        run_attempt: Номер попытки этого CI.

    Returns:
        Проверенные реквизиты установки.

    Raises:
        ValueError: Подпись, формат или ожидаемые реквизиты расходятся.
        OSError: Инструмент проверки или файл подписи недоступны.
    """
    proof = read_signed_admission(data, allowed_signers)
    expected = (version, candidate_sha, baseline, policy_sha, run_id, run_attempt)
    actual = (proof.version, proof.candidate_sha, proof.baseline, proof.policy_sha, proof.run_id, proof.run_attempt)
    if actual != expected:
        raise ValueError("Допуск PATCH относится к другой установке или попытке CI")
    return proof


def read_signed_admission(data: bytes, allowed_signers: Path) -> PatchAdmission:
    """Читает реквизиты допуска только после локального подтверждения подписи.

    Args:
        data: Канонический JSON подписанта.
        allowed_signers: Доверенный файл открытых ключей вне кандидата.

    Returns:
        Подписанное утверждение CI для сверки с запросом и состоянием VPS.

    Raises:
        ValueError: Допуск или подпись недействительны.
    """
    if len(data) > 8192:
        raise ValueError("Слишком большой допуск PATCH")
    raw = json.loads(data)
    if (not isinstance(raw, dict) or set(raw) != {"admission", "signature"}
            or _canonical(raw) != data or not isinstance(raw["admission"], dict)
            or not isinstance(raw["signature"], str) or not raw["signature"].isascii()):
        raise ValueError("Неканонический допуск PATCH")
    record = raw["admission"]
    with TemporaryDirectory(prefix="patch-admission-") as folder:
        signature = Path(folder) / "admission.sig"
        signature.write_bytes(raw["signature"].encode("ascii"))
        verified = subprocess.run(["ssh-keygen", "-Y", "verify", "-n", NAMESPACE, "-I", PRINCIPAL,
                                   "-f", str(allowed_signers), "-s", str(signature)],
                                  input=_canonical(record), capture_output=True)
    if verified.returncode:
        raise ValueError("Подпись допуска PATCH не подтверждена доверенным ключом")
    try:
        proof = PatchAdmission(**(record | {"baseline": AcceptedBaseline(**record["baseline"])}))
    except (TypeError, KeyError, DeploymentAcceptanceError) as error:
        raise ValueError("Некорректные поля допуска PATCH") from error
    return proof
