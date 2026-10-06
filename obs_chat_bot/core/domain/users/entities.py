from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


MAX_DISPLAY_NAME_LENGTH = 80


@dataclass(frozen=True, slots=True)
class AppUser:
    """Представляет внутреннего пользователя приложения, общего для всех каналов."""

    id: int
    display_name: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("id must be positive")
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("display_name must not be empty")
        if (
            self.display_name is not None
            and len(self.display_name) > MAX_DISPLAY_NAME_LENGTH
        ):
            raise ValueError(
                f"display_name must not exceed {MAX_DISPLAY_NAME_LENGTH} characters"
            )


@dataclass(frozen=True, slots=True)
class IncomingIdentity:
    """Описывает внешнюю личность пользователя в конкретном канале."""

    channel: str
    external_user_id: str
    external_chat_id: str
    username: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not self.channel.strip():
            raise ValueError("channel must not be empty")
        if not self.external_user_id.strip():
            raise ValueError("external_user_id must not be empty")
        if not self.external_chat_id.strip():
            raise ValueError("external_chat_id must not be empty")
        if self.username is not None and not self.username.strip():
            raise ValueError("username must not be empty")
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("display_name must not be empty")


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """Связывает внутреннего пользователя с профилем во внешнем канале."""

    id: int
    app_user_id: int
    channel: str
    external_user_id: str
    external_chat_id: str
    username: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("id must be positive")
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        IncomingIdentity(
            channel=self.channel,
            external_user_id=self.external_user_id,
            external_chat_id=self.external_chat_id,
            username=self.username,
            display_name=self.display_name,
        )
