from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


class ImageLoadError(RuntimeError):
    """Сообщает о недоступном изображении без деталей транспорта."""

    def __init__(self) -> None:
        """Создаёт ошибку без адреса файла и данных канала."""
        super().__init__("Не удалось получить изображение.")


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """Хранит ленивый доступ к одному фото из исходного события.

    Args:
        loader: Функция адаптера канала, возвращающая байты в пределах
            лимита загрузки. Она вызывается только после принятия вложения.
    """

    loader: Callable[[], bytes] = field(repr=False)

    def read_bytes(self) -> bytes:
        """Загружает изображение без передачи деталей транспорта вызывающему.

        Returns:
            Непустые байты изображения.

        Raises:
            ImageLoadError: Адаптер не смог загрузить файл или вернул пустой файл.
        """
        try:
            data = self.loader()
            if isinstance(data, bytes) and data:
                return data
        except Exception:
            # Ошибка клиента может содержать адрес файла и токен канала.
            pass
        raise ImageLoadError()


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
    """Передаёт ввод и адрес ответа из любого внешнего канала.

    Args:
        channel: Имя канала, например ``telegram`` или ``vk``.
        chat_id: Идентификатор конкретного чата в канале.
        message_id: Идентификатор сообщения для возможной дедупликации.
        text: Исходный текст. Для сообщения с фото это подпись, которая
            отбрасывается; пробельное текстовое значение проверяет владелец ввода.
        external_user_id: Идентификатор автора в канале, если он известен.
        username: Публичное имя автора, если оно доступно.
        display_name: Отображаемое имя автора, если оно доступно.
        images: Ленивые ссылки на самостоятельные фото в порядке сообщения.

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
    images: tuple[ImageAttachment, ...] = ()

    @property
    def reply_address(self) -> ReplyAddress:
        """Возвращает адрес немедленного и последующего ответа каналу."""
        return ReplyAddress(self.channel, self.chat_id)

    def __post_init__(self) -> None:
        """Проверяет обязательные поля события при создании."""
        if not all((self.channel.strip(), self.chat_id.strip(), self.message_id.strip())):
            raise ValueError("channel, chat_id and message_id must not be empty")
        if self.images:
            object.__setattr__(self, "text", "")
        elif not self.text:
            raise ValueError("text must not be empty without attachments")
        if self.external_user_id is not None and not self.external_user_id.strip():
            raise ValueError("external_user_id must not be empty")
