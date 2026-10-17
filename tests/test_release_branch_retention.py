"""Проверки правила хранения релизных линий на синтетических снимках."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from scripts.release_branch_retention import RetentionError, plan_retention, revalidate_candidate


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40


def fixture() -> dict:
    """Строит полный тестовый снимок с шестью прежними линиями."""
    return {
        "repository": "karateka-git/compadre-assistant",
        "server": {"version": "v2.0.3", "stage": "success",
                   "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
                   "marker_absent": True},
        "branches": [{"name": f"release/1.{minor}", "sha": SHA} for minor in range(6)]
                    + [{"name": "release/2.0", "sha": SHA}],
        "supported": [], "open_prs": [], "pending": [], "rollback": [],
        "protected_tags": ["v2.0.3"],
        "complete": {key: True for key in (
            "server", "branches", "supported", "open_prs", "pending", "rollback",
            "protected_tags")},
    }


class ReleaseBranchRetentionTest(unittest.TestCase):
    """Проверяет сохранение линий и закрытый отказ на плохих данных."""

    def test_more_than_three_old_lines(self) -> None:
        """Оставляет активную и три непосредственно предшествующие линии."""
        result = plan_retention(fixture(), now=NOW)
        self.assertEqual(result.retained, ("release/2.0", "release/1.5", "release/1.4", "release/1.3"))
        self.assertEqual(result.candidates, ("release/1.2", "release/1.1", "release/1.0"))

    def test_open_pr_supported_pending_and_rollback(self) -> None:
        """Исключает из кандидатов все линии с отдельным основанием хранения."""
        data = fixture()
        data["open_prs"] = [{"number": 12,
                            "base": {"repository": data["repository"], "ref": "release/1.2"},
                            "head": {"repository": "other/fork", "ref": "topic"}}]
        data["supported"] = ["release/1.1"]
        data["rollback"] = ["release/1.0"]
        data["pending"] = ["release/1.0"]
        self.assertEqual(plan_retention(data, now=NOW).candidates, ())

    def test_open_pr_local_head_with_main_base(self) -> None:
        """Не предлагает удалить head локального PR с base в main."""
        data = fixture()
        data["open_prs"] = [{"number": 13,
                            "base": {"repository": data["repository"], "ref": "main"},
                            "head": {"repository": data["repository"], "ref": "release/1.2"}}]
        self.assertNotIn("release/1.2", plan_retention(data, now=NOW).candidates)

    def test_open_pr_local_head_with_other_release_base(self) -> None:
        """Сохраняет оба конца PR между релизными линиями проекта."""
        data = fixture()
        data["open_prs"] = [{"number": 14,
                            "base": {"repository": data["repository"], "ref": "release/1.1"},
                            "head": {"repository": data["repository"], "ref": "release/1.2"}}]
        result = plan_retention(data, now=NOW)
        self.assertNotIn("release/1.1", result.candidates)
        self.assertNotIn("release/1.2", result.candidates)

    def test_fork_head_does_not_protect_same_named_local_ref(self) -> None:
        """Имя head чужого fork не считается ref нашего репозитория."""
        data = fixture()
        data["open_prs"] = [{"number": 15,
                            "base": {"repository": data["repository"], "ref": "main"},
                            "head": {"repository": "other/fork", "ref": "release/1.2"}}]
        self.assertIn("release/1.2", plan_retention(data, now=NOW).candidates)

    def test_local_head_repository_case_is_insensitive(self) -> None:
        """Различие регистра имени GitHub не превращает локальный head в fork."""
        data = fixture()
        data["open_prs"] = [{"number": 18,
                            "base": {"repository": data["repository"], "ref": "main"},
                            "head": {"repository": data["repository"].upper(), "ref": "release/1.2"}}]
        self.assertNotIn("release/1.2", plan_retention(data, now=NOW).candidates)

    def test_newer_line_is_retained(self) -> None:
        """Сохраняет линию новее активной, ожидающую развёртывания."""
        data = fixture()
        data["server"]["version"] = "v1.5.0"
        data["protected_tags"] = ["v1.5.0"]
        self.assertIn("release/2.0", plan_retention(data, now=NOW).retained)

    def test_stale_and_future_server_snapshots(self) -> None:
        """Отвергает просроченное и опережающее локальное время состояние VPS."""
        for offset in (timedelta(minutes=-6), timedelta(seconds=1)):
            with self.subTest(offset=offset):
                data = fixture()
                data["server"]["observed_at"] = (NOW + offset).isoformat()
                with self.assertRaises(RetentionError):
                    plan_retention(data, now=NOW)

    def test_pending_or_degraded_server(self) -> None:
        """Не строит кандидатов при незавершённом выпуске и rollback."""
        for stage in ("preparing", "rollback_started", "rolled_back", "degraded"):
            with self.subTest(stage=stage):
                data = fixture()
                data["server"]["stage"] = stage
                with self.assertRaises(RetentionError):
                    plan_retention(data, now=NOW)

    def test_missing_or_incomplete_sources(self) -> None:
        """Любой отсутствующий источник останавливает планирование."""
        for key in ("branches", "open_prs", "rollback", "pending", "supported", "protected_tags"):
            with self.subTest(key=key):
                data = fixture()
                del data[key]
                with self.assertRaises(RetentionError):
                    plan_retention(data, now=NOW)
        data = fixture()
        data["complete"]["open_prs"] = False
        with self.assertRaises(RetentionError):
            plan_retention(data, now=NOW)

    def test_incomplete_or_unmatched_pr_fails_closed(self) -> None:
        """Потерянный head и неизвестный локальный ref останавливают план."""
        data = fixture()
        data["open_prs"] = [{"number": 16,
                            "base": {"repository": data["repository"], "ref": "main"}}]
        with self.assertRaises(RetentionError):
            plan_retention(data, now=NOW)
        data["open_prs"][0]["head"] = {"repository": data["repository"], "ref": "release/9.9"}
        with self.assertRaises(RetentionError):
            plan_retention(data, now=NOW)

    def test_api_race_replan_changes_candidates(self) -> None:
        """Новый PR или сдвиг ref отменяет прежнее решение при повторной сверке."""
        before = fixture()
        after = deepcopy(before)
        after["open_prs"] = [{"number": 17,
                             "base": {"repository": before["repository"], "ref": "main"},
                             "head": {"repository": before["repository"], "ref": "release/1.2"}}]
        with self.assertRaises(RetentionError):
            revalidate_candidate(before, after, "release/1.2", now=NOW)
        after = deepcopy(before)
        after["branches"][2]["sha"] = "b" * 40
        with self.assertRaises(RetentionError):
            revalidate_candidate(before, after, "release/1.2", now=NOW)

    def test_unchanged_snapshot_can_be_revalidated(self) -> None:
        """Новое время чтения VPS допустимо при неизменном полезном состоянии."""
        before = fixture()
        after = deepcopy(before)
        after["server"]["observed_at"] = NOW.isoformat()
        self.assertEqual(revalidate_candidate(before, after, "release/1.2", now=NOW), SHA)

    def test_revalidation_requires_new_fresh_server_snapshot(self) -> None:
        """Повторное чтение должно быть новее, а исходный план ещё свежим."""
        before = fixture()
        with self.assertRaises(RetentionError):
            revalidate_candidate(before, deepcopy(before), "release/1.2", now=NOW)
        older = deepcopy(before)
        older["server"]["observed_at"] = (NOW - timedelta(minutes=2)).isoformat()
        with self.assertRaises(RetentionError):
            revalidate_candidate(before, older, "release/1.2", now=NOW)
        after = deepcopy(before)
        after["server"]["observed_at"] = NOW.isoformat()
        with self.assertRaises(RetentionError):
            revalidate_candidate(before, after, "release/1.2", now=NOW + timedelta(minutes=5))

    def test_active_branch_or_tag_missing(self) -> None:
        """Не принимает неполное доказательство активного выпуска."""
        data = fixture()
        data["branches"] = data["branches"][:-1]
        with self.assertRaises(RetentionError):
            plan_retention(data, now=NOW)
        data = fixture()
        data["protected_tags"] = []
        with self.assertRaises(RetentionError):
            plan_retention(data, now=NOW)


if __name__ == "__main__":
    unittest.main()
