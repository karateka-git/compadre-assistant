"""Форматы данных SQLite для общих пользователей и связей каналов."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppUserDto:
    """Представляет прочитанную строку общего профиля."""

    id: int
    display_name: str | None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingIdentityDto:
    """Передаёт внешнюю identity в параметры SQL-запроса."""

    channel: str
    external_user_id: str
    external_chat_id: str
    username: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalIdentityDto:
    """Представляет сохранённую связь пользователя с каналом."""

    id: int
    app_user_id: int
    channel: str
    external_user_id: str
    external_chat_id: str
    username: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityLinkTokenDto:
    """Представляет ID найденного одноразового кода и его владельца."""

    id: int
    app_user_id: int


@dataclass(frozen=True, slots=True)
class IdentityRebindConfirmationDto:
    """Передаёт активное ожидание перепривязки из SQL."""

    token_hash: str
    target_app_user_id: int
