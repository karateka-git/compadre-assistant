"""Модели и операции общего списка покупок."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from compadre_assistant.capabilities.family.domain.membership import FamilyError


MAX_ITEM_NUMBER = 2**63 - 2  # Последнее значение зарезервировано для счётчика исчерпанных номеров.


class InvalidShoppingNames(FamilyError):
    """Во вводе нет непустой строки с названием покупки."""


class InvalidItemNumbers(FamilyError):
    """Операция требует положительных номеров в допустимом диапазоне."""


class ItemNumbersExhausted(FamilyError):
    """В списке больше нельзя выделить новые номера."""


class ShoppingItemStatus(StrEnum):
    """Состояние пункта общего списка."""

    NEEDED = "needed"  # Пункт ещё предстоит купить.
    BOUGHT = "bought"  # Покупка уже отмечена участником.
    UNKNOWN = "unknown"  # Хранилище вернуло незнакомый статус.


@dataclass(frozen=True, slots=True)
class ShoppingItem:
    """Покупка с неизменяемым номером внутри списка её семьи."""

    number: int
    name: str
    status: ShoppingItemStatus
    created_by_app_user_id: int | None
    created_at: datetime
    bought_by_app_user_id: int | None
    bought_at: datetime | None


class ItemChangeStatus(StrEnum):
    """Результат применения операции к одному номеру."""

    CHANGED = "changed"  # Пункт обновили или удалили.
    NOT_FOUND = "not_found"  # Номера больше нет в текущем списке.
    ALREADY_BOUGHT = "already_bought"  # Состояние уже установлено.
    UNKNOWN = "unknown"  # Пункт оставили без изменений из-за неизвестного статуса.


@dataclass(frozen=True, slots=True)
class ItemChange:
    """Результат действия с конкретным номером покупки."""

    number: int
    status: ItemChangeStatus


@dataclass(frozen=True, slots=True)
class ShoppingChangeResult:
    """Передаёт результаты номеров и список из той же транзакции."""

    changes: tuple[ItemChange, ...]
    items: tuple[ShoppingItem, ...]


class ShoppingRepository(Protocol):
    """Хранит покупки семьи с проверкой членства в каждой операции.

    app_user_id обозначает действующего участника, family_id — ожидаемую семью,
    actor_joined_at — время начала проверенного членства. Его смена приводит
    к FamilyAccessEnded. Операции записи возвращают
    снимок списка из транзакции, в которой применили изменение.
    """

    def list_items(
        self, app_user_id: int, family_id: int, *, actor_joined_at: datetime,
    ) -> tuple[ShoppingItem, ...]:
        """Возвращает видимый участнику список по возрастанию номеров."""
        ...

    def add_items(
        self, app_user_id: int, family_id: int, names: tuple[str, ...], now: datetime, *, actor_joined_at: datetime,
    ) -> tuple[ShoppingItem, ...]:
        """Выделяет номера и добавляет проверенные названия одним пакетом.

        Args:
            app_user_id: Профиль автора покупок.
            family_id: Ожидаемая семья.
            names: Непустой набор нормализованных названий в порядке ввода.
            now: Время создания покупок.
            actor_joined_at: Время начала членства, из которого открыт список.

        Returns:
            Обновлённый список.

        Raises:
            FamilyAccessEnded: Членство исполнителя завершилось или сменилось.
            ItemNumbersExhausted: Пакету не хватает свободных номеров.
        """
        ...

    def buy_items(
        self, app_user_id: int, family_id: int, numbers: tuple[int, ...], now: datetime, *, actor_joined_at: datetime,
    ) -> ShoppingChangeResult:
        """Отмечает найденные пункты купленными и сообщает о результате каждого номера."""
        ...

    def delete_items(
        self, app_user_id: int, family_id: int, numbers: tuple[int, ...], *, actor_joined_at: datetime,
    ) -> ShoppingChangeResult:
        """Удаляет найденные пункты и сообщает о результате каждого номера."""
        ...

    def clear_bought(
        self, app_user_id: int, family_id: int, *, actor_joined_at: datetime,
    ) -> tuple[ShoppingItem, ...]:
        """Удаляет все известные купленные пункты и возвращает остаток списка."""
        ...


class ManageShopping:
    """Обрабатывает покупки в семье от имени участника с проверенным членством."""

    def __init__(self, repository: ShoppingRepository, clock: Callable[[], datetime] | None = None) -> None:
        """Принимает хранилище покупок и источник текущего времени."""
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_items(
        self, app_user_id: int, family_id: int, *, actor_joined_at: datetime,
    ) -> tuple[ShoppingItem, ...]:
        """Возвращает общий список выбранной семьи для её участника."""
        return self._repository.list_items(app_user_id, family_id, actor_joined_at=actor_joined_at)

    def add(
        self, app_user_id: int, family_id: int, text: str, *, actor_joined_at: datetime,
    ) -> tuple[ShoppingItem, ...]:
        """Добавляет отдельную покупку на каждую непустую строку.

        Args:
            app_user_id: Профиль участника, добавляющего покупки.
            family_id: Ожидаемая семья.
            text: Названия и количества в исходном сообщении.
            actor_joined_at: Время начала членства, из которого открыт список.

        Returns:
            Обновлённый список покупок.

        Raises:
            InvalidShoppingNames: Ни одна строка не содержит названия.
            FamilyAccessEnded: Членство исполнителя завершилось или сменилось.
            ItemNumbersExhausted: Пакету не хватает свободных номеров.
        """
        names = tuple(name for line in text.splitlines() if (name := line.strip()))
        if not names:
            raise InvalidShoppingNames("Нужно указать названия покупок")
        return self._repository.add_items(app_user_id, family_id, names, self._clock(), actor_joined_at=actor_joined_at)

    def buy(
        self, app_user_id: int, family_id: int, numbers: tuple[int, ...], *, actor_joined_at: datetime,
    ) -> ShoppingChangeResult:
        """Отмечает покупки по проверенным номерам; отклоняет весь ввод при неверном номере."""
        return self._repository.buy_items(
            app_user_id, family_id, _validate_numbers(numbers), self._clock(), actor_joined_at=actor_joined_at,
        )

    def delete(
        self, app_user_id: int, family_id: int, numbers: tuple[int, ...], *, actor_joined_at: datetime,
    ) -> ShoppingChangeResult:
        """Удаляет покупки по проверенным номерам; отклоняет весь ввод при неверном номере."""
        return self._repository.delete_items(
            app_user_id, family_id, _validate_numbers(numbers), actor_joined_at=actor_joined_at,
        )

    def clear_bought(
        self, app_user_id: int, family_id: int, *, actor_joined_at: datetime,
    ) -> tuple[ShoppingItem, ...]:
        """Удаляет у выбранной семьи купленные пункты при действующем членстве."""
        return self._repository.clear_bought(app_user_id, family_id, actor_joined_at=actor_joined_at)


def _validate_numbers(numbers: tuple[int, ...]) -> tuple[int, ...]:
    if not numbers or any(type(number) is not int or not 0 < number <= MAX_ITEM_NUMBER for number in numbers):
        raise InvalidItemNumbers("Укажи положительные целые номера покупок")
    return numbers
