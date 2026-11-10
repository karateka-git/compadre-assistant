"""Проверяет закреплённую policy, матрицу и результаты SQLite-совместимости."""

from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath, PureWindowsPath


POLICY_PATH = Path(__file__).with_name("repository_compatibility_policy.json")


def load_policy(path: Path = POLICY_PATH) -> dict:
    """Читает policy из доверенного checkout, отвергая неполную матрицу."""
    policy = json.loads(path.read_text(encoding="utf-8"))
    rows = policy["matrix"]
    keys = {(row["source"], row["class"], row["method"]) for row in rows}
    ids = policy["test_ids"]
    if (policy["version"] != 1 or len(rows) != 107 or len(keys) != 107
            or len({row["class"] for row in rows}) != 26
            or len(ids) != 173 or len(set(ids)) != len(ids)
            or any(row["test_id"] not in ids for row in rows)):
        raise ValueError("incomplete compatibility policy/matrix")
    return policy


def verify_inventory(checkout: Path, policy: dict) -> None:
    """Сверяет все публичные методы закреплённых SQLite-классов с матрицей.

    Args:
        checkout: Точный previous export до исполнения Python.
        policy: Проверенная policy выбранной базы.

    Raises:
        ValueError: Источник, класс или публичная операция не совпадают с policy.
    """
    expected = {(r["source"], r["class"], r["method"]) for r in policy["matrix"]}
    actual = set()
    for source in {r["source"] for r in policy["matrix"]}:
        tree = ast.parse((checkout / source).read_text(encoding="utf-8"))
        classes = {r["class"] for r in policy["matrix"] if r["source"] == source}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in classes:
                actual.update((source, node.name, method.name) for method in node.body
                              if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and not method.name.startswith("_"))
    if actual != expected:
        raise ValueError("previous public API differs from policy matrix")


def verify_tests(result: dict, expected_ids: list[str] | None = None) -> None:
    """Отклоняет skips, пустую выборку, дубликаты и неполный manifest tests.

    Args:
        result: Отчёт одного unittest набора из isolated worker.
        expected_ids: Полный закреплённый список для previous behavior;
            candidate migration suite проверяется без фиксированного списка.

    Raises:
        ValueError: Набор неполон либо содержит неуспешный результат.
    """
    ids = result.get("test_ids", [])
    if (result.get("passed") is not True or not ids or len(set(ids)) != len(ids)
            or result.get("tests_run") != len(ids)
            or any(result.get(key) != [] for key in ("skips", "failures", "errors"))
            or result.get("expected_failures", []) or result.get("unexpected_successes", [])
            or expected_ids is not None and sorted(ids) != sorted(expected_ids)):
        raise ValueError("incomplete or unsuccessful compatibility tests")


def verify_report(report: dict, policy: dict) -> None:
    """Проверяет оба обязательных набора, обмен, schema fingerprint и sources.

    Args:
        report: Полный runner report, ещё не считающийся доверенным admission.
        policy: Manifest закреплённой previous базы и всех её tests.

    Raises:
        ValueError: Результаты, sources, SQL либо imports не согласованы.
        KeyError: В отчёте отсутствует обязательное свидетельство.
    """
    if report.get("previous_sha") != policy["previous_sha"]:
        raise ValueError("baseline differs from trusted policy")
    if report.get("passed") is not True or report.get("source_verification") != "passed":
        raise ValueError("compatibility runner did not verify sources/results")
    if report.get("source_changes") != {"previous": [], "candidate": []}:
        raise ValueError("source changes detected")
    candidate = report["candidate_migrations_data_integrity"]
    behavior = report["old_repository_behavior"]
    for name in ("previous_seed", "candidate_migrations_data_integrity",
                 "old_repository_behavior", "candidate_verified_previous_updates"):
        if report[name].get("passed") is not True or not report[name].get("imports"):
            raise ValueError(f"missing worker/import evidence: {name}")
    verify_tests(candidate["candidate_tests"])
    verify_tests(behavior["old_tests"], policy["test_ids"])
    fingerprint = candidate["fresh_fingerprint"]
    if (candidate["upgraded_fingerprint"] != fingerprint
            or behavior["schema_fingerprints"] != [fingerprint]
            or report["candidate_verified_previous_updates"]["schema_fingerprint"] != fingerprint
            or behavior["candidate_schema_observations"] <= 0 or behavior["schema_errors"]):
        raise ValueError("candidate schema evidence mismatch")
    sql_prefix = "compadre_assistant/core/data/sqlite/migrations/"
    old, new = report["sources"]["previous"], report["sources"]["candidate"]
    overlay = {name: digest for name, digest in old.items()
               if not (name.startswith(sql_prefix) and name.endswith(".sql"))}
    overlay.update({name: digest for name, digest in new.items()
                    if name.startswith(sql_prefix) and name.endswith(".sql")})
    if not overlay or overlay != report["previous_with_candidate_sql"]:
        raise ValueError("SQL/source overlay mismatch")
    for name, source in (("previous_seed", old), ("old_repository_behavior", overlay),
                         ("candidate_migrations_data_integrity", new),
                         ("candidate_verified_previous_updates", new)):
        worker = report[name]
        checkout_origin = worker["environment"]["sys_path"][0]
        # Пути в артефакте принадлежат runner, а не машине оператора.
        checkout = PureWindowsPath(checkout_origin)
        if not checkout.is_absolute():
            checkout = PurePosixPath(checkout_origin)
        if not checkout.is_absolute() or ".." in checkout.parts:
            raise ValueError("invalid import checkout path")
        for origin in worker["imports"].values():
            path = type(checkout)(origin)
            if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(checkout):
                raise ValueError("import origin escaped checkout")
            if path.relative_to(checkout).as_posix() not in source:
                raise ValueError("import origin missing from exact Git source")
