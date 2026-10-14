"""Проверяет последовательность версии и безопасную публикацию без сети."""

from __future__ import annotations

import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from scripts import publish_release as publisher
from scripts.full_ci_admission import Admission, MAIN_WORKFLOW, RELEASE_WORKFLOW


SHA = "a" * 40
OLD = "b" * 40


class FakeApi:
    """Возвращает управляемую историю GitHub и записывает мутации."""

    repository = publisher.EXPECTED_REPOSITORY

    def __init__(self) -> None:
        """Создаёт пустую историю выпусков и включённую защиту."""
        self.releases: list[dict] = []
        self.tags: list[dict] = []
        self.immutable = True
        self.rules: list[dict] = [{"id": 7, "target": "tag", "enforcement": "active"}]
        self.rule: dict = {
            "target": "tag", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [{"type": "update"}, {"type": "deletion"}],
        }
        self.posts: list[tuple[str, dict]] = []
        self.move_after_tag = False

    def get(self, path, **parameters):
        """Возвращает настройку защиты или текущий ref ветки."""
        if path == "/immutable-releases":
            return {"enabled": self.immutable}
        if path == "/rulesets/7":
            return self.rule
        if path == "/git/ref/heads/release/1.0":
            return {"object": {"sha": OLD if self.move_after_tag and self.posts else SHA}}
        if path == "/releases/tags/v1.0.0":
            return {"id": 42, "tag_name": "v1.0.0", "target_commitish": SHA,
                    "body": self.posts[-1][1]["body"], "draft": False,
                    "prerelease": False, "published_at": "2026-10-14T00:00:00Z"}
        raise AssertionError(path)

    def pages(self, path, key=None, **parameters):
        """Возвращает историю выпусков, тегов или rulesets."""
        if path == "/releases":
            return self.releases
        if path == "/git/matching-refs/tags/v":
            return self.tags
        if path == "/rulesets":
            return self.rules
        raise AssertionError(path)

    def post(self, path, payload):
        """Имитирует атомарное создание ref и GitHub Release."""
        self.posts.append((path, payload))
        if path == "/git/refs":
            if self.tags:
                raise publisher.PublishError("HTTP 422")
            self.tags.append({"ref": payload["ref"]})
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        if path == "/releases":
            self.releases.append({"tag_name": payload["tag_name"]})
            return {"id": 42, "tag_name": payload["tag_name"], "draft": False,
                    "prerelease": False, "html_url": "https://github.com/release"}
        raise AssertionError(path)


class PublishTests(unittest.TestCase):
    """Проверяет допуск, защиту и частичные отказы записи."""

    def setUp(self) -> None:
        """Подменяет локальный checkout и selector CI."""
        self.api = FakeApi()
        self.candidate = patch.object(publisher, "_candidate")
        self.candidate.start()
        self.addCleanup(self.candidate.stop)
        self.main = patch.object(publisher, "checked_main_head", return_value=SHA)
        self.main.start()
        self.addCleanup(self.main.stop)
        self.hotfix = patch.object(publisher, "check_release", return_value=[(10, 11, "same patch")])
        self.hotfix.start()
        self.addCleanup(self.hotfix.stop)
        self.tag = patch.object(publisher, "tag_commit", return_value=SHA)
        self.tag.start()
        self.addCleanup(self.tag.stop)
        self.admission = patch.object(publisher, "select_successful_run", side_effect=self.admit)
        self.admission.start()
        self.addCleanup(self.admission.stop)

    def admit(self, api, repository, workflow, sha, branch):
        """Создаёт положительный результат для запрошенного caller."""
        return Admission(repository, workflow, branch, sha, 123, 1)

    def test_initial_release_reuses_exact_main_full(self) -> None:
        """Первый выпуск получает main-full, notes и ссылку на CI."""
        result = publisher.prepare(self.api, "v1.0.0", SHA, "initial", "Изменения")
        self.assertEqual(result.admission.workflow, MAIN_WORKFLOW)
        self.assertEqual(result.make_latest, "true")
        self.assertIn("/actions/runs/123/attempts/1", result.body)
        self.assertEqual(self.api.posts, [])

    def test_patch_requires_pair_and_release_full(self) -> None:
        """Следующий PATCH вызывает проверку пары и release-full."""
        self.api.releases = [{"tag_name": "v1.0.0", "published_at": "2026-10-14T00:00:00Z"}]
        with patch.object(publisher, "tag_commit", return_value=OLD), \
             patch.object(publisher, "check_release", return_value=[(10, 11, "same")]) as check:
            result = publisher.prepare(self.api, "v1.0.1", SHA, "patch", "Исправление")
        check.assert_called_once_with(self.api, "release/1.0", SHA, OLD)
        self.assertEqual(result.admission.workflow, RELEASE_WORKFLOW)
        self.assertEqual(result.make_latest, "true")
        self.assertIn("release #10, main #11", result.body)

    def test_version_sequence_and_occupied_names(self) -> None:
        """Пропуск PATCH и повторный тег/Release отвергаются."""
        self.api.releases = [{"tag_name": "v1.0.0", "published_at": "2026-10-14T00:00:00Z"}]
        with self.assertRaisesRegex(publisher.PublishError, "следующий PATCH"):
            publisher.prepare(self.api, "v1.0.2", SHA, "patch", "Исправление")
        self.api.tags = [{"ref": "refs/tags/v1.0.1"}]
        with self.assertRaisesRegex(publisher.PublishError, "Тег уже существует"):
            publisher.prepare(self.api, "v1.0.1", SHA, "patch", "Исправление")
        self.api.tags = []
        self.api.releases.append({"tag_name": "v1.0.1", "draft": True})
        with self.assertRaisesRegex(publisher.PublishError, "уже существует"):
            publisher.prepare(self.api, "v1.0.1", SHA, "patch", "Исправление")

    def test_missing_protection_or_ci_closes_admission(self) -> None:
        """Отсутствие immutable, tag ruleset или CI не допускает запись."""
        self.api.immutable = False
        with self.assertRaisesRegex(publisher.PublishError, "Immutable"):
            publisher.prepare(self.api, "v1.0.0", SHA, "initial", "Изменения")
        self.api.immutable = True
        self.api.rule["bypass_actors"] = [{"actor_type": "RepositoryRole"}]
        with self.assertRaisesRegex(publisher.PublishError, "tag ruleset"):
            publisher.prepare(self.api, "v1.0.0", SHA, "initial", "Изменения")
        self.api.rule["bypass_actors"] = []
        with patch.object(publisher, "select_successful_run", return_value=None):
            with self.assertRaisesRegex(publisher.PublishError, "полного CI"):
                publisher.prepare(self.api, "v1.0.0", SHA, "initial", "Изменения")

    def test_patch_of_older_line_does_not_become_latest(self) -> None:
        """Поддерживаемый старый PATCH не заменяет новую линию как latest."""
        self.api.releases = [
            {"tag_name": "v1.0.0", "published_at": "2026-10-13T00:00:00Z"},
            {"tag_name": "v1.1.0", "published_at": "2026-10-14T00:00:00Z"},
        ]
        result = publisher.prepare(self.api, "v1.0.1", SHA, "patch", "Исправление")
        self.assertEqual(result.make_latest, "false")

    def test_publish_creates_tag_then_release_and_rechecks(self) -> None:
        """Запись идёт после повторного допуска и сверки точного тега."""
        prepared = publisher.prepare(self.api, "v1.0.0", SHA, "initial", "Изменения")
        url = publisher.publish(self.api, prepared)
        self.assertEqual(url, "https://github.com/release")
        self.assertEqual([path for path, _ in self.api.posts], ["/git/refs", "/releases"])
        self.assertEqual(self.api.posts[0][1]["sha"], SHA)
        self.assertEqual(self.api.posts[1][1]["target_commitish"], SHA)

    def test_race_or_branch_move_stops_before_release(self) -> None:
        """Занятый ref и сдвиг ветки после тега не создают Release."""
        prepared = publisher.prepare(self.api, "v1.0.0", SHA, "initial", "Изменения")
        self.api.tags = [{"ref": "refs/tags/v1.0.0"}]
        with self.assertRaisesRegex(publisher.PublishError, "Тег уже существует"):
            publisher.publish(self.api, prepared)
        self.assertEqual(self.api.posts, [])
        self.api.tags = []
        self.api.move_after_tag = True
        with patch.object(publisher, "_candidate", side_effect=[None, None, publisher.PublishError("tip")]):
            with self.assertRaisesRegex(publisher.PublishError, "tip"):
                publisher.publish(self.api, prepared)
        self.assertEqual([path for path, _ in self.api.posts], ["/git/refs"])

    def test_check_and_dry_run_never_post(self) -> None:
        """Оба режима просмотра проверяют допуск без создания объектов."""
        output = StringIO()
        with patch.object(publisher, "PublishingGitHub", return_value=self.api), \
             patch.object(publisher.Path, "read_text", return_value="Изменения"), \
             patch.object(publisher.sys, "argv", ["publish_release", "v1.0.0", SHA,
                                                   "--change", "initial", "--notes-file",
                                                   "notes.txt", "--check"]), \
             redirect_stdout(output):
            publisher.main()
        self.assertNotIn("Commit:", output.getvalue())
        output = StringIO()
        with patch.object(publisher, "PublishingGitHub", return_value=self.api), \
             patch.object(publisher.Path, "read_text", return_value="Изменения"), \
             patch.object(publisher.sys, "argv", ["publish_release", "v1.0.0", SHA,
                                                   "--change", "initial", "--notes-file",
                                                   "notes.txt", "--dry-run"]), \
             redirect_stdout(output):
            publisher.main()
        self.assertIn("Commit:", output.getvalue())
        self.assertEqual(self.api.posts, [])


if __name__ == "__main__":
    unittest.main()
