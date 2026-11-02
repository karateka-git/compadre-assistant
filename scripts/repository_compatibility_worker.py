"""Внешний worker: импортирует только выбранный точный checkout и его tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import sys
import sysconfig
from tempfile import TemporaryDirectory
import threading
import traceback
import unittest


class CheckoutImports(importlib.abc.MetaPathFinder):
    """Не допускает загрузку project/tests/helpers из installed или чужого checkout."""

    def __init__(self, checkout: Path) -> None:
        """Запоминает корни исходников и имена прежних helpers до первого импорта."""
        self.checkout = checkout
        self.names = {"compadre_assistant", "document_chunker", "tests", "scripts"}
        self.names.update(p.stem for p in (checkout / "tests").glob("*.py"))
        self.origins: dict[str, str] = {}

    def check(self, name: str, origin: str | None) -> None:
        """Проверяет фактический origin контролируемого модуля.

        Raises:
            ImportError: Модуль разрешился вне выбранного checkout.
        """
        if name.split(".")[0] not in self.names:
            return
        if not origin or not Path(origin).resolve().is_relative_to(self.checkout):
            raise ImportError(f"import leakage: {name}: {origin}")
        self.origins[name] = str(Path(origin).resolve())

    def find_spec(self, fullname: str, path=None, target=None):
        """Проверяет origin до исполнения Python модуля выбранного проекта."""
        if fullname.split(".")[0] not in self.names:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            raise ImportError(f"missing checkout module: {fullname}")
        # Namespace packages допустимы только с каталогами этого checkout.
        if spec.origin is None and spec.submodule_search_locations:
            if any(not Path(p).resolve().is_relative_to(self.checkout) for p in spec.submodule_search_locations):
                raise ImportError(f"namespace import leakage: {fullname}")
        else:
            self.check(fullname, spec.origin)
        return spec

    def verify_loaded(self) -> None:
        """Повторяет проверку реальных __file__ после тестов, включая helpers."""
        for name, module in tuple(sys.modules.items()):
            if getattr(module, "__file__", None):
                self.check(name, module.__file__)


def assert_database(connection: sqlite3.Connection, names: list[str]) -> str:
    """Проверяет журнал, FK/integrity и возвращает fingerprint текущей схемы."""
    actual = [row[0] for row in connection.execute("SELECT name FROM schema_migrations ORDER BY version")]
    if actual != names:
        raise AssertionError(f"migration journal mismatch: {actual} != {names}")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise AssertionError("integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise AssertionError("foreign_key_check failed")
    schema = [tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name")]
    return hashlib.sha256(json.dumps(schema).encode()).hexdigest()


def run_tests(modules: list[str]) -> dict:
    """Исполняет неизменные unittest и сохраняет IDs и раздельные ошибки."""
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)

    def ids(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                yield from ids(test)
            else:
                yield test.id()

    test_ids = list(ids(suite))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return {"passed": result.wasSuccessful() and result.testsRun > 0 and not result.skipped,
            "tests_run": result.testsRun, "test_ids": test_ids,
            "failures": [test.id() for test, _ in result.failures],
            "errors": [test.id() for test, _ in result.errors],
            "skips": [(test.id(), reason) for test, reason in result.skipped]}


def seed_database(database: Path) -> dict:
    """Создаёт минимальный upgrade fixture прежним API на прежних миграциях."""
    from compadre_assistant.core.data.sqlite.connection import connect_database
    from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations
    from compadre_assistant.core.data.sqlite.user_identity_repository import SQLiteAppUserRepository

    with connect_database(database) as connection:
        apply_migrations(connection)
        # Полнота fixtures всех модулей добавляется в 5.3, этот seed явно минимален.
        user = SQLiteAppUserRepository(connection).create(display_name="Compatibility fixture")
        fingerprint = assert_database(connection, sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql")))
    return {"passed": True, "fixture_scope": "single app_user", "user_id": user.id,
            "schema_fingerprint": fingerprint}


def candidate_migrations(database: Path, previous_sql: Path) -> dict:
    """Проверяет fresh/upgrade, точные SQL bytes, журнал и минимальный seed."""
    from compadre_assistant.core.data.sqlite.connection import connect_database
    from compadre_assistant.core.data.sqlite.migration_runner import MIGRATIONS_DIR, apply_migrations

    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    previous_names = sorted(p.name for p in previous_sql.glob("*.sql"))
    if not previous_names or names[:len(previous_names)] != previous_names:
        raise AssertionError("previous SQL is not candidate prefix")
    for name in previous_names:
        if (previous_sql / name).read_bytes() != (MIGRATIONS_DIR / name).read_bytes():
            raise AssertionError(f"released SQL bytes changed: {name}")
    with TemporaryDirectory(prefix="candidate-fresh-") as temporary:
        with connect_database(Path(temporary) / "fresh.db") as connection:
            apply_migrations(connection)
            fingerprint = assert_database(connection, names)
            if apply_migrations(connection):
                raise AssertionError("fresh migrations are not idempotent")
    with connect_database(database) as connection:
        assert_database(connection, previous_names)
        before = [tuple(row) for row in connection.execute("SELECT id, display_name FROM app_users")]
        if len(before) != 1 or before[0][1] != "Compatibility fixture":
            raise AssertionError("previous seed missing")
        completed = apply_migrations(connection)
        if [m.name for m in completed] != names[len(previous_names):]:
            raise AssertionError("unexpected pending migrations")
        upgraded_fingerprint = assert_database(connection, names)
        after = [tuple(row) for row in connection.execute("SELECT id, display_name FROM app_users")]
        if before != after or apply_migrations(connection):
            raise AssertionError("upgrade lost fixture or is not idempotent")
    tests = run_tests(["tests.test_migration_runner"])
    return {"passed": tests["passed"], "fixture_scope": "single app_user; full coverage pending 5.3",
            "fresh_fingerprint": fingerprint, "upgraded_fingerprint": upgraded_fingerprint,
            "migration_names": names, "candidate_tests": tests}


def old_behavior(modules: list[str]) -> dict:
    """Наблюдает реальный старый apply_migrations, не заменяя его или assertions."""
    from compadre_assistant.core.data.sqlite import migration_runner

    names = sorted(p.name for p in migration_runner.MIGRATIONS_DIR.glob("*.sql"))
    fingerprints: set[str] = set()
    failures: list[str] = []
    calls = 0

    def observe(frame, event, arg):
        nonlocal calls
        if event != "return" or frame.f_code is not migration_runner.apply_migrations.__code__:
            return
        if Path(frame.f_locals["migrations_dir"]).resolve() != migration_runner.MIGRATIONS_DIR.resolve():
            return
        try:
            fingerprints.add(assert_database(frame.f_locals["connection"], names))
            calls += 1
        except Exception as error:
            failures.append(str(error))

    sys.setprofile(observe)
    threading.setprofile(observe)
    try:
        tests = run_tests(modules)
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
    return {"passed": tests["passed"] and calls > 0 and not failures,
            "old_tests": tests, "candidate_schema_observations": calls,
            "schema_fingerprints": sorted(fingerprints), "schema_errors": failures,
            "migration_names": names, "coverage": "audited 5.1 subset; not full operation matrix"}


def main() -> int:
    """Настраивает изолированный import path до первого импорта проекта/tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--mode", choices=("seed", "candidate", "behavior"), required=True)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--previous-sql", type=Path)
    parser.add_argument("--test-modules", nargs="+", default=[])
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    result = {"passed": False}
    guard = CheckoutImports(checkout)
    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode or sys.prefix == sys.base_prefix:
            raise RuntimeError("worker requires an isolated venv and -I -B")
        configuration = Path(sys.prefix) / "pyvenv.cfg"
        if "include-system-site-packages = false" not in configuration.read_text(encoding="utf-8").lower():
            raise RuntimeError("system site packages are forbidden")
        environment = Path(sys.prefix).resolve()
        base = Path(sys.base_prefix).resolve()
        standard_library = Path(sysconfig.get_path("stdlib")).resolve()
        for entry in sys.path:
            path = Path(entry).resolve()
            allowed = (path.is_relative_to(environment) or path == base
                       or path.is_relative_to(standard_library)
                       or path == base / "DLLs"
                       or path.name == f"python{sys.version_info.major}{sys.version_info.minor}.zip"
                       and path.parent in {base, standard_library.parent})
            if not entry or not allowed or ("site-packages" in path.parts and not path.is_relative_to(environment)):
                raise RuntimeError(f"unexpected initial import path: {entry}")
        # -I исключает cwd/PYTHONPATH/user site; добавляем только выбранные исходники.
        sys.path[:0] = [str(checkout), str(checkout / "tests")]
        expected_path = list(sys.path)
        sys.meta_path.insert(0, guard)
        guard.verify_loaded()
        if args.mode == "seed":
            result = seed_database(args.database)
        elif args.mode == "candidate":
            result = candidate_migrations(args.database, args.previous_sql)
        else:
            result = old_behavior(args.test_modules)
        guard.verify_loaded()
        if sys.path != expected_path:
            raise RuntimeError("tests changed isolated import path")
    except Exception as error:
        traceback.print_exc()
        result = {"passed": False, "error": str(error)}
    result["environment"] = {"executable": sys.executable, "prefix": sys.prefix,
                             "python": sys.version, "sqlite": sqlite3.sqlite_version,
                             "dependencies": sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()),
                             "sys_path": sys.path}
    result["imports"] = guard.origins
    args.result.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
