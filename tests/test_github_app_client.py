"""Тесты GitHub App HTTP adapter без реального интернета."""

from datetime import UTC, datetime
import base64
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from zipfile import ZipFile

from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import (
    GitHubDevicePollStatus,
    GitHubGatewayError,
    GitHubInstallationAccessToken,
    GitHubUserAccessToken,
    GitHubVaultSnapshotStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_configuration import (
    VaultConfigurationError,
    VaultConfigurationErrorCode,
)
from compadre_assistant.capabilities.obsidian.data.github import github_app_client as github_client_module
from compadre_assistant.capabilities.obsidian.data.github.github_app_client import HttpxGitHubAppClient
from compadre_assistant.capabilities.obsidian.data.github.jwt_signer import PyJwtGitHubAppSigner
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import ObsidianVault


class FakeResponse:
    """Имитирует JSON-ответ `urlopen` как context manager."""

    def __init__(self, payload: dict | list, *, headers: dict | None = None) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        return None

    def read(self) -> bytes:
        """Возвращает сериализованный JSON payload."""
        return self._body


class FakeBinaryResponse:
    """Имитирует ограниченно читаемый бинарный HTTP-ответ."""

    def __init__(self, body: bytes, *, headers: dict | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        """Возвращает не более запрошенного числа байт."""
        return self._body if size < 0 else self._body[:size]


class GitHubAppClientTest(unittest.TestCase):
    def test_inspect_target_reads_head_vault_tree_blob_and_markdown(self) -> None:
        """Pre-commit проверка возвращает snapshot vault и фактический target."""
        encoded = base64.b64encode("# Current".encode()).decode()
        responses = [
            FakeResponse({"object": {"sha": "commit-2"}}),
            FakeResponse({"tree": {"sha": "tree-2"}}),
            FakeResponse({"type": "file", "sha": "blob-2"}),
            FakeResponse({"encoding": "base64", "content": encoded}),
        ]
        client = _client()
        with (
            patch.object(
                client,
                "create_installation_token",
                return_value=_installation_token(),
            ) as token_factory,
            patch(
                "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
                side_effect=responses,
            ),
        ):
            state = client.inspect_vault_target(
                _vault(),
                target_path="Tech/Current.md",
            )

        self.assertEqual(state.head_commit_sha, "commit-2")
        self.assertEqual(state.tree_sha, "tree-2")
        self.assertEqual(state.target_blob_sha, "blob-2")
        self.assertEqual(state.target_markdown, "# Current")
        token_factory.assert_called_once_with(
            installation_id=101,
            repository_id=501,
        )

    def test_commit_target_uses_contents_api_and_expected_blob_sha(self) -> None:
        """Write отправляет один scoped PUT в default branch без скрытого retry."""
        response = FakeResponse(
            {
                "content": {"sha": "blob-new"},
                "commit": {"sha": "commit-new", "tree": {"sha": "tree-new"}},
            }
        )
        client = _client()
        with (
            patch.object(
                client,
                "create_installation_token",
                return_value=_installation_token(),
            ),
            patch(
                "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
                return_value=response,
            ) as opener,
        ):
            result = client.commit_vault_markdown(
                _vault(),
                target_path="Tech/Current.md",
                markdown="# Updated",
                expected_blob_sha="blob-old",
            )

        request = opener.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.method, "PUT")
        self.assertTrue(request.full_url.endswith("/contents/Tech/Current.md"))
        self.assertEqual(body["branch"], "main")
        self.assertEqual(body["sha"], "blob-old")
        self.assertEqual(result.commit_sha, "commit-new")
        self.assertEqual(result.tree_sha, "tree-new")
        self.assertEqual(result.blob_sha, "blob-new")

    """Проверяет Device Flow и installation token HTTP-контракты."""

    def test_request_device_authorization_uses_client_id_without_secret(self) -> None:
        """Первый Device Flow запрос передаёт только публичный Client ID."""
        payload = {
            "device_code": "device-secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            return_value=FakeResponse(payload),
        ) as opener:
            result = _client().request_device_authorization()

        request = opener.call_args.args[0]
        form = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(form, {"client_id": ["Iv1.client"]})
        self.assertEqual(result.user_code, "ABCD-EFGH")
        self.assertNotIn("device-secret", repr(result))

    def test_poll_device_token_maps_pending_slow_and_authorized(self) -> None:
        """GitHub error codes превращаются в типизированные статусы polling."""
        responses = [
            FakeResponse({"error": "authorization_pending"}),
            FakeResponse({"error": "slow_down"}),
            FakeResponse({"access_token": "ghu-secret", "token_type": "bearer"}),
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ):
            client = _client()
            pending = client.poll_device_token("device-secret")
            slow = client.poll_device_token("device-secret")
            authorized = client.poll_device_token("device-secret")

        self.assertEqual(pending.status, GitHubDevicePollStatus.PENDING)
        self.assertEqual(slow.status, GitHubDevicePollStatus.SLOW_DOWN)
        self.assertEqual(authorized.status, GitHubDevicePollStatus.AUTHORIZED)
        self.assertEqual(authorized.access_token.value, "ghu-secret")
        self.assertNotIn("ghu-secret", repr(authorized))

    def test_list_installations_reads_all_pages(self) -> None:
        """Installation IDs читаются со всех страниц user endpoint."""
        first_page = [{"id": value} for value in range(1, 101)]
        second_page = [{"id": 101}]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=[
                FakeResponse({"total_count": 101, "installations": first_page}),
                FakeResponse({"total_count": 101, "installations": second_page}),
            ],
        ) as opener:
            installation_ids = _client().list_installation_ids(
                GitHubUserAccessToken("ghu-secret")
            )

        self.assertEqual(installation_ids, set(range(1, 102)))
        self.assertIn("page=2", opener.call_args_list[1].args[0].full_url)
        self.assertEqual(
            opener.call_args_list[0].args[0].get_header("Authorization"),
            "Bearer ghu-secret",
        )

    def test_get_authenticated_account_reads_public_id_and_login(self) -> None:
        """Авторизованный user endpoint определяет аккаунт без сохранения token."""
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            return_value=FakeResponse({"id": 777, "login": "octocat"}),
        ) as opener:
            account = _client().get_authenticated_account(
                GitHubUserAccessToken("ghu-secret")
            )

        request = opener.call_args.args[0]
        self.assertEqual(account.github_user_id, 777)
        self.assertEqual(account.login, "octocat")
        self.assertEqual(request.full_url, "https://api.github.com/user")
        self.assertEqual(request.get_header("Authorization"), "Bearer ghu-secret")

    def test_create_installation_token_scopes_request_to_repository(self) -> None:
        """Installation token можно ограничить выбранным repository ID."""
        payload = {
            "token": "ghs-secret",
            "expires_at": "2026-07-22T12:00:00Z",
        }
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            return_value=FakeResponse(payload),
        ) as opener:
            token = _client().create_installation_token(
                installation_id=101,
                repository_id=501,
            )

        request = opener.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"repository_ids": [501]})
        self.assertEqual(request.get_header("Authorization"), "Bearer app-jwt")
        self.assertEqual(token.expires_at, datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
        self.assertNotIn("ghs-secret", repr(token))

    def test_inspect_repository_checks_default_branch_and_vault_directory(self) -> None:
        """Repository inspection требует write token и проверяет Contents API."""
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-23T12:00:00Z",
                    "permissions": {
                        "contents": "write",
                        "metadata": "read",
                    },
                    "repositories": [
                        {"id": 501, "full_name": "octocat/notes"}
                    ],
                }
            ),
            FakeResponse(
                {
                    "id": 501,
                    "name": "notes",
                    "owner": {"login": "octocat"},
                    "default_branch": "main",
                }
            ),
            FakeResponse([{"type": "file", "name": "Index.md"}]),
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ) as opener:
            inspection = _client().inspect_repository(
                installation_id=101,
                owner="octocat",
                repository="notes",
                root_path="Vault/Личное",
            )

        self.assertEqual(inspection.repository_id, 501)
        self.assertEqual(inspection.default_branch, "main")
        self.assertTrue(inspection.root_path_is_directory)
        token_request = opener.call_args_list[0].args[0]
        repository_request = opener.call_args_list[1].args[0]
        contents_request = opener.call_args_list[2].args[0]
        self.assertEqual(
            json.loads(token_request.data),
            {
                "repositories": ["notes"],
                "permissions": {
                    "contents": "write",
                    "metadata": "read",
                },
            },
        )
        self.assertEqual(
            repository_request.full_url,
            "https://api.github.com/repos/octocat/notes",
        )
        self.assertIn(
            "Vault/%D0%9B%D0%B8%D1%87%D0%BD%D0%BE%D0%B5",
            contents_request.full_url,
        )
        self.assertIn("ref=main", contents_request.full_url)
        self.assertEqual(
            contents_request.get_header("Authorization"),
            "Bearer ghs-secret",
        )

    def test_inspect_repository_rejects_repository_without_write_access(self) -> None:
        """Публичный repository не подключается, если GitHub не выдаёт write token."""
        for status in (403, 404, 422):
            denied = HTTPError(
                url="https://api.github.com/app/installations/101/access_tokens",
                code=status,
                msg="Repository access denied",
                hdrs=None,
                fp=None,
            )
            with self.subTest(status=status), patch(
                "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
                side_effect=denied,
            ) as opener:
                inspection = _client().inspect_repository(
                    installation_id=101,
                    owner="octocat",
                    repository="public-but-not-selected",
                    root_path="",
                )

            self.assertIsNone(inspection)
            self.assertEqual(opener.call_count, 1)

    def test_inspect_repository_rejects_read_only_token(self) -> None:
        """Даже выданный token без `Contents: write` не разрешает vault."""
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            return_value=FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-23T12:00:00Z",
                    "permissions": {
                        "contents": "read",
                        "metadata": "read",
                    },
                    "repositories": [
                        {"id": 501, "full_name": "octocat/read-only"}
                    ],
                }
            ),
        ) as opener:
            inspection = _client().inspect_repository(
                installation_id=101,
                owner="octocat",
                repository="read-only",
                root_path="",
            )

        self.assertIsNone(inspection)
        self.assertEqual(opener.call_count, 1)

    def test_inspect_repository_rejects_archived_repository(self) -> None:
        """Архивный repository нельзя выбрать для будущих commit."""
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-23T12:00:00Z",
                    "permissions": {
                        "contents": "write",
                        "metadata": "read",
                    },
                    "repositories": [
                        {"id": 501, "full_name": "octocat/notes"}
                    ],
                }
            ),
            FakeResponse(
                {
                    "id": 501,
                    "name": "notes",
                    "owner": {"login": "octocat"},
                    "default_branch": "main",
                    "archived": True,
                }
            ),
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ) as opener:
            inspection = _client().inspect_repository(
                installation_id=101,
                owner="octocat",
                repository="notes",
                root_path="",
            )

        self.assertIsNone(inspection)
        self.assertEqual(opener.call_count, 2)

    def test_inspect_repository_rejects_different_owner_with_same_name(self) -> None:
        """Token другого owner не разрешает публичный repository с тем же именем."""
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-23T12:00:00Z",
                    "permissions": {
                        "contents": "write",
                        "metadata": "read",
                    },
                    "repositories": [
                        {"id": 501, "full_name": "octocat/notes"}
                    ],
                }
            ),
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ) as opener:
            inspection = _client().inspect_repository(
                installation_id=101,
                owner="another-owner",
                repository="notes",
                root_path="",
            )

        self.assertIsNone(inspection)
        self.assertEqual(opener.call_count, 1)

    def test_http_error_does_not_include_response_or_token(self) -> None:
        """HTTP-ошибка сообщает только status и не раскрывает credentials."""
        error = HTTPError(
            url="https://api.github.com/user/installations",
            code=401,
            msg="token ghu-secret rejected",
            hdrs=None,
            fp=None,
        )
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=error,
        ):
            with self.assertRaises(GitHubGatewayError) as context:
                _client().list_installation_ids(GitHubUserAccessToken("ghu-secret"))

        self.assertEqual(str(context.exception), "GitHub request failed with HTTP 401")
        self.assertNotIn("ghu-secret", str(context.exception))

    def test_get_retries_transient_url_error_and_preserves_root_cause(self) -> None:
        """Безопасный GET переживает временный timeout и логирует его причину."""
        delays: list[float] = []
        timeout = URLError(TimeoutError("read timed out"))
        client = HttpxGitHubAppClient(
            client_id="Iv1.client",
            app_jwt_factory=lambda: "app-jwt",
            sleeper=delays.append,
            request_opener=_test_request_opener,
        )
        with (
            patch(
                "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
                side_effect=[
                    timeout,
                    timeout,
                    FakeResponse({"id": 777, "login": "octocat"}),
                ],
            ) as opener,
            self.assertLogs(
                "compadre_assistant.capabilities.obsidian.data.github.github_app_client",
                level="WARNING",
            ) as logs,
        ):
            account = client.get_authenticated_account(
                GitHubUserAccessToken("ghu-secret")
            )

        self.assertEqual(account.login, "octocat")
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(delays, [0.5, 1.0])
        self.assertIn("TimeoutError: read timed out", "\n".join(logs.output))

    def test_get_uses_retry_after_for_temporary_http_error(self) -> None:
        """GitHub Retry-After имеет приоритет над локальным backoff."""
        delays: list[float] = []
        unavailable = HTTPError(
            url="https://api.github.com/user",
            code=403,
            msg="Secondary rate limit",
            hdrs={"Retry-After": "2.5"},
            fp=None,
        )
        client = HttpxGitHubAppClient(
            client_id="Iv1.client",
            app_jwt_factory=lambda: "app-jwt",
            sleeper=delays.append,
            request_opener=_test_request_opener,
        )
        with (
            patch(
                "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
                side_effect=[
                    unavailable,
                    FakeResponse({"id": 777, "login": "octocat"}),
                ],
            ),
            self.assertLogs(
                "compadre_assistant.capabilities.obsidian.data.github.github_app_client",
                level="WARNING",
            ),
        ):
            client.get_authenticated_account(
                GitHubUserAccessToken("ghu-secret")
            )

        self.assertEqual(delays, [2.5])

    def test_fetch_vault_snapshot_downloads_only_changed_markdown_blobs(self) -> None:
        """Git tree формирует полный manifest, но неизменённый blob не скачивается."""
        encoded = base64.b64encode("# Новая заметка".encode()).decode()
        config = base64.b64encode(
            b"version: 1\ninstructions:\n  - rules.txt\n"
        ).decode()
        rules = base64.b64encode("Правила".encode()).decode()
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-27T13:00:00Z",
                }
            ),
            FakeResponse(
                {"object": {"sha": "commit-2"}},
                headers={"ETag": '"etag-2"'},
            ),
            FakeResponse({"tree": {"sha": "tree-2"}}),
            FakeResponse(
                {
                    "tree": [
                        {"type": "blob", "path": "same.md", "sha": "same-sha"},
                        {"type": "blob", "path": "new.md", "sha": "new-sha"},
                        {
                            "type": "blob",
                            "path": ".knowledge-catcher.yml",
                            "sha": "config-sha",
                        },
                        {"type": "blob", "path": "rules.txt", "sha": "rules-sha"},
                        {"type": "blob", "path": "image.png", "sha": "image-sha"},
                    ],
                    "truncated": False,
                }
            ),
            FakeResponse({"encoding": "base64", "content": config}),
            FakeResponse({"encoding": "base64", "content": encoded}),
            FakeResponse({"encoding": "base64", "content": rules}),
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ) as opener:
            snapshot = _client().fetch_vault_snapshot(
                _vault(),
                known_blobs={"same.md": "same-sha"},
                known_instruction_blobs={},
            )

        self.assertEqual(snapshot.status, GitHubVaultSnapshotStatus.CHANGED)
        self.assertEqual(snapshot.head_etag, '"etag-2"')
        self.assertEqual([file.path for file in snapshot.files], ["new.md", "same.md"])
        self.assertEqual(snapshot.files[0].markdown, "# Новая заметка")
        self.assertIsNone(snapshot.files[1].markdown)
        self.assertEqual(snapshot.instructions[0].content, "Правила")
        self.assertEqual(opener.call_count, 7)
        ref_request = opener.call_args_list[1].args[0]
        self.assertEqual(ref_request.get_header("If-none-match"), '"etag-1"')

    def test_fetch_vault_snapshot_uses_archive_for_bulk_download(self) -> None:
        """Первая массовая загрузка читает нужные тексты из одного ZIP snapshot."""
        config = base64.b64encode(
            b"version: 1\ninstructions:\n  - rules.txt\n"
        ).decode()
        archive_buffer = BytesIO()
        with ZipFile(archive_buffer, "w") as archive:
            archive.writestr("owner-notes-commit/note-a.md", "# A")
            archive.writestr("owner-notes-commit/note-b.md", "# B")
            archive.writestr("owner-notes-commit/rules.txt", "Rules")
            archive.writestr("owner-notes-commit/image.png", b"ignored")
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-27T13:00:00Z",
                }
            ),
            FakeResponse({"object": {"sha": "commit-2"}}),
            FakeResponse({"tree": {"sha": "tree-2"}}),
            FakeResponse(
                {
                    "tree": [
                        {
                            "type": "blob",
                            "path": "note-a.md",
                            "sha": "a-sha",
                            "size": 3,
                        },
                        {
                            "type": "blob",
                            "path": "note-b.md",
                            "sha": "b-sha",
                            "size": 3,
                        },
                        {
                            "type": "blob",
                            "path": ".knowledge-catcher.yml",
                            "sha": "config-sha",
                            "size": 44,
                        },
                        {
                            "type": "blob",
                            "path": "rules.txt",
                            "sha": "rules-sha",
                            "size": 5,
                        },
                    ],
                    "truncated": False,
                }
            ),
            FakeResponse({"encoding": "base64", "content": config}),
            FakeBinaryResponse(
                archive_buffer.getvalue(),
                headers={
                    "Content-Length": str(len(archive_buffer.getvalue()))
                },
            ),
        ]
        client = HttpxGitHubAppClient(
            client_id="Iv1.client",
            app_jwt_factory=lambda: "app-jwt",
            bulk_download_threshold=2,
            request_opener=_test_request_opener,
        )
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ) as opener:
            snapshot = client.fetch_vault_snapshot(
                _vault(),
                known_blobs={},
                known_instruction_blobs={},
            )

        self.assertEqual(
            [file.markdown for file in snapshot.files],
            ["# A", "# B"],
        )
        self.assertEqual(snapshot.instructions[0].content, "Rules")
        urls = [call.args[0].full_url for call in opener.call_args_list]
        self.assertEqual(len(urls), 6)
        self.assertTrue(any("/zipball/commit-2" in url for url in urls))
        self.assertFalse(any("/git/blobs/a-sha" in url for url in urls))

    def test_fetch_vault_snapshot_accepts_not_modified_etag(self) -> None:
        """HTTP 304 завершает проверку до чтения commit, tree и blobs."""
        not_modified = HTTPError(
            url="https://api.github.com/repos/owner/notes/git/ref/heads/main",
            code=304,
            msg="Not Modified",
            hdrs=None,
            fp=None,
        )
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-27T13:00:00Z",
                }
            ),
            not_modified,
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ) as opener:
            snapshot = _client().fetch_vault_snapshot(
                _vault(),
                known_blobs={},
                known_instruction_blobs={},
            )

        self.assertEqual(snapshot.status, GitHubVaultSnapshotStatus.NOT_MODIFIED)
        self.assertEqual(opener.call_count, 2)

    def test_fetch_vault_snapshot_accepts_empty_markdown_blob(self) -> None:
        """Пустая Markdown-заметка является корректным Git blob."""
        config = base64.b64encode(
            b"version: 1\ninstructions:\n  - rules.txt\n"
        ).decode()
        rules = base64.b64encode(b"Rules").decode()
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-27T13:00:00Z",
                }
            ),
            FakeResponse({"object": {"sha": "commit-2"}}),
            FakeResponse({"tree": {"sha": "tree-2"}}),
            FakeResponse(
                {
                    "tree": [
                        {"type": "blob", "path": "empty.md", "sha": "empty-sha"},
                        {
                            "type": "blob",
                            "path": ".knowledge-catcher.yml",
                            "sha": "config-sha",
                        },
                        {"type": "blob", "path": "rules.txt", "sha": "rules-sha"},
                    ],
                    "truncated": False,
                }
            ),
            FakeResponse({"encoding": "base64", "content": config}),
            FakeResponse({"encoding": "base64", "content": ""}),
            FakeResponse({"encoding": "base64", "content": rules}),
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ):
            snapshot = _client().fetch_vault_snapshot(
                _vault(),
                known_blobs={},
                known_instruction_blobs={},
            )

        self.assertEqual(snapshot.files[0].markdown, "")

    def test_fetch_vault_snapshot_requires_configuration_file(self) -> None:
        """Первичная синхронизация отклоняет vault без служебного YAML."""
        responses = [
            FakeResponse(
                {
                    "token": "ghs-secret",
                    "expires_at": "2026-07-27T13:00:00Z",
                }
            ),
            FakeResponse({"object": {"sha": "commit-2"}}),
            FakeResponse({"tree": {"sha": "tree-2"}}),
            FakeResponse(
                {
                    "tree": [
                        {"type": "blob", "path": "note.md", "sha": "note-sha"}
                    ],
                    "truncated": False,
                }
            ),
        ]
        with patch(
            "compadre_assistant.capabilities.obsidian.data.github.github_app_client.urlopen",
            side_effect=responses,
        ):
            with self.assertRaises(VaultConfigurationError) as context:
                _client().fetch_vault_snapshot(
                    _vault(),
                    known_blobs={},
                    known_instruction_blobs={},
                )

        self.assertEqual(
            context.exception.code,
            VaultConfigurationErrorCode.MISSING,
        )

    def test_jwt_signer_uses_client_id_and_short_lifetime(self) -> None:
        """JWT signer использует рекомендуемый Client ID и окно меньше 10 минут."""
        now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        calls: list[tuple[dict, bytes, str]] = []

        def encode(payload, key, algorithm):
            calls.append((payload, key, algorithm))
            return "signed-jwt"

        fake_jwt = types.SimpleNamespace(encode=encode)
        with TemporaryDirectory(prefix="compadre-assistant-github-jwt-") as directory:
            key_path = Path(directory) / "app.pem"
            key_path.write_bytes(b"private-key")
            with patch.dict("sys.modules", {"jwt": fake_jwt}):
                result = PyJwtGitHubAppSigner(
                    client_id="Iv1.client",
                    private_key_path=key_path,
                    clock=lambda: now,
                ).create()

        payload, key, algorithm = calls[0]
        self.assertEqual(result, "signed-jwt")
        self.assertEqual(payload["iss"], "Iv1.client")
        self.assertLessEqual(payload["exp"] - payload["iat"], 600)
        self.assertEqual(key, b"private-key")
        self.assertEqual(algorithm, "RS256")

    def test_jwt_signer_creates_verifiable_rs256_signature(self) -> None:
        """Реальные PyJWT и cryptography подписывают JWT временным RSA-ключом."""
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with TemporaryDirectory(prefix="compadre-assistant-github-rs256-") as directory:
            key_path = Path(directory) / "app.pem"
            key_path.write_bytes(private_pem)
            token = PyJwtGitHubAppSigner(
                client_id="Iv1.client",
                private_key_path=key_path,
            ).create()

        payload = jwt.decode(
            token,
            private_key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual(payload["iss"], "Iv1.client")


def _client() -> HttpxGitHubAppClient:
    """Создаёт HTTP client с предсказуемым App JWT."""
    return HttpxGitHubAppClient(
        client_id="Iv1.client",
        app_jwt_factory=lambda: "app-jwt",
        request_opener=_test_request_opener,
    )


def _installation_token() -> GitHubInstallationAccessToken:
    """Создаёт короткоживущий repository-scoped token для HTTP-тестов."""
    return GitHubInstallationAccessToken(
        value="installation-token",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


def _test_request_opener(request, timeout):
    """Передаёт тестовый запрос в патченный urllib opener."""
    return github_client_module.urlopen(request, timeout=timeout)


def _vault() -> ObsidianVault:
    """Создаёт сохранённый vault с условным ETag."""
    return ObsidianVault(
        id=1,
        app_user_id=1,
        installation_id=101,
        repository_id=501,
        owner="owner",
        repository="notes",
        branch="main",
        head_commit_sha="commit-1",
        tree_sha="tree-1",
        head_etag='"etag-1"',
    )


if __name__ == "__main__":
    unittest.main()
