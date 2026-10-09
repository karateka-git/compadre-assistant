"""Сценарий одного вопроса и одного ответа без истории диалога."""

from collections.abc import Callable

from obs_chat_bot.capabilities.ask.domain.errors import (
    EmptyQuestionError,
    InvalidAskAnswerError,
    QuestionTooLongError,
)
from obs_chat_bot.capabilities.ask.domain.ports.model import AskModel


# Инструкция принадлежит возможности и одинакова для всех каналов и запросов.
ASK_INSTRUCTION = (
    "Ответь на текущий вопрос пользователя. Каждый вопрос независим: "
    "история диалога и материалы vault недоступны. Не утверждай, что прочитал "
    "содержимое ссылок или использовал инструменты. Если данных недостаточно, "
    "скажи об этом. Отвечай на языке вопроса, если пользователь не попросил иначе."
)


class AskQuestion:
    """Проверяет вопрос и получает ответ без хранения контекста вызовов.

    Args:
        model: Port независимого обращения к модели.
        max_question_chars: Максимальное число Unicode-символов вопроса после
            удаления внешних пробелов; значение передаёт composition root.

    Raises:
        ValueError: Если ограничение длины не является положительным целым.
    """

    def __init__(self, *, model: AskModel, max_question_chars: int) -> None:
        if type(max_question_chars) is not int or max_question_chars <= 0:
            raise ValueError("Ограничение длины вопроса должно быть положительным целым.")
        self._model = model
        self._max_question_chars = max_question_chars

    def execute(self, question: str, *, on_request: Callable[[], None] | None = None) -> str:
        """Отправляет модели только текущий вопрос и фиксированную инструкцию.

        URL и другие фрагменты вопроса остаются обычным текстом. Внешние
        пробелы вопроса удаляются; форматирование ответа сохраняется.

        Args:
            question: Текст одного вопроса пользователя.
            on_request: Необязательное уведомление после проверки вопроса перед API.

        Returns:
            Непустой текст ответа модели.

        Raises:
            EmptyQuestionError: Если после удаления пробелов вопрос пуст.
            QuestionTooLongError: Если длина вопроса превышает ограничение.
            AskModelTimeoutError: Если истекло время ожидания модели.
            AskModelUnavailableError: Если провайдер модели недоступен.
            InvalidAskAnswerError: Если модель не вернула непустую строку.
        """
        question = question.strip()
        if not question:
            raise EmptyQuestionError("Введите непустой вопрос.")
        if len(question) > self._max_question_chars:
            raise QuestionTooLongError(
                f"Вопрос должен содержать не более {self._max_question_chars} символов."
            )

        if on_request is not None:
            on_request()
        answer = self._model.answer(question=question, instruction=ASK_INSTRUCTION)
        if not isinstance(answer, str) or not answer.strip():
            raise InvalidAskAnswerError("Модель не вернула непустой текст ответа.")
        return answer
