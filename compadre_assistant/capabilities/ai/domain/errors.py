"""Типизированные ошибки проверки вопроса и получения ответа модели."""


class AiError(RuntimeError):
    """Базовая ожидаемая ошибка сценариев ИИ."""


class EmptyQuestionError(AiError):
    """Вопрос не содержит символов, отличных от пробельных."""


class QuestionTooLongError(AiError):
    """Вопрос после удаления внешних пробелов превышает допустимую длину."""


class AiModelError(AiError):
    """Базовая ожидаемая ошибка получения ответа через port модели."""


class AiModelTimeoutError(AiModelError):
    """Модель не ответила за отведённое время."""


class AiModelUnavailableError(AiModelError):
    """Запрос к провайдеру модели завершился ошибкой API или соединения."""


class InvalidAskAnswerError(AiModelError):
    """Модель вернула пустой ответ или результат неподдерживаемого формата."""


class InvalidOcrImageError(AiError):
    """Байты изображения пусты, имеют неизвестную сигнатуру или превышают лимит."""


class InvalidOcrAnswerError(AiModelError):
    """Модель вернула незавершённый ответ либо нарушила формат распознавания."""
