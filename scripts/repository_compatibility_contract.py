"""Проверяет доверенную политику и доказательство SQLite-совместимости."""

from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


POLICY_PATH = Path(__file__).with_name("repository_compatibility_policy.json")
# V2 закрепляет только политику проверок. База хранится в Deployments.
DYNAMIC_POLICY_VERSION = 2
FULL_DISCOVERY_POLICY_VERSION = 3
_FULL_DISCOVERY_FIELDS = {"version", "repository"}
_MATRIX_FIELDS = {"version", "repository", "matrix", "test_ids"}
_LEGACY_BASELINE_FIELDS = {"previous_sha", "release_tag", "release_id"}


def load_policy(path: Path = POLICY_PATH) -> dict:
    """Читает политику из доверенного checkout, включая опубликованные форматы.

    Args:
        path: Файл политики из доверенного коммита.

    Returns:
        Поля политики соответствующей версии.

    Raises:
        ValueError: Формат политики повреждён либо версия неизвестна.
    """
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or type(policy.get("version")) is not int:
        raise ValueError("unknown compatibility policy")
    version = policy["version"]
    if version == FULL_DISCOVERY_POLICY_VERSION:
        repository = policy.get("repository")
        if (policy.keys() != _FULL_DISCOVERY_FIELDS or not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)):
            raise ValueError("invalid full-discovery compatibility policy")
        return policy
    if ((version == 1 and policy.keys() != _MATRIX_FIELDS | _LEGACY_BASELINE_FIELDS)
            or (version == DYNAMIC_POLICY_VERSION and policy.keys() != _MATRIX_FIELDS)
            or version not in (1, DYNAMIC_POLICY_VERSION)):
        raise ValueError("unknown compatibility policy")
    rows = policy["matrix"]
    keys = {(row["source"], row["class"], row["method"]) for row in rows}
    ids = policy["test_ids"]
    # Ранее опубликованные теги сохраняют evidence с прежним составом тестов.
    supported_counts = (173,) if version == 1 else (173, 177, 179)
    if (len(rows) != 107 or len(keys) != 107
            or len({row["class"] for row in rows}) != 26
            or len(ids) not in supported_counts or len(set(ids)) != len(ids)
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


def _verify_test_execution(result: dict, *, allow_skips: bool) -> set[str]:
    if not isinstance(result, dict):
        raise ValueError("invalid compatibility test result")
    discovered, started = result.get("test_ids"), result.get("started_test_ids")
    if any(not isinstance(ids, list) or not ids
           or any(not isinstance(test_id, str) or not test_id for test_id in ids)
           or len(set(ids)) != len(ids) for ids in (discovered, started)):
        raise ValueError("invalid compatibility test IDs")
    started_ids = set(started)
    if (result.get("passed") is not True or type(result.get("tests_run")) is not int
            or result["tests_run"] != len(discovered) or started_ids != set(discovered)
            or any(result.get(key) != [] for key in
                   ("loader_errors", "failures", "errors", "unexpected_successes"))):
        raise ValueError("incomplete or unsuccessful compatibility test execution")
    skips, expected_failures = result.get("skips"), result.get("expected_failures")
    if (not isinstance(skips, list) or not isinstance(expected_failures, list)
            or not allow_skips and (skips or expected_failures)):
        raise ValueError("invalid compatibility test skips or expected failures")
    for skip in skips:
        # unittest добавляет к ID родительского теста описание пропущенного subTest.
        if not isinstance(skip, list) or len(skip) != 2 or any(not isinstance(value, str) for value in skip):
            raise ValueError("invalid compatibility test skip")
        skipped_id = skip[0]
        if skipped_id not in started_ids and not any(skipped_id.startswith(f"{test_id} ") for test_id in started_ids):
            raise ValueError("unknown skipped compatibility test")
    if any(not isinstance(test_id, str) or test_id not in started_ids for test_id in expected_failures):
        raise ValueError("unknown expected compatibility test failure")
    return started_ids


def verify_behavior_tests(behavior: dict, policy: dict) -> None:
    """Проверяет состав и выполнение тестов принятой версии и отдельного набора.

    Args:
        behavior: Отчёт worker, выполнявшего старый код на новой схеме.
        policy: Версия доверенной политики, определяющая формат отчёта.

    Raises:
        ValueError: Набор пуст, неполон, неуспешен либо использует чужие ID.
        KeyError: В отчёте отсутствует обязательный набор.
    """
    if policy["version"] != FULL_DISCOVERY_POLICY_VERSION:
        verify_tests(behavior["old_tests"], policy["test_ids"])
        return

    discovered = _verify_test_execution(behavior["old_tests"], allow_skips=True)
    additional = _verify_test_execution(behavior["additional_tests"], allow_skips=False)
    if discovered & additional or any(not test_id.startswith("repository_compatibility_cases.")
                                      for test_id in additional):
        raise ValueError("additional compatibility tests differ from trusted suite")


def verify_report(report: dict, policy: dict, *, previous_sha: str | None = None) -> None:
    """Проверяет обязательные наборы, обмен, отпечаток схемы и исходники.

    Args:
        report: Полный runner report, ещё не считающийся доверенным admission.
        policy: Доверенная политика формата доказательства.
        previous_sha: Подтверждённый SHA принятой базы для динамической политики.

    Raises:
        ValueError: Результаты, sources, SQL либо imports не согласованы.
        KeyError: В отчёте отсутствует обязательное свидетельство.
    """
    expected_sha = policy.get("previous_sha") if previous_sha is None else previous_sha
    if not expected_sha or report.get("previous_sha") != expected_sha:
        raise ValueError("baseline differs from verified previous SHA")
    if report.get("passed") is not True or report.get("source_verification") != "passed":
        raise ValueError("compatibility runner did not verify sources/results")
    if report.get("source_changes") != {"previous": [], "candidate": []}:
        raise ValueError("source changes detected")
    full_discovery = policy["version"] == FULL_DISCOVERY_POLICY_VERSION
    if full_discovery:
        previous_git = report.get("previous_git")
        if (not isinstance(previous_git, dict) or previous_git.keys() != {"head", "index_tree"}
                or previous_git["head"] != expected_sha
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
                       for value in previous_git.values())):
            raise ValueError("previous Git identity evidence mismatch")
    candidate = report["candidate_migrations_data_integrity"]
    behavior = report["old_repository_behavior"]
    for name in ("previous_seed", "candidate_migrations_data_integrity",
                 "old_repository_behavior", "candidate_verified_previous_updates"):
        worker = report[name]
        if worker.get("passed") is not True or not worker.get("imports"):
            raise ValueError(f"missing worker/import evidence: {name}")
        if full_discovery and (type(worker.get("returncode")) is not int or worker["returncode"] != 0):
            raise ValueError(f"worker did not complete successfully: {name}")
    if full_discovery:
        _verify_test_execution(candidate["candidate_tests"], allow_skips=False)
    else:
        verify_tests(candidate["candidate_tests"])
    verify_behavior_tests(behavior, policy)
    fingerprint = candidate["fresh_fingerprint"]
    if full_discovery and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                           or type(behavior.get("candidate_schema_observations")) is not int
                           or behavior.get("schema_errors") != []):
        raise ValueError("invalid candidate schema evidence")
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
