"""Личный выбор встроенных возможностей без зависимостей от транспорта и БД."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CapabilityDefault:
    """Задаёт устойчивый ID возможности и начальный личный выбор."""

    capability_id: str
    enabled_by_default: bool = False

    def __post_init__(self) -> None:
        """Отклоняет пустой или неоднозначный ID возможности."""
        if not self.capability_id or self.capability_id != self.capability_id.strip():
            raise ValueError("capability_id must not be empty or padded")


@dataclass(frozen=True, slots=True)
class UserCapabilityPreference:
    """Хранит явное переопределение выбора одного AppUser."""

    app_user_id: int
    capability_id: str
    enabled: bool

    def __post_init__(self) -> None:
        """Проверяет владельца и устойчивый ID настройки."""
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if not self.capability_id or self.capability_id != self.capability_id.strip():
            raise ValueError("capability_id must not be empty or padded")


@dataclass(frozen=True, slots=True)
class UserCapabilitySelection:
    """Показывает итоговый выбор и наличие явной настройки."""

    capability_id: str
    enabled: bool
    explicit: bool


class UserCapabilityPreferenceRepository(Protocol):
    """Сохраняет независимые личные настройки без знания каталога возможностей."""

    def get_for_user(self, app_user_id: int) -> tuple[UserCapabilityPreference, ...]:
        """Возвращает все явные настройки AppUser, включая неизвестные ID."""
        ...

    def upsert(self, preference: UserCapabilityPreference) -> bool | None:
        """Атомарно записывает один ID и возвращает прежний явный выбор.

        Args:
            preference: Новый личный выбор одного AppUser и ID.

        Returns:
            Прежнее значение либо None, если явной строки не было.
        """
        ...


class GetUserCapabilitySelections:
    """Читает личный набор по каталогу, применяя defaults к отсутствующим строкам."""

    def __init__(
        self,
        repository: UserCapabilityPreferenceRepository,
        defaults: tuple[CapabilityDefault, ...],
    ) -> None:
        """Фиксирует каталог ID и отклоняет его неоднозначность."""
        ids = [entry.capability_id for entry in defaults]
        if len(ids) != len(set(ids)):
            raise ValueError("capability IDs must be unique")
        self._repository = repository
        self._defaults = defaults

    def execute(self, app_user_id: int) -> tuple[UserCapabilitySelection, ...]:
        """Возвращает выбор только известных ID в порядке каталога.

        Args:
            app_user_id: Внутренний ID разрешённого AppUser.

        Raises:
            ValueError: ID пользователя неположительный.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        explicit = {
            item.capability_id: item.enabled
            for item in self._repository.get_for_user(app_user_id)
        }
        return tuple(
            UserCapabilitySelection(
                entry.capability_id,
                explicit.get(entry.capability_id, entry.enabled_by_default),
                entry.capability_id in explicit,
            )
            for entry in self._defaults
        )


class SetUserCapabilityPreference:
    """Меняет один известный ID, сохраняя независимость остальных настроек."""

    def __init__(
        self,
        repository: UserCapabilityPreferenceRepository,
        defaults: tuple[CapabilityDefault, ...],
    ) -> None:
        """Фиксирует начальный выбор известных ID."""
        ids = [entry.capability_id for entry in defaults]
        if len(ids) != len(set(ids)):
            raise ValueError("capability IDs must be unique")
        self._repository = repository
        self._defaults = {entry.capability_id: entry.enabled_by_default for entry in defaults}

    def execute(self, app_user_id: int, capability_id: str, enabled: bool) -> bool:
        """Сохраняет явный выбор и сообщает, изменился ли итоговый выбор.

        Args:
            app_user_id: Внутренний ID разрешённого AppUser.
            capability_id: Точный ID из каталога.
            enabled: Новый личный выбор.

        Returns:
            True, если значение до записи отличалось от нового.

        Raises:
            ValueError: Пользователь или ID недопустимы.
        """
        if app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if capability_id not in self._defaults:
            raise ValueError(f"unknown capability_id: {capability_id}")
        previous = self._repository.upsert(
            UserCapabilityPreference(app_user_id, capability_id, enabled)
        )
        return (self._defaults[capability_id] if previous is None else previous) != enabled
