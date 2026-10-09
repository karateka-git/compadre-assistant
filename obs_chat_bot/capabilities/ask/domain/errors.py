"""Типизированные ошибки проверки вопроса и получения ответа модели."""


class AskError(RuntimeError):
    """Базовая ожидаемая ошибка сценария Ask."""


class EmptyQuestionError(AskError):
    """Вопрос не содержит символов, отличных от пробельных."""


class QuestionTooLongError(AskError):
    """Вопрос после удаления внешних пробелов превышает допустимую длину."""


class AskModelError(AskError):
    """Базовая ожидаемая ошибка получения ответа через port модели."""


class AskModelTimeoutError(AskModelError):
    """Модель не ответила за отведённое время."""


class AskModelUnavailableError(AskModelError):
    """Запрос к провайдеру модели завершился ошибкой API или соединения."""


class InvalidAskAnswerError(AskModelError):
    """Модель вернула пустой ответ или результат неподдерживаемого формата."""
