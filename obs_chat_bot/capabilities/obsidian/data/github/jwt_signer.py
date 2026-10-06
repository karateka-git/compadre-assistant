from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import GitHubGatewayError


class PyJwtGitHubAppSigner:
    """Создаёт короткий RS256 JWT GitHub App из приватного PEM-ключа."""

    def __init__(
        self,
        *,
        client_id: str,
        private_key_path: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not client_id.strip():
            raise ValueError("client_id must not be empty")
        self._client_id = client_id
        self._private_key_path = private_key_path
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(self) -> str:
        """Читает PEM и подписывает JWT со сроком жизни менее десяти минут.

        Raises:
            GitHubGatewayError: Если ключ нельзя прочитать или подписать.
        """
        try:
            private_key = self._private_key_path.read_bytes()
        except OSError as error:
            raise GitHubGatewayError(
                f"GitHub App JWT could not be created: {type(error).__name__}"
            ) from error
        try:
            import jwt
        except ModuleNotFoundError as error:
            raise GitHubGatewayError("PyJWT is not installed") from error
        try:
            now = self._clock()
            return jwt.encode(
                {
                    "iat": int((now - timedelta(seconds=60)).timestamp()),
                    "exp": int((now + timedelta(minutes=9)).timestamp()),
                    "iss": self._client_id,
                },
                private_key,
                algorithm="RS256",
            )
        except Exception as error:
            raise GitHubGatewayError(
                f"GitHub App JWT could not be created: {type(error).__name__}"
            ) from error
