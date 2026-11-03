"""Проверяет локальный прототип переключателя 6.1 на синтетических SQLite.

Это исследовательский стенд, не production controller. Требует только stdlib.
Все файлы создаются в TemporaryDirectory; реальные БД и Docker не используются.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


CRASH = 73  # Управляемое аварийное завершение без finally/SQLite close.
OLD = "app.db"  # Существующее имя принимается без переноса файлов.
NEW = "app-generation-test.db"  # В production нужен уникальный непереиспользуемый ID.


def _cut(actual, requested):
    """Завершает дочерний процесс на выбранной границе файловой операции."""
    if actual == requested:
        os._exit(CRASH)


def _sync_dir(directory):
    """Синхронизирует каталог на POSIX; Windows проверяет только process crash."""
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _publish(path, value, requested="", prefix=""):
    """Публикует JSON через replace, позволяя прервать каждую границу."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value))
        stream.flush()
        _cut(prefix + "written", requested)
        os.fsync(stream.fileno())
        _cut(prefix + "synced", requested)
    os.replace(temporary, path)
    _cut(prefix + "replaced", requested)
    _sync_dir(path.parent)
    _cut(prefix + "durable", requested)


def _wal(path, label):
    """Оставляет подтверждённую строку в WAL после аварии процесса."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE IF NOT EXISTS events(value TEXT NOT NULL)")
    db.execute("INSERT INTO events VALUES (?)", (label,))
    db.commit()
    os._exit(CRASH)


def _child(*args):
    """Запускает тот же стенд отдельным проектным Python-процессом."""
    result = subprocess.run([sys.executable, __file__, *map(str, args)],
                            capture_output=True, text=True, timeout=20)
    if result.returncode != CRASH:
        raise AssertionError((result.returncode, result.stdout, result.stderr))


def _rows(path):
    """Читает существующую БД с её собственным WAL и проверяет целостность."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        return [row[0] for row in db.execute("SELECT value FROM events ORDER BY rowid")]


def _prepare(root):
    """Создаёт старую WAL-БД, окончательный снимок и независимого кандидата."""
    _child("wal", root / OLD, "old-committed")
    assert (root / (OLD + "-wal")).stat().st_size > 0
    with closing(sqlite3.connect(root / OLD)) as source:
        with closing(sqlite3.connect(root / "snapshot.db")) as snapshot:
            source.backup(snapshot)
            with closing(sqlite3.connect(root / NEW)) as candidate:
                snapshot.backup(candidate)
    _child("wal", root / OLD, "old-only")
    _child("wal", root / NEW, "candidate-only")
    _publish(root / "state.json", {"transition": "test", "admission": False,
                                   "old": OLD, "new": NEW})
    _publish(root / "active-db.json", {"transition": "test", "database": OLD})


def _transition(root, cut):
    """Моделирует intent, переключатель и монотонную границу приёма."""
    state = json.loads((root / "state.json").read_text())
    state["switch_intent"] = True
    _publish(root / "state.json", state)
    _cut("intent", cut)
    _publish(root / "active-db.json", {"transition": "test", "database": NEW},
             cut, "selector-")
    state["selected"] = NEW
    _publish(root / "state.json", state)
    _cut("selected", cut)
    state["admission"] = True
    _publish(root / "state.json", state, cut, "admission-")
    _cut("before-polling", cut)
    _wal(root / NEW, "after-admission")


def _resolve(root):
    """Возвращает наблюдаемый выбор и БД recovery либо закрывает недоказанный путь."""
    state = json.loads((root / "state.json").read_text())
    pointer = json.loads((root / "active-db.json").read_text())
    if pointer["transition"] != state["transition"]:
        raise ValueError("Чужой transition")
    selected = pointer["database"]
    if selected not in (state["old"], state["new"]):
        raise ValueError("Неизвестная БД")
    if type(state.get("admission")) is not bool:
        raise ValueError("Неизвестная граница")
    if state["admission"] and selected != state["new"]:
        raise ValueError("Граница требует новую БД")
    if selected == state["new"] and not state.get("switch_intent"):
        raise ValueError("Нет intent")
    target = state["new"] if state["admission"] else state["old"]
    for name in (selected, target):
        if not (root / name).is_file():
            raise ValueError("БД отсутствует")
    return selected, target


class SwitchExperiment(unittest.TestCase):
    """Проверяет файловый выбор и собственные WAL двух поколений."""

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI отсутствует")
    def test_compose_config(self):
        """Проверяет настоящий Compose config без daemon и без секретов проекта."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = Path(__file__).resolve().parents[2] / "docker-compose.yml"
            shutil.copyfile(source, root / "compose.yml")
            (root / ".env").write_text("DATABASE_PATH=/app/data/app.db\nAPP_ENV=test\n")
            (root / "override.yml").write_text(
                "services:\n" + "".join(
                    f"  {channel}:\n    environment:\n"
                    "      DATABASE_PATH: ${ASSISTANT_DATABASE_PATH:?required}\n"
                    for channel in ("tg_assistant", "vk_assistant")))
            command = ["docker", "compose", "-f", str(root / "compose.yml"),
                       "-f", str(root / "override.yml"), "config", "--format", "json"]
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("COMPOSE_") and key != "ASSISTANT_DATABASE_PATH"}
            environment["ASSISTANT_DATABASE_PATH"] = "/app/data/" + NEW
            result = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, timeout=20, check=True)
            config = json.loads(result.stdout)
            for channel in ("tg_assistant", "vk_assistant"):
                service = config["services"][channel]
                self.assertEqual(service["environment"]["DATABASE_PATH"], "/app/data/" + NEW)
                self.assertEqual(len(service["volumes"]), 1)
                mount = service["volumes"][0]
                self.assertEqual(mount["type"], "bind")
                self.assertEqual(mount["target"], "/app/data")
                self.assertTrue(mount["source"].replace("\\", "/").endswith("/data"))
            del environment["ASSISTANT_DATABASE_PATH"]
            refused = subprocess.run(command, env=environment, capture_output=True,
                                     text=True, timeout=20)
            self.assertNotEqual(refused.returncode, 0)

    def test_crash_windows(self):
        """Прерывает 12 границ; сохраняет WAL и повторяемый выбор recovery."""
        cuts = ["intent", "selector-written", "selector-synced", "selector-replaced",
                "selector-durable", "selected", "admission-written", "admission-synced",
                "admission-replaced", "admission-durable", "before-polling", "after-write"]
        for cut in cuts:
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                _prepare(root)
                files = [root / (name + suffix) for name in (OLD, NEW)
                         for suffix in ("", "-wal", "-shm")]
                before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
                _child("transition", root, cut)
                admitted = cut in cuts[8:]
                selected, target = _resolve(root)
                self.assertEqual(selected, OLD if cut in cuts[:3] else NEW)
                self.assertEqual(target, NEW if admitted else OLD)
                self.assertEqual(_resolve(root), (selected, target))
                if cut != "after-write":
                    self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                              for p in files})
                self.assertEqual(_rows(root / OLD), ["old-committed", "old-only"])
                expected = ["old-committed", "candidate-only"]
                if cut == "after-write":
                    expected.append("after-admission")
                self.assertEqual(_rows(root / NEW), expected)
                self.assertEqual(_rows(root / "snapshot.db"), ["old-committed"])

    def test_ambiguous_state_rejected(self):
        """Отклоняет чужой selector, неверную границу и отсутствующую SQLite."""
        cases = ("foreign", "unknown", "boundary", "missing", "invalid-json", "no-boundary")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                _prepare(root)
                state = json.loads((root / "state.json").read_text())
                pointer = {"transition": "test", "database": OLD}
                if case == "foreign":
                    pointer["transition"] = "other"
                elif case == "unknown":
                    pointer["database"] = "unknown.db"
                elif case == "boundary":
                    state["admission"] = True
                elif case == "missing":
                    (root / OLD).unlink()
                elif case == "no-boundary":
                    del state["admission"]
                _publish(root / "state.json", state)
                _publish(root / "active-db.json", pointer)
                if case == "invalid-json":
                    (root / "active-db.json").write_text("{")
                with self.assertRaises(ValueError):
                    _resolve(root)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "wal":
        _wal(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "transition":
        _transition(Path(sys.argv[2]), sys.argv[3])
    else:
        print(f"Python {sys.version.split()[0]}; SQLite {sqlite3.sqlite_version}; os={os.name}",
              flush=True)
        unittest.main(verbosity=2)
