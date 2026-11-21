"""Обновляет общую ветку PR по сохранённому отчёту завершённого Production release."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZipFile

from scripts.deploy_summary import ATTEMPT, AttemptSummary
from scripts.deploy_summary_table import attempts, update_table, validated
from scripts.full_ci_controller import HttpLocation, collect_http_metrics
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.predeploy_compatibility import EvidenceAPI


WORKFLOW = ".github/workflows/production-release.yml"
DOCUMENT = "docs/DEPLOY_METRICS.md"
BRANCH = "codex/deploy-metrics"
REPOSITORY = "karateka-git/compadre-assistant"


def release_attempt(api: PublishingGitHub, key: str) -> AttemptSummary:
    """Читает только артефакт точной попытки доверенного workflow на main."""
    if not ATTEMPT.fullmatch(key):
        raise ValueError("Некорректный номер попытки")
    run_id, number = map(int, key.split(":"))
    run = api.get(f"/actions/runs/{run_id}/attempts/{number}")
    if (run["id"] != run_id or run["run_attempt"] != number or run["status"] != "completed"
            or run["path"] != WORKFLOW or run["event"] != "workflow_dispatch" or run["head_branch"] != "main"
            or any(run[k]["full_name"] != api.repository for k in ("repository", "head_repository"))):
        raise ValueError("Запуск не относится к завершённой выкладке из main")
    name = f"deployment-summary-{run_id}-{number}"
    artifacts = [item for item in api.pages(f"/actions/runs/{run_id}/artifacts", "artifacts", name=name)
                 if item["name"] == name]
    if not artifacts or len(artifacts) == 1 and artifacts[0]["expired"]:
        missing = AttemptSummary(key, key, None, None, "unknown", run["run_started_at"])
        return validated(asdict(missing))
    if len(artifacts) != 1:
        raise ValueError("Неоднозначный артефакт попытки")
    artifact = artifacts[0]
    owner = artifact["workflow_run"]
    if (type(artifact["id"]) is not int or artifact["id"] <= 0
            or owner["id"] != run_id or owner["head_sha"] != run["head_sha"]):
        raise ValueError("Артефакт принадлежит другому запуску")
    payload = EvidenceAPI(api.repository, api.token).download(int(artifact["id"]))
    if artifact["digest"] != "sha256:" + sha256(payload).hexdigest():
        raise ValueError("Не совпадает digest артефакта")
    with ZipFile(BytesIO(payload)) as archive:
        if archive.namelist() != ["deployment-summary.json"] or archive.infolist()[0].file_size > 4096:
            raise ValueError("Некорректный состав артефакта")
        row = validated(json.loads(archive.read("deployment-summary.json")))
    if row.attempt != key:
        raise ValueError("Артефакт содержит другую попытку")
    return row


def _git(*args: str, data: str | None = None, env=None) -> str:
    result = subprocess.run(["git", *args], input=data, text=True, encoding="utf-8",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=env)
    return result.stdout


def _commit(main: str, parents: list[str], document: str) -> str:
    # Дерево всегда берём из защищённой main, заменяем только Markdown.
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _git("read-tree", main, env=env)
        blob = _git("hash-object", "-w", "--stdin", data=document).strip()
        _git("update-index", "--add", "--cacheinfo", f"100644,{blob},{DOCUMENT}", env=env)
        tree = _git("write-tree", env=env).strip()
    parent_args = [arg for parent in parents for arg in ("-p", parent)]
    return _git("-c", "user.name=github-actions[bot]", "-c",
                "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit-tree", tree, *parent_args, "-m", "Обновить сводку выкладок").strip()


def update_branch(repository: str, row: AttemptSummary) -> bool:
    """Объединяет конкурирующие отчёты, возвращает необходимость открытого PR."""
    ref = "refs/heads/" + BRANCH
    for _ in range(8):
        _git("fetch", "--quiet", "--no-tags", "origin", "refs/heads/main")
        main = _git("rev-parse", "FETCH_HEAD").strip()
        main_doc = _git("show", f"{main}:{DOCUMENT}")
        document = main_doc
        head = None
        parents = [main]
        if _git("ls-remote", "--heads", "origin", ref).strip():
            try:
                _git("fetch", "--quiet", "--no-tags", "origin", ref)
            except subprocess.CalledProcessError:
                continue  # Ветка удалена после слияния PR.
            head = _git("rev-parse", "FETCH_HEAD").strip()
            base = _git("merge-base", main, head).strip()
            if base == main:
                parents = [head]
            elif base != head:
                parents = [head, main]
            paths = _git("diff", "--name-only", base, head).splitlines()
            if paths and paths != [DOCUMENT]:
                raise ValueError("В ветке сводки обнаружены посторонние изменения")
            pending = _git("show", f"{head}:{DOCUMENT}")
            if paths:
                baseline = {item.attempt: item for item in attempts(_git("show", f"{base}:{DOCUMENT}"))}
                for saved in attempts(pending):
                    if baseline.get(saved.attempt) != saved:
                        document = update_table(document, repository, saved)
        document = update_table(document, repository, row)
        if document == main_doc:
            return False
        if head and base == main and document == pending:
            return True
        commit = _commit(main, parents, document)
        try:
            # Lease также защищает случай, когда ветку успели слить и удалить.
            _git("push", "--quiet", f"--force-with-lease={ref}:{head or ''}", "origin", f"{commit}:{ref}")
        except subprocess.CalledProcessError:
            continue
        return True
    raise RuntimeError("Не удалось записать сводку после повторного чтения ветки")


def ensure_pr(api: PublishingGitHub) -> None:
    """Открывает один PR сводки и распознаёт его одновременное создание."""
    query = dict(base="main", head=f"{api.repository.split('/')[0]}:{BRANCH}", state="open")
    if api.get("/pulls", **query):
        return
    try:
        api.post("/pulls", {"head": BRANCH, "base": "main", "title": "Обновить сводку выкладок",
                            "body": "Автоматическая сводка завершённых попыток Production release."})
    except PublishingError:
        if not api.get("/pulls", **query):
            raise


def main() -> None:
    """Обрабатывает завершение выпуска либо ручной повтор записи по номеру попытки."""
    if os.environ["GITHUB_REPOSITORY"] != REPOSITORY or os.environ["GITHUB_REF"] != "refs/heads/main":
        raise ValueError("Требуется защищённая main репозитория проекта")
    api = PublishingGitHub(REPOSITORY, os.environ["GITHUB_TOKEN"])
    key = os.environ["RELEASE_RUN"] + ":" + os.environ["RELEASE_ATTEMPT"]
    with collect_http_metrics(HttpLocation.CI) as traffic:
        try:
            if update_branch(api.repository, release_attempt(api, key)):
                ensure_pr(api)
                print("Сводка подготовлена в PR")
            else:
                print("Попытка уже учтена в main")
        finally:
            print("documentation-http-metrics " + json.dumps(traffic.rows()))


if __name__ == "__main__":
    main()
