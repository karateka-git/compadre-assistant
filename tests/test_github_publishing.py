"""Проверяет HTTP-контракт общего клиента публикации GitHub."""

from __future__ import annotations

from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import github_publishing


class FakeResponse(BytesIO):
    """Возвращает JSON-ответ с выбранным HTTP-статусом."""

    def __init__(self, status: int, payload: dict) -> None:
        """Сохраняет статус и кодирует ответ GitHub API."""
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = status


class GitHubPublishingTests(unittest.TestCase):
    """Проверяет явный POST и отказ при неподтверждённом создании."""

    def test_post_uses_exact_path_payload_and_requires_201(self) -> None:
        """Клиент отправляет точный ref и принимает только HTTP 201."""
        client = github_publishing.PublishingGitHub("owner/repo", "test-token")
        payload = {"ref": "refs/heads/release/1.0", "sha": "a" * 40}
        with patch.object(github_publishing, "urlopen", return_value=FakeResponse(201, payload)) as open_url:
            self.assertEqual(client.post("/git/refs", payload), payload)
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/owner/repo/git/refs")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), payload)
        with patch.object(github_publishing, "urlopen", return_value=FakeResponse(200, payload)):
            with self.assertRaises(github_publishing.PublishingError):
                client.post("/git/refs", payload)

    def test_conflict_does_not_return_success(self) -> None:
        """HTTP 422 закрывает публикацию ошибкой."""
        client = github_publishing.PublishingGitHub("owner/repo", "test-token")
        failure = HTTPError("https://api.github.com", 422, "ref exists", {}, None)
        with patch.object(github_publishing, "urlopen", side_effect=failure):
            with self.assertRaisesRegex(github_publishing.PublishingError, "422"):
                client.post("/git/refs", {"ref": "refs/heads/release/1.0", "sha": "a" * 40})

    def test_authenticated_user_requires_confirmed_response(self) -> None:
        """Личность token читается через /user и ошибка API закрывает допуск."""
        client = github_publishing.PublishingGitHub("owner/repo", "test-token")
        identity = {"id": 42288695, "login": "karateka-git"}
        with patch.object(github_publishing, "urlopen", return_value=FakeResponse(200, identity)) as open_url:
            self.assertEqual(client.authenticated_user(), identity)
        self.assertEqual(open_url.call_args.args[0].full_url, "https://api.github.com/user")
        with patch.object(github_publishing, "urlopen", return_value=FakeResponse(401, identity)):
            with self.assertRaises(github_publishing.PublishingError):
                client.authenticated_user()


if __name__ == "__main__":
    unittest.main()
