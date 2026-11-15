"""Port независимого запроса к языковой модели."""

from typing import Protocol

from compadre_assistant.capabilities.ai.domain.ocr import OcrImage, OcrResult


class AskModel(Protocol):
    """Отвечает на текущий вопрос без накопления истории между вызовами."""

    def answer(self, *, question: str, instruction: str) -> str:
        """Получает текст ответа только по вопросу и фиксированной инструкции.

        Реализация не добавляет прежние вопросы, ответы, материалы vault или
        инструменты. Ошибки провайдера преобразуются в ошибки domain без ключей,
        тела ответа провайдера и других чувствительных деталей в сообщении.

        Args:
            question: Непустой вопрос, проверенный сценарием вопроса.
            instruction: Фиксированная инструкция сценария вопроса.

        Returns:
            Непустой текст ответа модели.

        Raises:
            AiModelTimeoutError: Если истекло время ожидания ответа.
            AiModelUnavailableError: Если API или соединение недоступны.
            InvalidAskAnswerError: Если провайдер вернул некорректный ответ.
        """


class OcrModel(Protocol):
    """Распознаёт текст без истории диалога и внешних инструментов."""

    def recognize_text(self, *, image: OcrImage, instruction: str) -> OcrResult:
        """Получает распознанный текст или явное отсутствие текста.

        Args:
            image: Проверенный по размеру и сигнатуре файл.
            instruction: Фиксированная инструкция с форматом результата.

        Returns:
            Текст с сохранёнными переносами либо результат без текста.

        Raises:
            AiModelTimeoutError: Если истёк таймаут модели.
            AiModelUnavailableError: Если API или соединение недоступны.
            InvalidOcrAnswerError: Если ответ модели неполный или некорректный.
        """
