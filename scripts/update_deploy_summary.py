"""Обновляет общую ветку PR по сохранённому отчёту завершённого Production release."""

from __future__ import annotations

import base64
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request
from zipfile import ZipFile

from scripts.deploy_summary import ATTEMPT, SHA, AttemptSummary
from scripts.deploy_summary_table import attempts, link_deployment_status, update_table, validated, verify_table_change
from scripts.full_ci_controller import HttpLocation, collect_http_metrics, open_http
from scripts.github_publishing import PublishingError, PublishingGitHub
from scripts.predeploy_compatibility import EvidenceAPI


WORKFLOW = ".github/workflows/production-release.yml"
DOCUMENT = "docs/DEPLOY_METRICS.md"
BRANCH = "codex/deploy-metrics"
REPOSITORY = "karateka-git/compadre-assistant"
CHECK_WORKFLOW = ".github/workflows/pr-fast.yml"


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


def update_branch(repository: str, row: AttemptSummary) -> str | None:
    """Возвращает точный SHA проверенной ветки сводки, если нужен PR."""
    ref = "refs/heads/" + BRANCH
    for _ in range(8):
        _git("fetch", "--quiet", "--no-tags", "origin", "refs/heads/main")
        main = _git("rev-parse", "FETCH_HEAD").strip()
        main_doc = _git("show", f"{main}:{DOCUMENT}")
        document = main_doc
        pending = ""
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
        document = link_deployment_status(document, repository, row.attempt, pending)
        if document == main_doc:
            return None
        if head and base == main and document == pending:
            return head
        commit = _commit(main, parents, document)
        try:
            # Lease также защищает случай, когда ветку успели слить и удалить.
            _git("push", "--quiet", f"--force-with-lease={ref}:{head or ''}", "origin", f"{commit}:{ref}")
        except subprocess.CalledProcessError:
            continue
        return commit
    raise RuntimeError("Не удалось записать сводку после повторного чтения ветки")


def ensure_pr(api: PublishingGitHub) -> dict:
    """Открывает один PR от имени писателя и возвращает подтверждённые реквизиты."""
    query = dict(base="main", head=f"{api.repository.split('/')[0]}:{BRANCH}", state="open")
    existing = api.get("/pulls", **query)
    if existing:
        return existing[0]
    try:
        return api.post("/pulls", {"head": BRANCH, "base": "main", "title": "Обновить сводку выкладок",
                                   "body": "Автоматическая сводка завершённых попыток Production release."})
    except PublishingError as error:
        existing = api.get("/pulls", **query)
        if not existing:
            if isinstance(error.__cause__, HTTPError) and error.__cause__.code == 403:
                raise PublishingError("GitHub отказал в создании PR (HTTP 403). Проверьте право "
                                      "Pull requests: write у PRODUCTION_RELEASE_TOKEN для этого репозитория") from error
            raise
        return existing[0]


def _document_at(api: PublishingGitHub, sha: str) -> str:
    obj = api.get(f"/contents/{DOCUMENT}", ref=sha)
    if obj["path"] != DOCUMENT or obj["type"] != "file" or obj["encoding"] != "base64":
        raise ValueError("GitHub не подтвердил содержимое документа")
    return base64.b64decode(obj["content"]).decode("utf-8")


def _successful_check(api: PublishingGitHub, key: str) -> dict:
    if not ATTEMPT.fullmatch(key):
        raise ValueError("Некорректный номер попытки проверки PR")
    run_id, attempt = map(int, key.split(":"))
    path = f"/actions/runs/{run_id}/attempts/{attempt}"
    run = api.get(path)
    workflow = api.get("/actions/workflows/pr-fast.yml")
    head = run["head_sha"]
    if (run["id"] != run_id or run["run_attempt"] != attempt or not SHA.fullmatch(head)
            or run["workflow_id"] != workflow["id"] or run["path"] != CHECK_WORKFLOW or workflow["path"] != CHECK_WORKFLOW
            or run["event"] != "pull_request" or run["head_branch"] != BRANCH or run["status"] != "completed"
            or run["conclusion"] != "success"
            or any(run[field]["full_name"] != api.repository for field in ("repository", "head_repository"))):
        raise ValueError("Не подтверждена проверка PR сводки")
    jobs = api.pages(path + "/jobs", "jobs")
    required = [j for j in jobs if j["name"] == "pr-check"]
    release_only = [j for j in jobs if j["name"] == "pr-merge-main-check"]
    if (len(required) != 1 or required[0]["head_sha"] != head or required[0]["conclusion"] != "success"
            or len(release_only) != 1 or release_only[0]["conclusion"] != "skipped"):
        raise ValueError("Обязательный pr-check не подтвердил SHA сводки")
    return run


def merge_checked_pr(api: PublishingGitHub, key: str) -> None:
    """Сливает точный SHA сводки после успешного обычного pr-check.

    Args:
        api: Клиент со штатным GITHUB_TOKEN без обхода правил main.
        key: ID запуска и номер попытки из события завершения проверки.
    """
    run = _successful_check(api, key)
    head = run["head_sha"]
    pulls = api.get("/pulls", state="open", base="main", head=f"{api.repository.split('/')[0]}:{BRANCH}")
    if not pulls:
        print("Открытого PR сводки уже нет")
        return
    if len(pulls) != 1:
        raise ValueError("Неоднозначный PR сводки")
    pr = pulls[0]
    if (pr["state"] != "open" or pr["head"]["ref"] != BRANCH or pr["base"]["ref"] != "main"
            or any(pr[side]["repo"]["full_name"] != api.repository for side in ("base", "head"))):
        raise ValueError("Посторонняя ветка PR")
    if pr["head"]["sha"] != head:
        print("Ветка сводки обновилась и ожидает проверки нового SHA")
        return
    if run.get("pull_requests") and not any(link["number"] == pr["number"] and link["base"]["ref"] == "main"
                                            for link in run["pull_requests"]):
        raise ValueError("Попытка pr-check связана с другим PR")
    pr_path = f"/pulls/{int(pr['number'])}"
    files = api.pages(pr_path + "/files")
    if len(files) != 1 or files[0]["filename"] != DOCUMENT or files[0]["status"] != "modified":
        raise ValueError("В PR есть изменения вне сводки")
    verify_table_change(_document_at(api, pr["base"]["sha"]), _document_at(api, head))
    # GitHub атомарно проверяет этот SHA и правила защищённой ветки при слиянии.
    request = Request(api.root + pr_path + "/merge", method="PUT",
                      data=json.dumps({"merge_method": "merge", "sha": head}).encode(), headers={
                          "Authorization": f"Bearer {api.token}", "Accept": "application/vnd.github+json",
                          "Content-Type": "application/json"})
    try:
        with open_http(request, timeout=20) as response:
            merged = json.load(response)
    except HTTPError as error:
        if error.code in (405, 409):
            current = api.get(pr_path)
            if current.get("merged") or current["head"]["sha"] != head:
                print("PR уже слит или ожидает проверки обновлённого SHA")
                return
        raise
    if merged.get("merged") is not True or not SHA.fullmatch(merged.get("sha", "")):
        raise ValueError("GitHub не подтвердил слияние сводки")
    print(f"PR #{pr['number']} со сводкой слит, merge SHA {merged['sha']}")


def main() -> None:
    """Обрабатывает завершение выпуска либо ручной повтор записи по номеру попытки."""
    if os.environ["GITHUB_REPOSITORY"] != REPOSITORY or os.environ["GITHUB_REF"] != "refs/heads/main":
        raise ValueError("Требуется защищённая main репозитория проекта")
    api = PublishingGitHub(REPOSITORY, os.environ["GITHUB_TOKEN"])
    with collect_http_metrics(HttpLocation.CI) as traffic:
        try:
            if sys.argv[1:] == ["--merge"]:
                merge_checked_pr(api, os.environ["CHECK_RUN"] + ":" + os.environ["CHECK_ATTEMPT"])
                return
            if sys.argv[1:]:
                raise ValueError("Неизвестный режим обновления сводки")
            key = os.environ["RELEASE_RUN"] + ":" + os.environ["RELEASE_ATTEMPT"]
            head = update_branch(api.repository, release_attempt(api, key))
            if head:
                writer = PublishingGitHub(REPOSITORY, os.environ["SUMMARY_PR_TOKEN"])
                pr = ensure_pr(writer)
                print(f"PR #{pr['number']} со сводкой ожидает штатный pr-check")
            else:
                print("Попытка уже учтена в main")
        finally:
            print("documentation-http-metrics " + json.dumps(traffic.rows()))


if __name__ == "__main__":
    main()
