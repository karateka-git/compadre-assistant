"""Представления семейных записей в SQLite и их преобразование."""

from dataclasses import dataclass
from datetime import datetime

from compadre_assistant.capabilities.family.domain.membership import Family, FamilyMember


@dataclass(frozen=True, slots=True)
class FamilyDto:
    """Хранит поля SQLite для чтения семьи."""

    id: int
    name: str
    owner_app_user_id: int
    created_at: str


@dataclass(frozen=True, slots=True)
class FamilyMemberDto:
    """Хранит запись членства и ID владельца из связанной семьи."""

    app_user_id: int
    joined_at: str
    owner_app_user_id: int


def family_from_dto(dto: FamilyDto) -> Family:
    """Преобразует дату создания семьи из формата хранения."""
    return Family(dto.id, dto.name, dto.owner_app_user_id, datetime.fromisoformat(dto.created_at))


def family_member_from_dto(dto: FamilyMemberDto) -> FamilyMember:
    """Преобразует дату вступления и вычисляет роль по владельцу семьи."""
    return FamilyMember(dto.app_user_id, datetime.fromisoformat(dto.joined_at), dto.app_user_id == dto.owner_app_user_id)
