"""Адаптер сценариев ИИ к OpenAI-compatible Chat Completions API."""

from base64 import b64encode
from typing import Any

from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from compadre_assistant.capabilities.ai.data.configuration import AiConfig
from compadre_assistant.capabilities.ai.domain.errors import (
    AiModelTimeoutError,
    AiModelUnavailableError,
    InvalidAskAnswerError,
    InvalidOcrAnswerError,
)
from compadre_assistant.capabilities.ai.domain.ocr import OcrImage, OcrResult
from compadre_assistant.capabilities.ai.domain.ports.model import AskModel, OcrModel
from compadre_assistant.capabilities.ai.domain.use_cases.extract_image_text import OCR_NO_TEXT, OCR_TEXT_HEADER


class OpenAIModel(AskModel, OcrModel):
    """Отправляет независимые запросы и скрывает детали ошибок провайдера.

    Args:
        config: Проверенные настройки только возможности ИИ.
        client: Подменный SDK-клиент; его жизненным циклом владеет вызывающий.
    """

    def __init__(self, config: AiConfig, *, client: Any | None = None) -> None:
        """Собирает адаптер с клиентом SDK."""
        self._config = config
        self._owns_client = client is None
        self._client = client if client is not None else OpenAI(
            base_url=config.base_url, api_key=config.api_key,
            timeout=config.timeout_seconds, max_retries=0,
        )

    def answer(self, *, question: str, instruction: str) -> str:
        """Возвращает текст без истории, инструментов и материалов vault.

        Args:
            question: Проверенный текущий вопрос.
            instruction: Фиксированная инструкция сценария вопроса.

        Returns:
            Непустой текст в пределах настроенного ограничения символов.

        Raises:
            AiModelTimeoutError: Истекло время ожидания API.
            AiModelUnavailableError: Запрос API завершился ошибкой.
            InvalidAskAnswerError: Ответ пуст, некорректен или превышает лимит.
        """
        response = self._complete([{"role": "system", "content": instruction},
                                   {"role": "user", "content": question}])
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            raise InvalidAskAnswerError("Модель вернула некорректный ответ.") from None
        if (not isinstance(text, str) or not text.strip()
                or len(text) > self._config.max_response_chars):
            raise InvalidAskAnswerError("Модель вернула пустой или недопустимый текст ответа.")
        return text

    def recognize_text(self, *, image: OcrImage, instruction: str) -> OcrResult:
        """Передаёт изображение и возвращает только результат транскрипции.

        Args:
            image: Изображение с определённым по байтам MIME.
            instruction: Фиксированная инструкция сценария распознавания.

        Returns:
            Транскрипция с исходным форматированием либо результат без текста.

        Raises:
            AiModelTimeoutError: Если истёк таймаут модели.
            AiModelUnavailableError: Если провайдер недоступен.
            InvalidOcrAnswerError: Если ответ оборван или нарушает формат.
        """
        encoded = b64encode(image.data).decode("ascii")
        response = self._complete([{"role": "user", "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{encoded}"}},
        ]}])
        try:
            choice = response.choices[0]
            answer = choice.message.content
            finished = choice.finish_reason == "stop" and not getattr(choice.message, "refusal", None)
        except (AttributeError, IndexError, TypeError):
            raise InvalidOcrAnswerError("Модель вернула некорректный результат распознавания.") from None
        if finished and isinstance(answer, str):
            if answer.strip() == OCR_NO_TEXT:
                return OcrResult(text=None)
            header, _, text = answer.replace("\r\n", "\n").partition("\n")
            if header.strip() == OCR_TEXT_HEADER and text.strip() and len(text) <= self._config.max_response_chars:
                return OcrResult(text=text)
        raise InvalidOcrAnswerError("Модель вернула некорректный результат распознавания.")

    def _complete(self, messages: list[ChatCompletionMessageParam]) -> ChatCompletion:
        """Отправляет один запрос и преобразует ошибки SDK."""
        try:
            return self._client.chat.completions.create(
                model=self._config.model,
                messages=messages,
                max_tokens=self._config.max_response_tokens,
                timeout=self._config.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError):
            raise AiModelTimeoutError("Модель не ответила за отведённое время.") from None
        except Exception:
            # SDK может включать credentials или тело ответа в исключение.
            # Подавляем цепочку, чтобы обычный traceback тоже был безопасным.
            raise AiModelUnavailableError("Не удалось получить ответ модели.") from None

    def close(self) -> None:
        """Закрывает только SDK-клиент, созданный этим adapter."""
        if self._owns_client:
            self._client.close()
