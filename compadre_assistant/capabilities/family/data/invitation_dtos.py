"""Переносит сроки приглашения между SQLite и предметным сценарием."""

from dataclasses import dataclass
from datetime import datetime

from compadre_assistant.capabilities.family.domain.invitations import InvitationPeriod


@dataclass(frozen=True, slots=True)
class InvitationPeriodDto:
    """Хранит представление срока и отметки об использовании в SQLite."""

    created_at: str
    expires_at: str
    used_at: str | None


def invitation_period_from_dto(dto: InvitationPeriodDto) -> InvitationPeriod:
    """Читает даты SQLite в предметные значения времени."""
    return InvitationPeriod(
        datetime.fromisoformat(dto.created_at),
        datetime.fromisoformat(dto.expires_at),
        datetime.fromisoformat(dto.used_at) if dto.used_at is not None else None,
    )
