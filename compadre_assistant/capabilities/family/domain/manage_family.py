"""Входы предметных сценариев управления семьёй."""

from collections.abc import Callable
from datetime import UTC, datetime

from compadre_assistant.capabilities.family.domain.membership import Family, FamilyMember, InvalidFamilyName
from compadre_assistant.capabilities.family.domain.ports import FamilyRepository


class ManageFamily:
    """Принимает решения пользователя о семье через предметный порт."""

    def __init__(self, repository: FamilyRepository, clock: Callable[[], datetime] | None = None) -> None:
        """Принимает хранилище и, при необходимости, источник времени."""
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(self, app_user_id: int, name: str) -> Family:
        """Создаёт семью с пользователем в роли владельца.

        Args:
            app_user_id: Профиль создателя.
            name: Название, присланное пользователем.

        Returns:
            Созданная семья.

        Raises:
            InvalidFamilyName: Название пустое.
            AlreadyInFamily: Профиль уже состоит в семье.
        """
        normalized = name.strip()
        if not normalized:
            raise InvalidFamilyName("Название семьи не может быть пустым")
        return self._repository.create(app_user_id, normalized, self._clock())

    def find_for_member(self, app_user_id: int) -> Family | None:
        """Возвращает семью действующего профиля или None."""
        return self._repository.find_for_member(app_user_id)

    def list_members(self, app_user_id: int, family_id: int) -> tuple[FamilyMember, ...]:
        """Возвращает состав указанной семьи, если профиль в ней состоит."""
        return self._repository.list_members(app_user_id, family_id)

    def leave(self, app_user_id: int, family_id: int) -> None:
        """Выполняет решение участника выйти из указанной семьи."""
        self._repository.leave(app_user_id, family_id)

    def remove_member(self, app_user_id: int, family_id: int, member_id: int) -> None:
        """Выполняет решение владельца исключить другого участника указанной семьи."""
        self._repository.remove_member(app_user_id, family_id, member_id)

    def transfer_ownership(self, app_user_id: int, family_id: int, member_id: int) -> None:
        """Выполняет решение владельца передать свою роль другому участнику."""
        self._repository.transfer_ownership(app_user_id, family_id, member_id)

    def close(self, app_user_id: int, family_id: int) -> None:
        """Выполняет решение единственного участника закрыть свою семью."""
        self._repository.close(app_user_id, family_id)
