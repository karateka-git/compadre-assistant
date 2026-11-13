"""Вход и результат распознавания текста на одном изображении."""

from dataclasses import dataclass, field

from compadre_assistant.capabilities.ask.domain.errors import InvalidOcrImageError


# Верхний предел проверяется даже для вызовов вне адаптеров каналов.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OcrImage:
    """Хранит входные байты и определяет MIME по сигнатуре.

    Args:
        data: Полные байты одного изображения.

    Raises:
        InvalidOcrImageError: Файл превышает лимит или не имеет поддерживаемой сигнатуры.
    """

    data: bytes = field(repr=False)
    mime_type: str = field(init=False)

    def __post_init__(self) -> None:
        """Устанавливает MIME подтверждённой для OCR разновидности файла."""
        if not isinstance(self.data, bytes) or len(self.data) > _MAX_IMAGE_BYTES:
            raise InvalidOcrImageError("Недопустимый размер изображения.")
        if len(self.data) > 3 and self.data.startswith(b"\xff\xd8\xff"):
            mime_type = "image/jpeg"
        elif len(self.data) > 8 and self.data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime_type = "image/png"
        else:
            raise InvalidOcrImageError("Допустимы изображения JPEG или PNG.")
        object.__setattr__(self, "mime_type", mime_type)


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Передаёт текст или явное отсутствие текста на изображении.

    Args:
        text: Непустой исходный текст с переносами либо None при отсутствии текста.
    """

    text: str | None = field(repr=False)
