"""Разделяет миграции кандидата и прежние repository tests в одноразовых средах."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from tempfile import TemporaryDirectory


MIGRATIONS = "compadre_assistant/core/data/sqlite/migrations"
WORKER = Path(__file__).with_name("repository_compatibility_worker.py")
# Это наблюдённый набор 5.1, а не обязательная полная матрица допуска 5.4.
BEHAVIOR_MODULES = (
    "analysis_result_repository", "article_user_isolation", "incoming_message_repository",
    "processing_error_recorder", "user_identity_repository", "capability_preference_repository",
    "chat_modes", "vault_repositories", "obsidian_proposal_repository",
    "vault_chunk_index_repository", "vault_embedding_index_repository", "embedding_checkpoints",
    "vault_full_text_search", "vault_lease_fencing", "github_connection_state_store",
    "github_installation_writer", "vault_selection", "operation_scopes", "linked_chat_menus",
    "menu_reconciliation", "telegram_flow_smoke", "capability_composition",
)


def snapshot(root: Path) -> dict[str, str]:
    """Возвращает SHA256 всех файлов и отклоняет ссылки в одноразовом checkout."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink in checkout: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def export_commit(repository: Path, sha: str, destination: Path) -> dict[str, str]:
    """Извлекает точные Git bytes полного commit, без checkout-фильтров.

    Args:
        repository: Локальный Git repository с обоими объектами commit.
        sha: Полный lowercase SHA; разрешение release policy выполняет вызывающий CI.
        destination: Ещё не существующий одноразовый каталог.

    Returns:
        Manifest SHA256 точных файлов.

    Raises:
        ValueError: SHA, тип Git entry или bytes архива не соответствуют commit.
        subprocess.CalledProcessError: Git не смог прочитать объект.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("a full lowercase commit SHA is required")

    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True).stdout

    if git("rev-parse", "--verify", f"{sha}^{{commit}}").decode().strip() != sha:
        raise ValueError("SHA is not the exact commit")
    entries = {}
    for entry in git("ls-tree", "-rz", sha).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, digest = metadata.decode().split()
        name = raw_name.decode("utf-8")
        path = PurePosixPath(name)
        if mode not in {"100644", "100755"} or kind != "blob" or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError(f"unsupported Git entry: {name}")
        entries[name] = digest
    destination.mkdir()
    # cat-file обходит core.autocrlf/export-subst/export-ignore и не меняет bytes.
    batch = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        input="".join(f"{digest}\n" for digest in entries.values()).encode(),
        check=True, capture_output=True,
    ).stdout
    offset = 0
    for name, digest in entries.items():
        end = batch.index(b"\n", offset)
        actual_digest, kind, size = batch[offset:end].decode().split()
        length = int(size)
        content = batch[end + 1:end + 1 + length]
        offset = end + length + 2
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if kind != "blob" or actual_digest != digest or actual != digest:
            raise ValueError(f"content differs from Git blob: {name}")
        target = destination / name
        if target.exists():
            raise ValueError(f"filesystem name collision: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = snapshot(destination)
    if manifest.keys() != entries.keys():
        raise ValueError("export omitted tracked files")
    return manifest


def overlay_sql(previous: Path, candidate: Path, original: dict[str, str]) -> dict[str, str]:
    """Заменяет только SQL source до импорта, проверяя неизменность прочих bytes.

    Raises:
        ValueError: Источник содержит не SQL, SQL пуст или прежний checkout изменён.
    """
    if snapshot(previous) != original:
        raise ValueError("previous checkout changed before SQL overlay")
    sources = {}
    for root in (previous, candidate):
        directory = root / MIGRATIONS
        entries = sorted(directory.iterdir())
        if any(not p.is_file() or p.is_symlink() or (p.suffix != ".sql" and p.name != "__init__.py") for p in entries):
            raise ValueError("migration source must contain only SQL files and tracked __init__.py")
        paths = [p for p in entries if p.suffix == ".sql"]
        if not paths:
            raise ValueError("migration source has no SQL files")
        sources[root] = paths
    expected = {name: digest for name, digest in original.items()
                if not (name.startswith(MIGRATIONS + "/") and name.endswith(".sql"))}
    for path in sources[previous]:
        path.unlink()
    for path in sources[candidate]:
        content = path.read_bytes()
        (previous / MIGRATIONS / path.name).write_bytes(content)
        expected[f"{MIGRATIONS}/{path.name}"] = hashlib.sha256(content).hexdigest()
    if snapshot(previous) != expected:
        raise ValueError("SQL overlay modified non-SQL bytes")
    return expected


def clean_environment() -> dict[str, str]:
    """Убирает внешние настройки Python; worker дополнительно запускается с -I."""
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    # Старый vault lock test создаёт дочерний Python без -I/-B. Не меняя test,
    # запрещаем user site/pyc также его процессу; cwd и venv остаются previous.
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return environment


def create_environment(checkout: Path, target: Path, log: Path, wheelhouse: Path | None) -> Path:
    """Создаёт отдельный venv и ставит requirements только выбранного checkout.

    Args:
        checkout: Точный экспорт previous либо candidate.
        target: Новый каталог venv без system-site-packages.
        log: Журнал установки и pip check.
        wheelhouse: Необязательный offline-каталог wheels для CI/локальных тестов.

    Returns:
        Абсолютный путь к Python новой среды.
    """
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    with log.open("w", encoding="utf-8") as stream:
        commands = [[sys.executable, "-I", "-m", "venv", str(target)]]
        install = [str(python), "-I", "-m", "pip", "--isolated", "install", "-r", str(checkout / "requirements.txt")]
        if wheelhouse:
            install.extend(["--no-index", "--find-links", str(wheelhouse)])
        commands.extend([install, [str(python), "-I", "-m", "pip", "--isolated", "check"]])
        for command in commands:
            subprocess.run(command, cwd=target.parent, env=clean_environment(), check=True,
                           stdout=stream, stderr=subprocess.STDOUT, timeout=600)
    return python


def run_worker(python: Path, checkout: Path, mode: str, output: Path,
               extra: list[str], timeout: int) -> dict:
    """Запускает независимый worker и сохраняет отдельный результат даже при отказе."""
    result_path = output / f"{mode}.json"
    log = output / f"{mode}.log"
    try:
        with log.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(
                [str(python), "-I", "-B", str(WORKER.resolve()), "--checkout", str(checkout),
                 "--mode", mode, "--result", str(result_path), *extra],
                cwd=checkout, env=clean_environment(), stdout=stream, stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        result["returncode"] = completed.returncode
        result["passed"] = completed.returncode == 0 and result.get("passed") is True
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result = {"passed": False, "error": str(error)}
    result["log"] = str(log)
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    """Выполняет механизм шага 5.2; JSON не является доверенным CI admission."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--previous-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"scope": "step-5.2-mechanism-not-admission", "previous_sha": args.previous_sha,
              "candidate_sha": args.candidate_sha, "passed": False,
              "candidate_migrations_data_integrity": {"passed": False, "status": "not_run"},
              "old_repository_behavior": {"passed": False, "status": "not_run"},
              "harness": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), WORKER)}}
    try:
        with TemporaryDirectory(prefix="repository-compatibility-") as temporary:
            root = Path(temporary).resolve()
            previous, candidate = root / "previous", root / "candidate"
            old_manifest = export_commit(args.repository.resolve(), args.previous_sha, previous)
            new_manifest = export_commit(args.repository.resolve(), args.candidate_sha, candidate)
            report["sources"] = {"previous": old_manifest, "candidate": new_manifest}
            old_python = create_environment(previous, root / "previous-venv", output / "previous-install.log", args.wheelhouse)
            new_python = create_environment(candidate, root / "candidate-venv", output / "candidate-install.log", args.wheelhouse)
            database = root / "upgrade.db"
            seed = run_worker(old_python, previous, "seed", output, ["--database", str(database)], args.timeout)
            report["previous_seed"] = seed
            old_sql = root / "previous-sql"
            old_sql.mkdir()
            for path in (previous / MIGRATIONS).glob("*.sql"):
                (old_sql / path.name).write_bytes(path.read_bytes())
            expected = overlay_sql(previous, candidate, old_manifest)
            report["previous_with_candidate_sql"] = expected
            if seed["passed"]:
                report["candidate_migrations_data_integrity"] = run_worker(
                    new_python, candidate, "candidate", output,
                    ["--database", str(database), "--previous-sql", str(old_sql)], args.timeout)
            report["old_repository_behavior"] = run_worker(
                old_python, previous, "behavior", output,
                ["--test-modules", *[f"tests.test_{name}" for name in BEHAVIOR_MODULES]], args.timeout)
            actual_previous, actual_candidate = snapshot(previous), snapshot(candidate)
            report["source_changes"] = {
                label: [name for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
                for label, before, after in (("previous", expected, actual_previous),
                                             ("candidate", new_manifest, actual_candidate))
            }
            if any(report["source_changes"].values()):
                raise ValueError("checkout bytes changed during worker execution")
            report["source_verification"] = "passed"
            report["passed"] = all(report[key]["passed"] for key in (
                "candidate_migrations_data_integrity", "old_repository_behavior"))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Step 5.2 mechanism passed={report['passed']}; report: {output / 'report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
