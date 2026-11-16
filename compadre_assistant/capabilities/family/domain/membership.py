"""Семья, текущий состав и правила управления участниками."""

from dataclasses import dataclass
from datetime import datetime


class FamilyError(ValueError):
    """Предметный отказ при работе с семьёй."""


class InvalidFamilyName(FamilyError):
    """Название семьи не содержит текста."""


class AlreadyInFamily(FamilyError):
    """Профиль уже состоит в семье."""


class FamilyAccessEnded(FamilyError):
    """Профиль больше не состоит в семье, для которой запрошено действие."""


class OwnerRequired(FamilyError):
    """Действие доступно текущему владельцу семьи."""


class OwnerCannotLeave(FamilyError):
    """Владелец должен передать роль или закрыть семью."""

    def __init__(self, *, has_other_members: bool) -> None:
        """Указывает, есть ли участники, которым можно передать роль."""
        super().__init__("Владелец не может выйти из семьи")
        self.has_other_members = has_other_members


class FamilyHasOtherMembers(FamilyError):
    """Закрыть семью может владелец, оставшийся один."""


class OtherMemberRequired(FamilyError):
    """Действие требует другого действующего участника той же семьи."""


@dataclass(frozen=True, slots=True)
class Family:
    """Описывает семью и её текущего владельца."""

    id: int
    name: str
    owner_app_user_id: int
    created_at: datetime

    def require_owner(self, app_user_id: int) -> None:
        """Проверяет право действующего участника управлять семьёй.

        Args:
            app_user_id: Идентификатор участника, который выполняет действие.

        Raises:
            OwnerRequired: Участник не владелец семьи.
        """
        if app_user_id != self.owner_app_user_id:
            raise OwnerRequired("Действие доступно владельцу семьи")

    def require_may_leave(self, app_user_id: int, *, has_other_members: bool) -> None:
        """Проверяет, может ли участник выйти, сохранив владельца семьи.

        Args:
            app_user_id: Идентификатор действующего участника.
            has_other_members: В семье есть другие профили.

        Raises:
            OwnerCannotLeave: Участник сейчас владеет семьёй.
        """
        if app_user_id == self.owner_app_user_id:
            raise OwnerCannotLeave(has_other_members=has_other_members)


@dataclass(frozen=True, slots=True)
class FamilyMember:
    """Описывает участника без данных его профиля и каналов."""

    app_user_id: int
    joined_at: datetime
    is_owner: bool
