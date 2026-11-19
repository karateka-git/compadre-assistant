"""Сверяет выбор базы обоими читателями без записей GitHub и обращений к VPS."""

from dataclasses import asdict
import json
import os
from unittest.mock import patch

from scripts.create_release_branch import EXPECTED_REPOSITORY
from scripts.deployment_acceptance import (
    AcceptedBaseline, DeploymentAcceptanceError, DeploymentRecord, _latest_for, _resolve_observed_baseline,
    resolve_accepted_baseline,
    verify_accepted_baseline,
)
from scripts.full_ci_controller import GitHub


def compare_readers(api: GitHub, latest: tuple[dict, DeploymentRecord] | None = None) -> AcceptedBaseline:
    """Подтверждает совпадение исходной базы, текущего статуса и записи её SHA.

    Args:
        api: Клиент с реальным токеном проверяемого процесса.
        latest: Свежая запись v2, уже прочитанная в той же секции под lock.

    Returns:
        Согласованная база для текущего наблюдения.

    Raises:
        DeploymentAcceptanceError: Хотя бы один читатель вернул другую запись.
        ControllerError: Один из обязательных API-запросов недоступен.
    """
    with patch.dict(os.environ, COMPADRE_DEPLOYMENT_READER="v1"):
        original = resolve_accepted_baseline(api)
    with patch.dict(os.environ, COMPADRE_DEPLOYMENT_READER="v2"):
        selected = resolve_accepted_baseline(api) if latest is None else _resolve_observed_baseline(api, latest)[0]
        pinned = verify_accepted_baseline(api, original.deployment_id, original.status_id)
        by_sha = _latest_for(api, original.sha)
    if (original != selected or original != pinned or by_sha is None
            or (by_sha[1].deployment_id, by_sha[1].status_id) != (original.deployment_id, original.status_id)):
        raise DeploymentAcceptanceError("Исходная база расходится при проверке v1 и v2")
    return original


if __name__ == "__main__":
    print(json.dumps(asdict(compare_readers(GitHub(EXPECTED_REPOSITORY, os.environ["GITHUB_TOKEN"]))), sort_keys=True))
