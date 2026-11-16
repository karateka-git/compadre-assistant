"""Порт хранения состава семьи и атомарного изменения членства."""

from datetime import datetime
from typing import Protocol

from compadre_assistant.capabilities.family.domain.membership import Family, FamilyMember


class FamilyRepository(Protocol):
    """Хранит семью и выполняет предметные проверки в снимке каждой операции.

    app_user_id всегда обозначает действующего пользователя. family_id указывает
    семью, для которой начали действие. Изменения проверяют членство, роль и
    актуальность цели в той же транзакции, в которой применяют изменения.
    """

    def create(self, app_user_id: int, name: str, now: datetime) -> Family:
        """Создаёт семью с первым владельцем или сообщает об AlreadyInFamily."""
        ...

    def find_for_member(self, app_user_id: int) -> Family | None:
        """Возвращает текущую семью профиля, если он состоит в семье."""
        ...

    def list_members(self, app_user_id: int, family_id: int) -> tuple[FamilyMember, ...]:
        """Возвращает участников с владельцем первым или сообщает об FamilyAccessEnded."""
        ...

    def leave(self, app_user_id: int, family_id: int) -> None:
        """Исключает профиль по его решению с проверкой FamilyAccessEnded и OwnerCannotLeave."""
        ...

    def remove_member(self, app_user_id: int, family_id: int, member_id: int) -> None:
        """Исключает другого участника по решению владельца.

        Args:
            app_user_id: Профиль, выполняющий действие.
            family_id: Семья, указанная в подтверждении.
            member_id: Профиль участника, указанного в подтверждении.

        Raises:
            FamilyAccessEnded: Исполнитель больше не состоит в указанной семье.
            OwnerRequired: Исполнитель больше не владелец указанной семьи.
            OtherMemberRequired: Цель совпадает с исполнителем или больше не состоит в семье.
        """
        ...

    def transfer_ownership(self, app_user_id: int, family_id: int, member_id: int) -> None:
        """Передаёт владение другому участнику с теми же проверками, что remove_member."""
        ...

    def close(self, app_user_id: int, family_id: int) -> None:
        """Закрывает семью, если действующий владелец остался в ней один.

        Args:
            app_user_id: Профиль, выполняющий действие.
            family_id: Семья, указанная в подтверждении.

        Raises:
            FamilyAccessEnded: Исполнитель больше не состоит в указанной семье.
            OwnerRequired: Исполнитель больше не владелец указанной семьи.
            FamilyHasOtherMembers: В семье есть другие участники.
        """
        ...
