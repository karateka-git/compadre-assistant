"""Собирает и проверяет автономный controller snapshot из точных Git-байтов."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile


# Только эти файлы приложения нужны импортам контроллера вне release checkout.
DEPENDENCIES = (
    "compadre_assistant/__init__.py",
    "compadre_assistant/core/__init__.py",
    "compadre_assistant/core/data/__init__.py",
    "compadre_assistant/core/data/sqlite/__init__.py",
    "compadre_assistant/core/data/sqlite/runtime_schema.py",
    "compadre_assistant/core/data/sqlite/migration_runner.py",
)
MANIFEST = "controller-manifest.json"
# Ограничения защищают staging от неожиданного размера и состава архива.
MAX_SIZE = 16 * 1024 * 1024
MAX_FILES = 512


def _git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repository, capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Не удалось прочитать controller commit из Git")
    return result.stdout


def _path(name: str) -> bool:
    path = PurePosixPath(name)
    return (str(path) == name and not path.is_absolute() and ".." not in path.parts
            and re.fullmatch(r"[a-zA-Z0-9_./-]+", name) is not None
            and (name.startswith(("scripts/", "deploy/")) or name in DEPENDENCIES))


def build(repository: Path, sha: str, destination: Path) -> str:
    """Создаёт новый tar с manifest и возвращает SHA256 всего архива.

    Args:
        repository: Локальный Git repository; рабочие файлы не читаются.
        sha: Полный SHA проверяемого commit, отдельно от SHA Release.
        destination: Отсутствующий путь архива вне checkout.

    Raises:
        ValueError: Commit, тип Git-записи или состав snapshot недопустим.
        OSError: Архив уже существует либо запись невозможна.
    """
    if (not re.fullmatch(r"[0-9a-f]{40}", sha)
            or _git(repository, "cat-file", "-t", sha).strip() != b"commit"):
        raise ValueError("Нужен полный SHA controller commit")
    if destination.resolve().is_relative_to(repository.resolve()):
        raise ValueError("Архив должен находиться вне checkout")
    tree = _git(repository, "ls-tree", "-r", "-z", sha, "--", "scripts", "deploy", *DEPENDENCIES)
    payload = {}
    files = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        name = raw_name.decode("utf-8")
        if not _path(name) or mode not in ("100644", "100755") or kind != "blob":
            raise ValueError("Snapshot содержит недопустимую Git-запись")
        value = _git(repository, "cat-file", "blob", object_id)
        if b"\r" in value:
            raise ValueError("Snapshot содержит CR вместо точных LF-скриптов")
        payload[name] = value
        files[name] = {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value),
                       "mode": 0o755 if name == "deploy/deploy-prod" else 0o644}
    if not set(DEPENDENCIES).issubset(files) or "scripts/deploy_prod.py" not in files:
        raise ValueError("Commit не содержит автономных зависимостей controller")
    manifest = {"format_version": 1, "controller_sha": sha, "files": files}
    payload[MANIFEST] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    if len(payload) > MAX_FILES or sum(map(len, payload.values())) > MAX_SIZE // 2:
        raise ValueError("Snapshot превышает допустимый размер")
    with destination.open("xb") as output:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, value in sorted(payload.items()):
                info = tarfile.TarInfo(name)
                info.mode = files[name]["mode"] if name != MANIFEST else 0o644
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def inspect_archive(archive: Path, sha: str, digest: str) -> tuple[dict, dict[str, bytes]]:
    """Проверяет digest из доверенного канала, manifest и все tar-записи.

    SHA/digest передаются оператором из локального протокола сборки. Manifest
    внутри архива сам по себе не является доказательством происхождения.

    Raises:
        ValueError: Digest, SHA, путь, тип, режим либо состав не совпадает.
    """
    if (not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not re.fullmatch(r"[0-9a-f]{64}", digest) or archive.stat().st_size > MAX_SIZE):
        raise ValueError("Некорректная идентичность или размер snapshot")
    raw = archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("SHA256 архива не совпадает")
    values = {}
    modes = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source:
        for item in source:
            if (len(values) >= MAX_FILES or not item.isreg() or item.pax_headers
                    or item.name in values or not (item.name == MANIFEST or _path(item.name))
                    or item.mode not in (0o644, 0o755) or item.size > MAX_SIZE // 2
                    or item.uid != 0 or item.gid != 0):
                raise ValueError("Недопустимая запись tar snapshot")
            values[item.name] = source.extractfile(item).read()
            modes[item.name] = item.mode
    manifest = json.loads(values.pop(MANIFEST))
    files = manifest["files"]
    if (manifest.get("format_version") != 1 or manifest.get("controller_sha") != sha
            or set(files) != set(values) or not set(DEPENDENCIES).issubset(files)
            or "scripts/deploy_prod.py" not in files or modes[MANIFEST] != 0o644):
        raise ValueError("Manifest controller snapshot не совпадает")
    for name, value in values.items():
        expected_mode = 0o755 if name == "deploy/deploy-prod" else 0o644
        if (files[name] != {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value),
                            "mode": expected_mode} or modes[name] != expected_mode or b"\r" in value):
            raise ValueError("Файл controller snapshot не совпадает с manifest")
        if name.endswith(".py"):
            compile(value, name, "exec")
    return manifest, values


def verify_directory(directory: Path, manifest: dict, *, require_root: bool = False) -> None:
    """Сверяет staging/установленный каталог без links, лишних файлов и pycache.

    Args:
        directory: Канонический каталог snapshot вне release checkout.
        manifest: Manifest из уже проверенного архива.
        require_root: Требовать root:root при проверке установленного snapshot.

    Raises:
        ValueError: Тип, digest, состав или Linux-владелец/права отличаются.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Snapshot должен быть обычным каталогом")
    found = set()
    expected = set(manifest["files"]) | {MANIFEST}
    for path in (directory, *directory.rglob("*")):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not (path.is_dir() or stat.S_ISREG(info.st_mode))
                or (not path.is_dir() and info.st_nlink != 1)):
            raise ValueError("Snapshot содержит link или специальный файл")
        if os.name == "posix" and (info.st_mode & 0o022 or
                                  (require_root and (info.st_uid != 0 or info.st_gid != 0))):
            raise ValueError("Snapshot должен принадлежать root без чужой записи")
        if path.is_file():
            name = path.relative_to(directory).as_posix()
            found.add(name)
            if name not in expected:
                raise ValueError("Snapshot содержит лишние файлы")
            if name == MANIFEST:
                if json.loads(path.read_bytes()) != manifest:
                    raise ValueError("Установленный manifest отличается")
            elif (hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]["sha256"]
                  or (os.name == "posix" and stat.S_IMODE(info.st_mode) != manifest["files"][name]["mode"])):
                raise ValueError("Установленные байты или права snapshot отличаются")
    if found != expected:
        raise ValueError("Snapshot неполон")


def unpack(archive: Path, sha: str, digest: str, staging: Path) -> dict:
    """Распаковывает проверенные байты исключительно в новый staging.

    Не переключает установленный controller, wrapper или внешнее состояние.
    При отказе staging сохраняется для диагностики и не используется.
    """
    manifest, values = inspect_archive(archive, sha, digest)
    if staging.is_symlink() or staging.resolve() != staging.absolute():
        raise ValueError("Staging содержит перенаправление пути")
    staging.mkdir(mode=0o700)
    for name, value in values.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(value)
        target.chmod(manifest["files"][name]["mode"])
    (staging / MANIFEST).write_bytes((json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    verify_directory(staging, manifest)
    return manifest


def check_imports(directory: Path) -> None:
    """Проверяет imports отдельным изолированным Python без release checkout.

    Проверяется только доверенный snapshot: импорт исполняет его Python-код.
    """
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from scripts import deploy_prod, controller_snapshot, deploy_backup, deploy_runtime, "
            "deploy_schema_admission, deploy_forward_recovery; "
            "assert str(deploy_prod.SOURCE_ROOT) == sys.argv[1]")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(directory.resolve())],
                            cwd=directory.parent, capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Изолированный import controller snapshot завершился отказом")


def main() -> None:
    """Обрабатывает сборку, проверку и подготовку staging без установки на VPS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify", "unpack"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--digest")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--require-root", action="store_true")
    args = parser.parse_args()
    if args.action == "build":
        print(json.dumps({"controller_sha": args.sha,
                          "archive_sha256": build(args.repository, args.sha, args.archive)}))
    else:
        if args.digest is None:
            parser.error("Проверка требует --digest из протокола сборки")
        manifest, _ = inspect_archive(args.archive, args.sha, args.digest)
        if args.action == "unpack":
            if args.directory is None:
                parser.error("Распаковка требует --directory нового staging")
            unpack(args.archive, args.sha, args.digest, args.directory)
        if args.directory is not None:
            verify_directory(args.directory, manifest, require_root=args.require_root)
            check_imports(args.directory)
        print(json.dumps({"controller_sha": args.sha, "files": len(manifest["files"]), "verified": True}))


if __name__ == "__main__":
    main()
