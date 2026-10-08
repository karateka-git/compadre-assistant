"""Модели и ports сохранённой маршрутизации конкретного чата."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ChatModeKey:
    """Адресует режим внутренним пользователем, каналом и конкретным чатом.

    Raises:
        ValueError: ID пользователя неположительный или адрес чата пустой.
    """

    app_user_id: int
    channel: str
    chat_id: str

    def __post_init__(self) -> None:
        """Проверяет владельца и обязательные части адреса чата."""
        if self.app_user_id <= 0:
            raise ValueError("app_user_id must be positive")
        if not self.channel.strip() or not self.chat_id.strip():
            raise ValueError("channel and chat_id must not be empty")


@dataclass(frozen=True, slots=True)
class ChatMode:
    """Хранит только устойчивый ID возможности для маршрутизации чата.

    Raises:
        ValueError: ID возможности пустой.
    """

    key: ChatModeKey
    capability_id: str

    def __post_init__(self) -> None:
        """Проверяет непустой ID маршрута."""
        if not self.capability_id.strip():
            raise ValueError("capability_id must not be empty")


@dataclass(frozen=True, slots=True)
class ChatModeState:
    """Хранит версию выбора, включая очищенный режим конкретного чата.

    Нулевая ревизия означает, что запись для этого чата ещё не создавалась.
    """

    key: ChatModeKey
    capability_id: str | None
    revision: int


class ChatModeRepository(Protocol):
    """Задаёт хранение режима независимо от транспорта и предметного сценария."""

    def get(self, key: ChatModeKey) -> ChatMode | None:
        """Возвращает режим конкретного чата либо отсутствие выбора."""
        ...

    def get_state(self, key: ChatModeKey) -> ChatModeState:
        """Возвращает выбор и ревизию, сохраняя версию очищенного режима."""
        ...

    def replace_if_revision(
        self, key: ChatModeKey, expected_revision: int, capability_id: str | None
    ) -> bool:
        """Меняет выбор, только если ревизия не изменилась после чтения.

        Args:
            key: Адрес конкретного чата.
            expected_revision: Ревизия перед вызовом обработчика.
            capability_id: Новый ID или None для завершения режима.

        Returns:
            True при изменении, False при устаревшем результате.
        """
        ...

    def save(self, mode: ChatMode) -> None:
        """Атомарно заменяет режим только указанного пользователя и чата."""
        ...

    def activate_if_absent(self, mode: ChatMode) -> ChatMode:
        """Сохраняет первый выбор, не заменяя существующий при гонке процессов."""
        ...

    def clear(self, key: ChatModeKey) -> None:
        """Очищает выбор чата, сохраняя его ревизию и предметные данные."""
        ...
