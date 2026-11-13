"""Port независимого запроса к языковой модели."""

from typing import Protocol

from compadre_assistant.capabilities.ask.domain.ocr import OcrImage, OcrResult


class AskModel(Protocol):
    """Отвечает на текущий вопрос без накопления истории между вызовами."""

    def answer(self, *, question: str, instruction: str) -> str:
        """Получает текст ответа только по вопросу и фиксированной инструкции.

        Реализация не добавляет прежние вопросы, ответы, материалы vault или
        инструменты. Ошибки провайдера преобразуются в ошибки domain без ключей,
        тела ответа провайдера и других чувствительных деталей в сообщении.

        Args:
            question: Непустой вопрос, проверенный сценарием Ask.
            instruction: Фиксированная инструкция, принадлежащая Ask.

        Returns:
            Непустой текст ответа модели.

        Raises:
            AskModelTimeoutError: Если истекло время ожидания ответа.
            AskModelUnavailableError: Если API или соединение недоступны.
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
            AskModelTimeoutError: Если истёк таймаут модели.
            AskModelUnavailableError: Если API или соединение недоступны.
            InvalidOcrAnswerError: Если ответ модели неполный или некорректный.
        """
