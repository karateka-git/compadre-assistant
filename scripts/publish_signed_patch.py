"""Публикует выбранный PATCH по подписанному результату доверенного CI."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re

from scripts import publish_release as releases
from scripts.ci_repository_compatibility import DeploymentReleaseTagEvidence
from scripts.full_ci_admission import SHA_PATTERN
from scripts.full_ci_controller import ControllerError, HttpPhase, http_phase
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.patch_admission import PatchAdmission, read_signed_admission
from scripts.predeploy_compatibility import EvidenceAPI, _files
from scripts.release_sqlite_summary import EXPECTED_MIGRATION_LABEL
from scripts.select_deploy_release import DeploySelectionError, SelectedRelease, _manifest, _tag, checked_release


# REST по имени возвращает опубликованный Release. GraphQL видит также pending tag черновика.
_FREE_PATCH = """
query PatchPublication($owner:String!, $name:String!, $version:String!, $ref:String!) {
  repository(owner:$owner, name:$name) {
    nameWithOwner release(tagName:$version) { isDraft isPrerelease } ref(qualifiedName:$ref) { id } latestRelease { tagName }
  }
}
"""


def download_admission(api: PublishingGitHub, signer: str, keys: Path, policy: str,
                       version: str, sha: str) -> tuple[PatchAdmission, bytes]:
    """Получает один артефакт точной попытки подписанта и проверяет его локально.

    Args:
        api: Клиент защищённого запуска публикации.
        signer: Номер запуска и попытки подписания из его сводки.
        keys: Доверенные ключи из окружения production-release.
        policy: Внешний SHA ожидаемой политики.
        version: Запрошенная оператором версия.
        sha: Полный SHA выбранного кандидата.

    Returns:
        Проверенные реквизиты и исходный подписанный JSON для контроллера.

    Raises:
        ValueError: Попытка, артефакт, подпись или содержимое расходятся.
    """
    if not re.fullmatch(r"[1-9][0-9]{0,19}:[1-9][0-9]{0,9}", signer):
        raise ValueError("Укажите точный signer run_id:run_attempt")
    run_id, attempt = map(int, signer.split(":"))
    run = api.get(f"/actions/runs/{run_id}/attempts/{attempt}")
    if (run.get("id") != run_id or run.get("run_attempt") != attempt or run.get("event") != "workflow_run"
            or run.get("path") != ".github/workflows/patch-admission.yml" or run.get("head_branch") != "main"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or not isinstance(run.get("head_sha"), str) or not SHA_PATTERN.fullmatch(run["head_sha"])
            or run.get("repository", {}).get("full_name") != api.repository
            or run.get("head_repository", {}).get("full_name") != api.repository):
        raise ValueError("GitHub не подтвердил попытку доверенного подписанта")
    name = f"patch-admission-{run_id}-{attempt}"
    artifacts = api.pages(f"/actions/runs/{run_id}/artifacts", key="artifacts", name=name)
    if len(artifacts) != 1 or artifacts[0].get("name") != name:
        raise ValueError("Артефакт подписанта отсутствует или неоднозначен")
    item = artifacts[0]
    if (type(item.get("id")) is not int or item["id"] <= 0 or item.get("expired") is not False
            or datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)
            or item.get("workflow_run", {}).get("id") != run_id
            or item["workflow_run"].get("head_sha") != run.get("head_sha")):
        raise ValueError("Метаданные допуска расходятся с попыткой подписания")
    archive = EvidenceAPI(api.repository, api.token).download(item["id"])
    if item.get("digest") != "sha256:" + sha256(archive).hexdigest():
        raise ValueError("Не совпал digest архива допуска")
    files = _files(archive)
    if set(files) != {"patch-admission.json"}:
        raise ValueError("Некорректный состав архива допуска")
    data = files["patch-admission.json"]
    proof = read_signed_admission(data, keys)
    if (proof.repository, proof.version, proof.candidate_sha, proof.policy_sha) != (api.repository, version, sha, policy):
        raise ValueError("Допуск не совпадает с запрошенным PATCH или закреплённой политикой")
    return proof, data


def _select(proof: PatchAdmission, release: dict, object_sha: str, evidence: DeploymentReleaseTagEvidence) -> SelectedRelease:
    checked_release(release, proof.version)
    identity = _manifest(release["body"], proof.version, proof.repository)
    migration = [line for line in release["body"].splitlines() if line.startswith(EXPECTED_MIGRATION_LABEL)]
    if (release["target_commitish"] != proof.candidate_sha
            or identity != (proof.candidate_sha, proof.branch, proof.run_id, proof.run_attempt)
            or migration != [f"{EXPECTED_MIGRATION_LABEL} `{proof.migration}`"]):
        raise ValueError("Опубликованный Release расходится с подписанным допуском")
    return SelectedRelease(proof.repository, proof.version, proof.branch, proof.candidate_sha, release["id"],
                           proof.workflow, proof.run_id, proof.run_attempt, tag_object_sha=object_sha,
                           compatibility=evidence)


def publish_or_select(api: PublishingGitHub, proof: PatchAdmission, notes: str | None) -> SelectedRelease:
    """Создаёт аннотированный тег и Release либо сверяет готовую публикацию.

    Args:
        api: Клиент доверенного писателя.
        proof: Локально проверенный допуск.
        notes: Описание для первой публикации. При восстановлении требуется готовый Release.

    Returns:
        Точные реквизиты опубликованного объекта для резервации.

    Raises:
        ValueError: Release, тег или версия расходятся с допуском.
        PublishingError: Не подтверждён POST. Автоматического повторения записи нет.
    """
    version = proof.version
    evidence = DeploymentReleaseTagEvidence(proof.baseline.sha, proof.policy_sha, proof.run_id,
                                           proof.run_attempt, proof.schema_fingerprint, proof.baseline)
    path = f"/releases/tags/{version}"
    with http_phase(HttpPhase.RELEASE_LOOKUP):
        owner, name = proof.repository.split("/")
        observed = api.graphql(_FREE_PATCH, owner=owner, name=name, version=version,
                               ref=f"refs/tags/{version}")["repository"]
        if observed["nameWithOwner"] != proof.repository:
            raise ValueError("GitHub не подтвердил репозиторий PATCH")
        found = observed["release"]
        if found is not None and (found["isDraft"] is not False or found["isPrerelease"] is not False):
            raise ValueError("Имя занято черновиком или предварительным Release")
        if found is not None or observed["ref"] is not None:
            existing = api.get(path)
            sha, object_sha, actual = _tag(api, version, allow_annotation=True)
            if sha != proof.candidate_sha or not object_sha or actual != evidence:
                raise ValueError("Готовый тег не совпадает с подписанным допуском")
            return _select(proof, existing, object_sha, evidence)
        latest = releases._version(observed["latestRelease"]["tagName"])
    if notes is None:
        raise ValueError("Для восстановления требуется ранее опубликованный Release")
    releases.check_notes(notes)
    run_url = f"https://github.com/{proof.repository}/actions/runs/{proof.run_id}/attempts/{proof.run_attempt}"
    body = (f"{notes.strip()}\n\nТип выпуска: обычный\nCommit: `{proof.candidate_sha}`\nВетка: `{proof.branch}`\n"
            f"Полный CI: {run_url}\n{EXPECTED_MIGRATION_LABEL} `{proof.migration}`\n")
    with http_phase(HttpPhase.RELEASE_PUBLISH):
        try:
            object_sha = releases.create_annotated_tag(api, version, proof.candidate_sha, evidence)
        except PublishingError:
            # Ответ мог потеряться после создания ref, а имя могло быть занято раньше.
            try:
                sha, object_sha, actual = _tag(api, version, allow_annotation=True)
                if sha != proof.candidate_sha or not object_sha or actual != evidence:
                    raise ValueError("Занятый тег расходится с допуском PATCH")
                return _select(proof, api.get(path), object_sha, evidence)
            except (ControllerError, DeploySelectionError) as observed:
                raise PublishingError("Не подтверждена полная публикация. Сверьте тег и Release") from observed
        try:
            raw = api.post("/releases", {
                "name": version, "tag_name": version, "target_commitish": proof.candidate_sha,
                "draft": False, "prerelease": False, "body": body,
                "make_latest": "true" if releases._version(version) > latest else "false",
            })
        except PublishingError:
            raw = api.get(path)
        if raw.get("body") != body:
            raise ValueError("Опубликованный текст Release расходится с выбранным")
        return _select(proof, raw, object_sha, evidence)
