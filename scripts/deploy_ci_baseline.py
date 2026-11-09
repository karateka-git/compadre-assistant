"""Активирует принятый CI baseline на VPS после успешной ротации GitHub."""

from dataclasses import asdict
import json
import re

from scripts import deploy_acceptance, deploy_prepare as prep
from scripts import predeploy_compatibility as compatibility
from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deploy_prod import DeployError, Host, _atomic_text
from scripts.publish_installation_tag import _receipt
from scripts.rotate_ci_baseline import require_accepted_installation, verify_rotation_run


def activate(host: Host, version: str, sha: str, *, expected_old: str, policy_sha: str, main_sha: str,
             run_id: int, attempt: int, runtime_id: str) -> None:
    """Атомарно переключает только локальный CI pin под блокировкой выпуска.

    Args:
        host: Адаптер локального состояния VPS.
        version: Принятая версия выпуска.
        sha: Точный SHA установленного last_good.
        expected_old: Явно подтверждённый прежний локальный pin.
        policy_sha: Reviewed merge, на который уже указывает GitHub pin.
        main_sha: Полный SHA main из доказательства ротации.
        run_id: ID подтверждаемого полного запуска.
        attempt: Номер точной попытки этого запуска.
        runtime_id: Идентичность технической приёмки с машины оператора.

    Raises:
        DeployError: Активное состояние или локальный pin изменились.
        RotationError: GitHub pin или полная успешная проверка не подтверждены.
    """
    if (not prep.VERSION_PATTERN.fullmatch(version)
            or any(not prep.SHA_PATTERN.fullmatch(value) for value in (sha, expected_old, policy_sha, main_sha))
            or not re.fullmatch(r"[0-9a-f]{64}", runtime_id)):
        raise DeployError("Ротация CI требует полные SHA и точный runtime_id")
    with prep.deploy_lock(host.state_dir, host.checkout):
        path = host.state_dir / "compatibility-policy.sha"
        current = compatibility.private_text(path)
        if current not in (expected_old, policy_sha):
            raise DeployError("Локальный CI pin отличается от ожидаемого прежнего значения")
        accepted = deploy_acceptance._verify_locked(host, version, sha)
        receipt = _receipt(json.dumps(accepted), version, sha)
        if receipt.runtime_id != runtime_id:
            raise DeployError("Техническая приёмка VPS изменилась после сверки оператором")
        token = compatibility.private_text(host.state_dir / "github-read-token")
        api = compatibility.EvidenceAPI(EXPECTED_REPOSITORY, token)
        require_accepted_installation(api, receipt)
        verify_rotation_run(api, receipt=receipt, policy_sha=policy_sha, main_sha=main_sha,
                            run_id=run_id, attempt=attempt)
        if (deploy_acceptance._verify_locked(host, version, sha) != asdict(receipt)
                or compatibility.private_text(path) != current):
            raise DeployError("Установка или локальный CI pin изменились в ходе проверки")
        if current != policy_sha:
            _atomic_text(path, policy_sha + "\n")
