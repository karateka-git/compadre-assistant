from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReplyAddress:
    """Сохраняет адрес канала независимо от времени завершения операции."""

    channel: str
    chat_id: str

    def __post_init__(self) -> None:
        """Отклоняет пустые части адреса ответа."""
        if not self.channel.strip() or not self.chat_id.strip():
            raise ValueError("channel and chat_id must not be empty")


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    """Передаёт текст и адрес ответа из любого внешнего канала.

    Args:
        channel: Имя канала, например ``telegram`` или ``vk``.
        chat_id: Идентификатор конкретного чата в канале.
        message_id: Идентификатор сообщения для возможной дедупликации.
        text: Непустой исходный текст; пробельное значение проверяет владелец ввода.
        external_user_id: Идентификатор автора в канале, если он известен.
        username: Публичное имя автора, если оно доступно.
        display_name: Отображаемое имя автора, если оно доступно.

    Raises:
        ValueError: Если обязательное поле пусто.
    """

    channel: str
    chat_id: str
    message_id: str
    text: str
    external_user_id: str | None = None
    username: str | None = None
    display_name: str | None = None

    @property
    def reply_address(self) -> ReplyAddress:
        """Возвращает адрес немедленного и последующего ответа каналу."""
        return ReplyAddress(self.channel, self.chat_id)

    def __post_init__(self) -> None:
        """Проверяет обязательные поля события при создании."""
        if not all((self.channel.strip(), self.chat_id.strip(), self.message_id.strip(), self.text)):
            raise ValueError("channel, chat_id, message_id and text must not be empty")
        if self.external_user_id is not None and not self.external_user_id.strip():
            raise ValueError("external_user_id must not be empty")
