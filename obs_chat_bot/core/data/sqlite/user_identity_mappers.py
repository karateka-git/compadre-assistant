"""Явно преобразует DTO пользователя и identity в общие domain-модели."""

from datetime import UTC, datetime

from obs_chat_bot.core.data.sqlite.user_identity_dtos import (
    AppUserDto,
    ExternalIdentityDto,
    IncomingIdentityDto,
    IdentityLinkTokenDto,
    IdentityRebindConfirmationDto,
)
from obs_chat_bot.core.domain.users.entities import (
    AppUser,
    ExternalIdentity,
    IncomingIdentity,
    IdentityRebindConfirmation,
)


def app_user_from_dto(dto: AppUserDto) -> AppUser:
    """Преобразует SQLite timestamp профиля в timezone-aware дату UTC."""
    created_at = datetime.fromisoformat(dto.created_at) if dto.created_at is not None else None
    if created_at is not None:
        created_at = (
            created_at.replace(tzinfo=UTC)
            if created_at.tzinfo is None
            else created_at.astimezone(UTC)
        )
    return AppUser(dto.id, dto.display_name, created_at)


def incoming_identity_to_dto(identity: IncomingIdentity) -> IncomingIdentityDto:
    """Передаёт внешнюю identity в отдельный формат SQL-параметров."""
    return IncomingIdentityDto(
        identity.channel,
        identity.external_user_id,
        identity.external_chat_id,
        identity.username,
        identity.display_name,
    )


def external_identity_from_dto(dto: ExternalIdentityDto) -> ExternalIdentity:
    """Возвращает смысловую связь пользователя из SQLite DTO."""
    return ExternalIdentity(
        dto.id,
        dto.app_user_id,
        dto.channel,
        dto.external_user_id,
        dto.external_chat_id,
        dto.username,
        dto.display_name,
    )


def link_token_owner_from_dto(dto: IdentityLinkTokenDto) -> int:
    """Возвращает владельца одноразового кода через публичный port."""
    return dto.app_user_id


def rebind_confirmation_from_dto(dto: IdentityRebindConfirmationDto) -> IdentityRebindConfirmation:
    """Восстанавливает адресную операцию ядра из отдельной модели хранения."""
    return IdentityRebindConfirmation(
        id=dto.id,
        source_app_user_id=dto.source_app_user_id,
        target_app_user_id=dto.target_app_user_id,
        channel=dto.channel,
        external_user_id=dto.external_user_id,
        chat_id=dto.origin_chat_id,
        token_hash=dto.token_hash,
        expires_at=datetime.fromisoformat(dto.expires_at),
        version=dto.version,
        status=dto.status,
    )
