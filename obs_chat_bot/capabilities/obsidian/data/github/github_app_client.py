from __future__ import annotations

import base64
from io import BytesIO
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import logging
from pathlib import PurePosixPath
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

import httpx

from obs_chat_bot.capabilities.obsidian.data.configuration.vault_configuration import parse_vault_configuration
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import (
    GitHubAuthenticatedAccount,
    GitHubDeviceAuthorization,
    GitHubDevicePollResult,
    GitHubDevicePollStatus,
    GitHubGatewayError,
    GitHubInstallationAccessToken,
    GitHubInstructionFile,
    GitHubMarkdownFile,
    GitHubRepositoryInspection,
    GitHubUserAccessToken,
    GitHubVaultSnapshot,
    GitHubVaultSnapshotStatus,
    GitHubVaultCommitResult,
    GitHubVaultTargetState,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_configuration import (
    VAULT_CONFIGURATION_PATH,
    VaultConfigurationError,
    VaultConfigurationErrorCode,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.ports import (
    GitHubDeviceFlowGateway,
    GitHubInstallationTokenProvider,
    GitHubRepositoryGateway,
    GitHubVaultGateway,
    GitHubVaultWriteGateway,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import ObsidianVault


GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_WEB_BASE_URL = "https://github.com"
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_USER_AGENT = "obsChatBot/0.1.0"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_READ_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.5
DEFAULT_BULK_DOWNLOAD_THRESHOLD = 50
MAX_BLOB_DOWNLOAD_WORKERS = 6  # Ограничивает параллелизм первой синхронизации.
MAX_CONFIGURATION_BYTES = 64 * 1024
MAX_INSTRUCTION_BYTES = 256 * 1024
MAX_MARKDOWN_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_VAULT_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_EXTRACTED_BYTES = 64 * 1024 * 1024
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
LOGGER = logging.getLogger(__name__)


class _GitHubArchiveTooLargeError(GitHubGatewayError):
    """Архив repository превышает безопасный предел массовой загрузки."""


class _HttpxResponseAdapter:
    """Предоставляет минимальный file-like интерфейс потокового ответа httpx."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._iterator = response.iter_bytes()
        self._buffer = bytearray()
        self.headers = response.headers

    def __enter__(self) -> _HttpxResponseAdapter:
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self._response.close()

    def read(self, size: int = -1) -> bytes:
        """Читает весь ответ либо не более заданного числа байт."""
        try:
            if size < 0:
                self._buffer.extend(b"".join(self._iterator))
                result = bytes(self._buffer)
                self._buffer.clear()
                return result
            while len(self._buffer) < size:
                try:
                    self._buffer.extend(next(self._iterator))
                except StopIteration:
                    break
            result = bytes(self._buffer[:size])
            del self._buffer[:size]
            return result
        except httpx.TimeoutException as error:
            raise URLError(TimeoutError(type(error).__name__)) from None
        except httpx.RequestError as error:
            raise URLError(f"httpx {type(error).__name__}") from None


class _PooledHttpxRequestOpener:
    """Открывает GitHub-запросы через общий thread-safe connection pool."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=12,
                max_keepalive_connections=6,
            ),
        )

    def __call__(self, request: Request, timeout: float) -> _HttpxResponseAdapter:
        """Преобразует urllib Request в потоковый запрос общего httpx.Client."""
        try:
            outgoing = self._client.build_request(
                request.get_method(),
                request.full_url,
                headers=dict(request.header_items()),
                content=request.data,
                timeout=timeout,
            )
            response = self._client.send(
                outgoing,
                stream=True,
                follow_redirects=True,
            )
        except httpx.TimeoutException as error:
            raise URLError(TimeoutError(type(error).__name__)) from None
        except httpx.RequestError as error:
            raise URLError(f"httpx {type(error).__name__}") from None
        if response.status_code >= 300:
            response.close()
            raise HTTPError(
                url=request.full_url,
                code=response.status_code,
                msg=response.reason_phrase,
                hdrs=response.headers,
                fp=None,
            )
        return _HttpxResponseAdapter(response)


class HttpxGitHubAppClient(
    GitHubDeviceFlowGateway,
    GitHubInstallationTokenProvider,
    GitHubRepositoryGateway,
    GitHubVaultGateway,
    GitHubVaultWriteGateway,
):
    """Выполняет GitHub Device Flow и выпускает installation tokens."""

    def __init__(
        self,
        *,
        client_id: str,
        app_jwt_factory: Callable[[], str],
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        read_attempts: int = DEFAULT_READ_ATTEMPTS,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        bulk_download_threshold: int = DEFAULT_BULK_DOWNLOAD_THRESHOLD,
        request_opener: Callable[[Request, float], Any] | None = None,
    ) -> None:
        if not client_id.strip():
            raise ValueError("client_id must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if read_attempts <= 0:
            raise ValueError("read_attempts must be positive")
        if retry_base_delay_seconds < 0:
            raise ValueError("retry_base_delay_seconds must not be negative")
        if bulk_download_threshold <= 0:
            raise ValueError("bulk_download_threshold must be positive")
        self._client_id = client_id
        self._app_jwt_factory = app_jwt_factory
        self._timeout_seconds = timeout_seconds
        self._read_attempts = read_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._sleeper = sleeper
        self._bulk_download_threshold = bulk_download_threshold
        self._request_opener = request_opener or _PooledHttpxRequestOpener()

    def request_device_authorization(self) -> GitHubDeviceAuthorization:
        """Запрашивает Device Flow challenge по Client ID GitHub App."""
        payload = self._post_form(
            f"{GITHUB_WEB_BASE_URL}/login/device/code",
            {"client_id": self._client_id},
        )
        return GitHubDeviceAuthorization(
            device_code=_require_string(payload, "device_code"),
            user_code=_require_string(payload, "user_code"),
            verification_uri=_require_string(payload, "verification_uri"),
            expires_in=_require_positive_int(payload, "expires_in"),
            interval=_require_positive_int(payload, "interval"),
        )

    def poll_device_token(self, device_code: str) -> GitHubDevicePollResult:
        """Проверяет одно состояние Device Flow без внутреннего ожидания."""
        if not device_code.strip():
            raise ValueError("device_code must not be empty")
        payload = self._post_form(
            f"{GITHUB_WEB_BASE_URL}/login/oauth/access_token",
            {
                "client_id": self._client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        access_token = payload.get("access_token")
        if isinstance(access_token, str) and access_token.strip():
            return GitHubDevicePollResult(
                status=GitHubDevicePollStatus.AUTHORIZED,
                access_token=GitHubUserAccessToken(access_token),
            )

        error = payload.get("error")
        statuses = {
            "authorization_pending": GitHubDevicePollStatus.PENDING,
            "slow_down": GitHubDevicePollStatus.SLOW_DOWN,
            "expired_token": GitHubDevicePollStatus.EXPIRED,
            "access_denied": GitHubDevicePollStatus.DENIED,
        }
        if isinstance(error, str) and error in statuses:
            return GitHubDevicePollResult(status=statuses[error])
        raise GitHubGatewayError("GitHub Device Flow returned an unknown response")

    def list_installation_ids(
        self,
        access_token: GitHubUserAccessToken,
    ) -> set[int]:
        """Читает все доступные installations с пагинацией по 100 строк."""
        installation_ids: set[int] = set()
        page = 1
        while True:
            query = urlencode({"per_page": 100, "page": page})
            payload = self._request_json(
                Request(
                    f"{GITHUB_API_BASE_URL}/user/installations?{query}",
                    headers=self._api_headers(access_token.value),
                    method="GET",
                )
            )
            installations = payload.get("installations")
            if not isinstance(installations, list):
                raise GitHubGatewayError(
                    "GitHub installations response has unexpected format"
                )
            for installation in installations:
                if not isinstance(installation, dict):
                    raise GitHubGatewayError(
                        "GitHub installation item has unexpected format"
                    )
                installation_ids.add(_require_positive_int(installation, "id"))

            total_count = _require_non_negative_int(payload, "total_count")
            if len(installations) < 100 or len(installation_ids) >= total_count:
                return installation_ids
            page += 1

    def get_authenticated_account(
        self,
        access_token: GitHubUserAccessToken,
    ) -> GitHubAuthenticatedAccount:
        """Читает публичные ID и login авторизованного GitHub-аккаунта."""
        payload = self._request_json(
            Request(
                f"{GITHUB_API_BASE_URL}/user",
                headers=self._api_headers(access_token.value),
                method="GET",
            )
        )
        return GitHubAuthenticatedAccount(
            github_user_id=_require_positive_int(payload, "id"),
            login=_require_string(payload, "login"),
        )

    def create_installation_token(
        self,
        *,
        installation_id: int,
        repository_id: int | None = None,
    ) -> GitHubInstallationAccessToken:
        """Выпускает installation token, опционально ограниченный repository."""
        if installation_id <= 0:
            raise ValueError("installation_id must be positive")
        if repository_id is not None and repository_id <= 0:
            raise ValueError("repository_id must be positive")
        body: dict[str, Any] = {}
        if repository_id is not None:
            body["repository_ids"] = [repository_id]
        app_jwt = self._app_jwt_factory()
        payload = self._request_json(
            Request(
                f"{GITHUB_API_BASE_URL}/app/installations/"
                f"{installation_id}/access_tokens",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    **self._api_headers(app_jwt),
                    "Content-Type": "application/json",
                },
                method="POST",
            )
        )
        return GitHubInstallationAccessToken(
            value=_require_string(payload, "token"),
            expires_at=_parse_github_timestamp(
                _require_string(payload, "expires_at")
            ),
        )

    def inspect_repository(
        self,
        *,
        installation_id: int,
        owner: str,
        repository: str,
        root_path: str,
    ) -> GitHubRepositoryInspection | None:
        """Проверяет доступ installation к repository и каталогу vault.

        Args:
            installation_id: Разрешённая пользователю установка GitHub App.
            owner: Владелец repository.
            repository: Имя repository.
            root_path: Относительный путь каталога vault или пустая строка.

        Returns:
            Metadata repository и признак каталога либо `None` при HTTP 404.

        Raises:
            GitHubGatewayError: GitHub недоступен или вернул некорректный ответ.
        """
        if installation_id <= 0:
            raise ValueError("installation_id must be positive")
        if not owner.strip() or not repository.strip():
            raise ValueError("owner and repository must not be empty")
        token = self._create_repository_write_token(
            installation_id=installation_id,
            owner=owner,
            repository=repository,
        )
        if token is None:
            return None
        encoded_owner = quote(owner, safe="")
        encoded_repository = quote(repository, safe="")
        repository_payload = self._request_json_or_none(
            Request(
                f"{GITHUB_API_BASE_URL}/repos/{encoded_owner}/{encoded_repository}",
                headers=self._api_headers(token.value),
                method="GET",
            )
        )
        if repository_payload is None:
            return None
        if repository_payload.get("archived") is True:
            return None
        if repository_payload.get("disabled") is True:
            return None

        repository_id = _require_positive_int(repository_payload, "id")
        canonical_owner = _require_nested_string(
            repository_payload,
            container_name="owner",
            field_name="login",
        )
        canonical_repository = _require_string(repository_payload, "name")
        default_branch = _require_string(repository_payload, "default_branch")
        encoded_path = quote(root_path, safe="/")
        contents_suffix = f"/{encoded_path}" if encoded_path else ""
        query = urlencode({"ref": default_branch})
        contents_payload = self._request_json_value(
            Request(
                f"{GITHUB_API_BASE_URL}/repos/{encoded_owner}/"
                f"{encoded_repository}/contents{contents_suffix}?{query}",
                headers=self._api_headers(token.value),
                method="GET",
            ),
            allow_not_found=True,
        )
        return GitHubRepositoryInspection(
            installation_id=installation_id,
            repository_id=repository_id,
            owner=canonical_owner,
            repository=canonical_repository,
            default_branch=default_branch,
            root_path_is_directory=isinstance(contents_payload, list),
        )

    def fetch_vault_snapshot(
        self,
        vault: ObsidianVault,
        *,
        known_blobs: Mapping[str, str],
        known_instruction_blobs: Mapping[str, str],
    ) -> GitHubVaultSnapshot:
        """Читает Git tree, правила vault и изменённые Markdown blobs.

        Args:
            vault: Сохраненное подключение repository, ветки и корневой папки.
            known_blobs: Локальное соответствие пути и последнего blob SHA.
            known_instruction_blobs: Локальные SHA обязательных правил.

        Returns:
            Условный снимок с полным manifest и содержимым изменённых файлов.

        Raises:
            GitHubGatewayError: GitHub недоступен или вернул некорректные данные.
            VaultConfigurationError: Конфигурация правил отсутствует или неверна.
        """
        token = self.create_installation_token(
            installation_id=vault.installation_id,
            repository_id=vault.repository_id,
        )
        repository_url = (
            f"{GITHUB_API_BASE_URL}/repos/{quote(vault.owner, safe='')}/"
            f"{quote(vault.repository, safe='')}"
        )
        ref_headers = self._api_headers(token.value)
        if vault.head_etag is not None:
            ref_headers["If-None-Match"] = vault.head_etag
        ref_payload, ref_etag, not_modified = self._request_json_with_metadata(
            Request(
                f"{repository_url}/git/ref/heads/{quote(vault.branch, safe='')}",
                headers=ref_headers,
                method="GET",
            ),
            allow_not_modified=True,
        )
        if not_modified:
            return GitHubVaultSnapshot(
                status=GitHubVaultSnapshotStatus.NOT_MODIFIED,
                head_etag=vault.head_etag,
            )
        head_commit_sha = _require_nested_string(
            ref_payload,
            container_name="object",
            field_name="sha",
        )
        commit_payload = self._request_json(
            Request(
                f"{repository_url}/git/commits/{quote(head_commit_sha, safe='')}",
                headers=self._api_headers(token.value),
                method="GET",
            )
        )
        repository_tree_sha = _require_nested_string(
            commit_payload,
            container_name="tree",
            field_name="sha",
        )
        vault_tree_sha = self._resolve_vault_tree_sha(
            repository_url=repository_url,
            token=token.value,
            tree_sha=repository_tree_sha,
            root_path=vault.root_path,
        )
        if vault.tree_sha == vault_tree_sha:
            return GitHubVaultSnapshot(
                status=GitHubVaultSnapshotStatus.TREE_UNCHANGED,
                head_commit_sha=head_commit_sha,
                tree_sha=vault_tree_sha,
                head_etag=ref_etag,
            )

        tree_payload = self._request_json(
            Request(
                f"{repository_url}/git/trees/{quote(vault_tree_sha, safe='')}"
                "?recursive=1",
                headers=self._api_headers(token.value),
                method="GET",
            )
        )
        if tree_payload.get("truncated") is True:
            raise GitHubGatewayError("GitHub vault tree is truncated")
        tree = tree_payload.get("tree")
        if not isinstance(tree, list):
            raise GitHubGatewayError("GitHub tree response has unexpected format")
        tree_blobs: dict[str, str] = {}
        vault_bytes = 0
        archive_size_known = True
        for item in tree:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = item.get("path")
            blob_sha = item.get("sha")
            if (
                not isinstance(path, str)
                or not isinstance(blob_sha, str)
                or not blob_sha.strip()
            ):
                continue
            tree_blobs[path] = blob_sha
            size = item.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                vault_bytes += size
            else:
                archive_size_known = False

        configuration_sha = tree_blobs.get(VAULT_CONFIGURATION_PATH)
        if configuration_sha is None:
            raise VaultConfigurationError(VaultConfigurationErrorCode.MISSING)
        configuration_content = self._download_text_blob(
            repository_url=repository_url,
            token=token.value,
            blob_sha=configuration_sha,
            max_bytes=MAX_CONFIGURATION_BYTES,
        )
        configuration = parse_vault_configuration(configuration_content)
        instruction_manifest: list[tuple[int, str, str]] = []
        for position, path in enumerate(configuration.instruction_paths):
            blob_sha = tree_blobs.get(path)
            if blob_sha is None:
                raise VaultConfigurationError(
                    VaultConfigurationErrorCode.INSTRUCTION_MISSING,
                    path=path,
                )
            instruction_manifest.append((position, path, blob_sha))

        instruction_paths = set(configuration.instruction_paths)
        manifest = [
            (path, blob_sha)
            for path, blob_sha in tree_blobs.items()
            if path.lower().endswith(".md") and path not in instruction_paths
        ]
        changed = [
            (path, blob_sha)
            for path, blob_sha in manifest
            if known_blobs.get(path) != blob_sha
        ]
        changed_instructions = [
            (path, blob_sha)
            for _position, path, blob_sha in instruction_manifest
            if known_instruction_blobs.get(path) != blob_sha
        ]
        changed_count = len(changed) + len(changed_instructions)
        use_archive = (
            changed_count >= self._bulk_download_threshold
            and archive_size_known
            and vault_bytes <= MAX_ARCHIVE_VAULT_BYTES
        )
        download_mode = "archive" if use_archive else "blobs"
        download_started_at = time.monotonic()
        LOGGER.info(
            "GitHub vault content download started: app_user_id=%s "
            "repository=%s/%s mode=%s markdown_count=%s "
            "instruction_count=%s vault_bytes=%s",
            vault.app_user_id,
            vault.owner,
            vault.repository,
            download_mode,
            len(changed),
            len(changed_instructions),
            vault_bytes if archive_size_known else "unknown",
        )
        try:
            if use_archive:
                try:
                    archive_files = self._download_archive_text_files(
                        repository_url=repository_url,
                        token=token.value,
                        commit_sha=head_commit_sha,
                        root_path=vault.root_path,
                        limits={
                            **{
                                path: MAX_MARKDOWN_BYTES
                                for path, _blob_sha in changed
                            },
                            **{
                                path: MAX_INSTRUCTION_BYTES
                                for path, _blob_sha in changed_instructions
                            },
                        },
                    )
                    downloaded = {
                        path: archive_files[path]
                        for path, _blob_sha in changed
                    }
                    downloaded_instructions = {
                        path: archive_files[path]
                        for path, _blob_sha in changed_instructions
                    }
                except _GitHubArchiveTooLargeError:
                    download_mode = "blobs_fallback"
                    LOGGER.warning(
                        "GitHub archive is too large; falling back to blobs: "
                        "app_user_id=%s repository=%s/%s",
                        vault.app_user_id,
                        vault.owner,
                        vault.repository,
                    )
                    downloaded = self._download_changed_text_files(
                        repository_url=repository_url,
                        token=token.value,
                        changed=changed,
                        max_bytes=MAX_MARKDOWN_BYTES,
                    )
                    downloaded_instructions = (
                        self._download_changed_text_files(
                            repository_url=repository_url,
                            token=token.value,
                            changed=changed_instructions,
                            max_bytes=MAX_INSTRUCTION_BYTES,
                        )
                    )
            else:
                downloaded = self._download_changed_text_files(
                    repository_url=repository_url,
                    token=token.value,
                    changed=changed,
                    max_bytes=MAX_MARKDOWN_BYTES,
                )
                downloaded_instructions = self._download_changed_text_files(
                    repository_url=repository_url,
                    token=token.value,
                    changed=changed_instructions,
                    max_bytes=MAX_INSTRUCTION_BYTES,
                )
        except Exception:
            LOGGER.exception(
                "GitHub vault content download failed: app_user_id=%s "
                "repository=%s/%s mode=%s duration_seconds=%.3f",
                vault.app_user_id,
                vault.owner,
                vault.repository,
                download_mode,
                time.monotonic() - download_started_at,
            )
            raise
        LOGGER.info(
            "GitHub vault content download completed: app_user_id=%s "
            "repository=%s/%s mode=%s duration_seconds=%.3f",
            vault.app_user_id,
            vault.owner,
            vault.repository,
            download_mode,
            time.monotonic() - download_started_at,
        )
        files = [
            GitHubMarkdownFile(
                path=path,
                blob_sha=blob_sha,
                markdown=downloaded.get(path),
            )
            for path, blob_sha in manifest
        ]
        instructions = [
            GitHubInstructionFile(
                position=position,
                path=path,
                blob_sha=blob_sha,
                content=downloaded_instructions.get(path),
            )
            for position, path, blob_sha in instruction_manifest
        ]
        return GitHubVaultSnapshot(
            status=GitHubVaultSnapshotStatus.CHANGED,
            head_commit_sha=head_commit_sha,
            tree_sha=vault_tree_sha,
            head_etag=ref_etag,
            files=tuple(sorted(files, key=lambda file: file.path)),
            instructions=tuple(instructions),
        )

    def inspect_vault_target(
        self,
        vault: ObsidianVault,
        *,
        target_path: str,
    ) -> GitHubVaultTargetState:
        """Повторно читает HEAD/tree и target blob перед подтверждённой записью."""
        token = self.create_installation_token(
            installation_id=vault.installation_id,
            repository_id=vault.repository_id,
        )
        repository_url = _repository_api_url(vault)
        headers = self._api_headers(token.value)
        ref_payload = self._request_json(
            Request(
                f"{repository_url}/git/ref/heads/{quote(vault.branch, safe='')}",
                headers=headers,
                method="GET",
            )
        )
        head_commit_sha = _require_nested_string(
            ref_payload,
            container_name="object",
            field_name="sha",
        )
        commit_payload = self._request_json(
            Request(
                f"{repository_url}/git/commits/{quote(head_commit_sha, safe='')}",
                headers=headers,
                method="GET",
            )
        )
        repository_tree_sha = _require_nested_string(
            commit_payload,
            container_name="tree",
            field_name="sha",
        )
        tree_sha = self._resolve_vault_tree_sha(
            repository_url=repository_url,
            token=token.value,
            tree_sha=repository_tree_sha,
            root_path=vault.root_path,
        )
        repository_path = _vault_repository_path(vault, target_path)
        contents = self._request_json_value(
            Request(
                f"{repository_url}/contents/{quote(repository_path, safe='/')}?"
                f"{urlencode({'ref': vault.branch})}",
                headers=headers,
                method="GET",
            ),
            allow_not_found=True,
        )
        if contents is None:
            target_blob_sha = None
            target_markdown = None
        elif not isinstance(contents, dict) or contents.get("type") != "file":
            raise GitHubGatewayError("GitHub target path is not a regular file")
        else:
            target_blob_sha = _require_string(contents, "sha")
            target_markdown = self._download_text_blob(
                repository_url=repository_url,
                token=token.value,
                blob_sha=target_blob_sha,
                max_bytes=MAX_MARKDOWN_BYTES,
            )
        return GitHubVaultTargetState(
            head_commit_sha=head_commit_sha,
            tree_sha=tree_sha,
            target_blob_sha=target_blob_sha,
            target_markdown=target_markdown,
        )

    def commit_vault_markdown(
        self,
        vault: ObsidianVault,
        *,
        target_path: str,
        markdown: str,
        expected_blob_sha: str | None,
    ) -> GitHubVaultCommitResult:
        """Создаёт один прямой commit в default branch через Contents API.

        Запрос записи намеренно не повторяется автоматически: после потерянного
        ответа повтор может создать второй commit. Pending proposal сохраняется,
        а следующий явный ответ снова начинает с проверки удалённых SHA.
        """
        if not markdown.strip():
            raise ValueError("markdown must not be empty")
        if expected_blob_sha is not None and not expected_blob_sha.strip():
            raise ValueError("expected_blob_sha must not be empty")
        token = self.create_installation_token(
            installation_id=vault.installation_id,
            repository_id=vault.repository_id,
        )
        repository_path = _vault_repository_path(vault, target_path)
        body: dict[str, Any] = {
            "message": (
                f"Knowledge Catcher: {'update' if expected_blob_sha else 'add'} "
                f"{target_path}"
            ),
            "content": base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
            "branch": vault.branch,
        }
        if expected_blob_sha is not None:
            body["sha"] = expected_blob_sha
        payload = self._request_json(
            Request(
                f"{_repository_api_url(vault)}/contents/"
                f"{quote(repository_path, safe='/')}",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    **self._api_headers(token.value),
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
        )
        content = payload.get("content")
        commit = payload.get("commit")
        if not isinstance(content, dict) or not isinstance(commit, dict):
            raise GitHubGatewayError("GitHub commit response has unexpected format")
        tree = commit.get("tree")
        if not isinstance(tree, dict):
            raise GitHubGatewayError("GitHub commit tree has unexpected format")
        repository_tree_sha = _require_string(tree, "sha")
        vault_tree_sha = self._resolve_vault_tree_sha(
            repository_url=_repository_api_url(vault),
            token=token.value,
            tree_sha=repository_tree_sha,
            root_path=vault.root_path,
        )
        result = GitHubVaultCommitResult(
            commit_sha=_require_string(commit, "sha"),
            tree_sha=vault_tree_sha,
            blob_sha=_require_string(content, "sha"),
        )
        LOGGER.info(
            "GitHub vault Markdown committed: app_user_id=%s vault_id=%s "
            "action=%s target_path=%s commit_sha=%s",
            vault.app_user_id,
            vault.id,
            "update" if expected_blob_sha is not None else "add",
            target_path,
            result.commit_sha,
        )
        return result

    def _resolve_vault_tree_sha(
        self,
        *,
        repository_url: str,
        token: str,
        tree_sha: str,
        root_path: str,
    ) -> str:
        current_sha = tree_sha
        for segment in root_path.split("/") if root_path else ():
            payload = self._request_json(
                Request(
                    f"{repository_url}/git/trees/{quote(current_sha, safe='')}",
                    headers=self._api_headers(token),
                    method="GET",
                )
            )
            entries = payload.get("tree")
            if not isinstance(entries, list):
                raise GitHubGatewayError(
                    "GitHub tree response has unexpected format"
                )
            match = next(
                (
                    item
                    for item in entries
                    if isinstance(item, dict)
                    and item.get("type") == "tree"
                    and item.get("path") == segment
                ),
                None,
            )
            if match is None or not isinstance(match.get("sha"), str):
                raise GitHubGatewayError("GitHub vault path no longer exists")
            current_sha = match["sha"]
        return current_sha

    def _download_changed_text_files(
        self,
        *,
        repository_url: str,
        token: str,
        changed: list[tuple[str, str]],
        max_bytes: int | None,
    ) -> dict[str, str]:
        if not changed:
            return {}
        with ThreadPoolExecutor(
            max_workers=min(MAX_BLOB_DOWNLOAD_WORKERS, len(changed))
        ) as executor:
            contents = executor.map(
                lambda item: self._download_text_blob(
                    repository_url=repository_url,
                    token=token,
                    blob_sha=item[1],
                    max_bytes=max_bytes,
                ),
                changed,
            )
            return {
                path: content
                for (path, _blob_sha), content in zip(changed, contents)
            }

    def _download_archive_text_files(
        self,
        *,
        repository_url: str,
        token: str,
        commit_sha: str,
        root_path: str,
        limits: Mapping[str, int],
    ) -> dict[str, str]:
        """Скачивает ZIP snapshot и безопасно читает только нужные UTF-8 файлы.

        Args:
            repository_url: REST URL выбранного repository.
            token: Краткоживущий installation token.
            commit_sha: Неизменяемый commit, которому должен соответствовать архив.
            root_path: Корень vault внутри repository.
            limits: Максимальный размер каждого требуемого пути.

        Returns:
            Соответствие vault-relative пути и его полного текста.

        Raises:
            GitHubGatewayError: Архив слишком велик, повреждён или не содержит
                согласованный с tree набор файлов.
        """
        if not limits:
            return {}
        archive_bytes = self._request_bytes(
            Request(
                f"{repository_url}/zipball/{quote(commit_sha, safe='')}",
                headers=self._api_headers(token),
                method="GET",
            ),
            max_bytes=MAX_ARCHIVE_BYTES,
        )
        normalized_root = root_path.strip("/")
        root_prefix = f"{normalized_root}/" if normalized_root else ""
        wanted = set(limits)
        extracted: dict[str, str] = {}
        extracted_bytes = 0
        try:
            with ZipFile(BytesIO(archive_bytes)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    parts = PurePosixPath(info.filename).parts
                    if (
                        len(parts) < 2
                        or info.filename.startswith("/")
                        or ".." in parts
                    ):
                        continue
                    repository_path = PurePosixPath(*parts[1:]).as_posix()
                    if root_prefix:
                        if not repository_path.startswith(root_prefix):
                            continue
                        relative_path = repository_path[len(root_prefix):]
                    else:
                        relative_path = repository_path
                    if relative_path not in wanted:
                        continue
                    if relative_path in extracted:
                        raise GitHubGatewayError(
                            "GitHub archive contains a duplicate vault path"
                        )
                    file_limit = limits[relative_path]
                    if info.file_size > file_limit:
                        raise GitHubGatewayError(
                            f"GitHub vault file exceeds size limit: {relative_path}"
                        )
                    with archive.open(info) as source:
                        content = source.read(file_limit + 1)
                    if len(content) > file_limit:
                        raise GitHubGatewayError(
                            f"GitHub vault file exceeds size limit: {relative_path}"
                        )
                    extracted_bytes += len(content)
                    if extracted_bytes > MAX_ARCHIVE_EXTRACTED_BYTES:
                        raise GitHubGatewayError(
                            "GitHub vault extracted content exceeds size limit"
                        )
                    extracted[relative_path] = content.decode("utf-8")
        except (BadZipFile, UnicodeDecodeError) as error:
            raise GitHubGatewayError(
                "GitHub repository archive is not a valid UTF-8 vault snapshot"
            ) from error
        missing = wanted - set(extracted)
        if missing:
            raise GitHubGatewayError(
                "GitHub repository archive is inconsistent with its tree"
            )
        return extracted

    def _download_text_blob(
        self,
        *,
        repository_url: str,
        token: str,
        blob_sha: str,
        max_bytes: int | None,
    ) -> str:
        payload = self._request_json(
            Request(
                f"{repository_url}/git/blobs/{quote(blob_sha, safe='')}",
                headers=self._api_headers(token),
                method="GET",
            )
        )
        if payload.get("encoding") != "base64":
            raise GitHubGatewayError("GitHub blob encoding is not base64")
        encoded = _require_string_allow_empty(payload, "content")
        try:
            decoded = base64.b64decode(encoded, validate=False)
        except (ValueError, UnicodeDecodeError) as error:
            raise GitHubGatewayError("GitHub text blob is invalid") from error
        if max_bytes is not None and len(decoded) > max_bytes:
            raise VaultConfigurationError(VaultConfigurationErrorCode.INVALID)
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GitHubGatewayError("GitHub text blob is invalid") from error

    def _request_bytes(
        self,
        request: Request,
        *,
        max_bytes: int,
    ) -> bytes:
        """Читает ограниченный бинарный GET с общими retry-правилами GitHub."""
        for attempt in range(1, self._read_attempts + 1):
            try:
                with self._request_opener(
                    request,
                    self._timeout_seconds,
                ) as response:
                    content_length = _parse_content_length(
                        getattr(response, "headers", None)
                    )
                    if content_length is not None and content_length > max_bytes:
                        raise _GitHubArchiveTooLargeError(
                            "GitHub repository archive exceeds size limit"
                        )
                    payload = response.read(max_bytes + 1)
                    if len(payload) > max_bytes:
                        raise _GitHubArchiveTooLargeError(
                            "GitHub repository archive exceeds size limit"
                        )
                    return payload
            except HTTPError as error:
                if (
                    not _is_retryable_http_error(error)
                    or attempt == self._read_attempts
                ):
                    raise GitHubGatewayError(
                        f"GitHub request failed with HTTP {error.code}"
                    ) from error
                self._wait_before_retry(
                    request,
                    error,
                    attempt,
                    self._read_attempts,
                )
            except (URLError, TimeoutError, OSError) as error:
                if attempt == self._read_attempts:
                    LOGGER.exception(
                        "GitHub archive request failed permanently: "
                        "url=%s cause=%s",
                        request.full_url,
                        _describe_exception_chain(error),
                    )
                    raise GitHubGatewayError(
                        "GitHub request failed: "
                        f"{_describe_exception_chain(error)}"
                    ) from error
                self._wait_before_retry(
                    request,
                    error,
                    attempt,
                    self._read_attempts,
                )
        raise RuntimeError("GitHub archive retry loop completed unexpectedly")

    def _create_repository_write_token(
        self,
        *,
        installation_id: int,
        owner: str,
        repository: str,
    ) -> GitHubInstallationAccessToken | None:
        """Выпускает token записи, ограниченный конкретным repository.

        GitHub отклоняет запрос, если repository не входит в installation или
        installation не получила `Contents: write`. Выданный token дополнительно
        содержит repository, на который он ограничен: это позволяет сверить полный
        owner/name, даже если одинаковое имя есть у разных владельцев.
        """
        body = {
            "repositories": [repository],
            "permissions": {
                "contents": "write",
                "metadata": "read",
            },
        }
        app_jwt = self._app_jwt_factory()
        payload = self._request_json_or_none(
            Request(
                f"{GITHUB_API_BASE_URL}/app/installations/"
                f"{installation_id}/access_tokens",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    **self._api_headers(app_jwt),
                    "Content-Type": "application/json",
                },
                method="POST",
            ),
            absent_statuses=frozenset({403, 404, 422}),
        )
        if payload is None:
            return None
        permissions = payload.get("permissions")
        if not isinstance(permissions, dict):
            raise GitHubGatewayError(
                "GitHub installation token permissions have unexpected format"
            )
        if permissions.get("contents") != "write":
            return None
        repositories = payload.get("repositories")
        if not isinstance(repositories, list):
            raise GitHubGatewayError(
                "GitHub installation token repositories have unexpected format"
            )
        expected_full_name = f"{owner}/{repository}".casefold()
        granted_full_names = {
            item.get("full_name").casefold()
            for item in repositories
            if isinstance(item, dict)
            and isinstance(item.get("full_name"), str)
        }
        if expected_full_name not in granted_full_names:
            return None
        return GitHubInstallationAccessToken(
            value=_require_string(payload, "token"),
            expires_at=_parse_github_timestamp(
                _require_string(payload, "expires_at")
            ),
        )

    def _post_form(self, url: str, values: dict[str, str]) -> dict[str, Any]:
        return self._request_json(
            Request(
                url,
                data=urlencode(values).encode("utf-8"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": GITHUB_USER_AGENT,
                },
                method="POST",
            )
        )

    def _api_headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": GITHUB_ACCEPT,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": GITHUB_USER_AGENT,
        }

    def _request_json(self, request: Request) -> dict[str, Any]:
        payload = self._request_json_value(request)
        if not isinstance(payload, dict):
            raise GitHubGatewayError("GitHub response is not a JSON object")
        return payload

    def _request_json_with_metadata(
        self,
        request: Request,
        *,
        allow_not_modified: bool,
    ) -> tuple[dict[str, Any], str | None, bool]:
        try:
            payload, headers = self._read_json_response(request)
            etag = headers.get("ETag") if headers is not None else None
        except HTTPError as error:
            if allow_not_modified and error.code == 304:
                return {}, None, True
            raise GitHubGatewayError(
                f"GitHub request failed with HTTP {error.code}"
            ) from error
        except (
            URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            LOGGER.exception(
                "GitHub request failed permanently: method=%s url=%s cause=%s",
                request.get_method(),
                request.full_url,
                _describe_exception_chain(error),
            )
            raise GitHubGatewayError(
                f"GitHub request failed: {_describe_exception_chain(error)}"
            ) from error
        if not isinstance(payload, dict):
            raise GitHubGatewayError("GitHub response is not a JSON object")
        return payload, etag, False

    def _request_json_or_none(
        self,
        request: Request,
        *,
        absent_statuses: frozenset[int] = frozenset({404}),
    ) -> dict[str, Any] | None:
        payload = self._request_json_value(
            request,
            absent_statuses=absent_statuses,
        )
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise GitHubGatewayError("GitHub response is not a JSON object")
        return payload

    def _request_json_value(
        self,
        request: Request,
        *,
        allow_not_found: bool = False,
        absent_statuses: frozenset[int] = frozenset(),
    ) -> Any | None:
        try:
            payload, _headers = self._read_json_response(request)
        except HTTPError as error:
            if (allow_not_found and error.code == 404) or (
                error.code in absent_statuses
            ):
                return None
            raise GitHubGatewayError(
                f"GitHub request failed with HTTP {error.code}"
            ) from error
        except (
            URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            LOGGER.exception(
                "GitHub request failed permanently: method=%s url=%s cause=%s",
                request.get_method(),
                request.full_url,
                _describe_exception_chain(error),
            )
            raise GitHubGatewayError(
                f"GitHub request failed: {_describe_exception_chain(error)}"
            ) from error
        return payload

    def _read_json_response(
        self,
        request: Request,
    ) -> tuple[Any, Any]:
        """Читает JSON и повторяет только временные ошибки безопасных GET-запросов.

        Args:
            request: Полностью сформированный HTTP-запрос GitHub.

        Returns:
            Декодированный JSON и заголовки ответа.

        Raises:
            HTTPError: GitHub вернул HTTP-ошибку, исчерпавшую повторы.
            OSError: Сетевая ошибка сохранилась после всех повторов.
            UnicodeDecodeError: Ответ не является UTF-8.
            json.JSONDecodeError: Ответ не является корректным JSON.
        """
        attempts = self._read_attempts if request.get_method() == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                with self._request_opener(
                    request,
                    self._timeout_seconds,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    return payload, getattr(response, "headers", None)
            except HTTPError as error:
                retryable = _is_retryable_http_error(error)
                if not retryable or attempt == attempts:
                    raise
                self._wait_before_retry(request, error, attempt, attempts)
            except (URLError, TimeoutError, OSError) as error:
                if attempt == attempts:
                    raise
                self._wait_before_retry(request, error, attempt, attempts)
        raise RuntimeError("GitHub request retry loop completed unexpectedly")

    def _wait_before_retry(
        self,
        request: Request,
        error: Exception,
        attempt: int,
        attempts: int,
    ) -> None:
        """Логирует исходную сетевую причину и выдерживает exponential backoff."""
        delay = _retry_after_seconds(error)
        if delay is None:
            delay = self._retry_base_delay_seconds * (2 ** (attempt - 1))
        LOGGER.warning(
            "GitHub read request failed; retrying: method=%s url=%s "
            "failed_attempt=%s/%s retry_delay_seconds=%.3f cause=%s",
            request.get_method(),
            request.full_url,
            attempt,
            attempts,
            delay,
            _describe_exception_chain(error),
        )
        self._sleeper(delay)


def _parse_content_length(headers: Any) -> int | None:
    """Читает безопасное неотрицательное значение Content-Length."""
    if headers is None:
        return None
    raw_value = headers.get("Content-Length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _retry_after_seconds(error: Exception) -> float | None:
    """Возвращает ограниченную задержку GitHub Retry-After, если она задана."""
    if not isinstance(error, HTTPError) or error.headers is None:
        return None
    raw_value = error.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return min(value, 120.0)


def _is_retryable_http_error(error: HTTPError) -> bool:
    """Разрешает retry 403 только при явной паузе secondary rate limit."""
    return (
        error.code in RETRYABLE_HTTP_STATUSES
        or (error.code == 403 and _retry_after_seconds(error) is not None)
    )


def _describe_exception_chain(error: BaseException) -> str:
    """Возвращает типы и сообщения вложенных исключений без HTTP-заголовков."""
    parts: list[str] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        message = str(current).strip()
        parts.append(
            f"{type(current).__name__}: {message}"
            if message
            else type(current).__name__
        )
        nested = current.__cause__
        if nested is None and not current.__suppress_context__:
            nested = current.__context__
        if nested is None and isinstance(current, URLError):
            reason = current.reason
            nested = reason if isinstance(reason, BaseException) else None
        current = nested
    return " <- ".join(parts)


def _repository_api_url(vault: ObsidianVault) -> str:
    """Возвращает REST URL repository без пользовательского содержимого."""
    return (
        f"{GITHUB_API_BASE_URL}/repos/{quote(vault.owner, safe='')}/"
        f"{quote(vault.repository, safe='')}"
    )


def _vault_repository_path(vault: ObsidianVault, target_path: str) -> str:
    """Безопасно объединяет root vault и относительный Markdown target."""
    if (
        not target_path
        or target_path.startswith("/")
        or "\\" in target_path
        or not target_path.lower().endswith(".md")
        or any(part in {"", ".", ".."} for part in target_path.split("/"))
    ):
        raise ValueError("target_path must be a safe relative Markdown path")
    return f"{vault.root_path}/{target_path}" if vault.root_path else target_path


def _require_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise GitHubGatewayError(f"GitHub response field is invalid: {name}")
    return value


def _require_string_allow_empty(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise GitHubGatewayError(f"GitHub response field is invalid: {name}")
    return value


def _require_positive_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GitHubGatewayError(f"GitHub response field is invalid: {name}")
    return value


def _require_non_negative_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GitHubGatewayError(f"GitHub response field is invalid: {name}")
    return value


def _require_nested_string(
    payload: dict[str, Any],
    *,
    container_name: str,
    field_name: str,
) -> str:
    container = payload.get(container_name)
    if not isinstance(container, dict):
        raise GitHubGatewayError(
            f"GitHub response field is invalid: {container_name}"
        )
    return _require_string(container, field_name)


def _parse_github_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GitHubGatewayError("GitHub timestamp has unexpected format") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
