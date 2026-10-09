"""Adapter Ask к OpenAI-compatible Chat Completions API."""

from typing import Any

from openai import APITimeoutError, OpenAI

from obs_chat_bot.capabilities.ask.data.configuration import AskConfig
from obs_chat_bot.capabilities.ask.domain.errors import (
    AskModelTimeoutError,
    AskModelUnavailableError,
    InvalidAskAnswerError,
)
from obs_chat_bot.capabilities.ask.domain.ports.model import AskModel


class OpenAIAskModel(AskModel):
    """Отправляет независимые запросы и скрывает детали ошибок провайдера.

    Args:
        config: Проверенные настройки только возможности Ask.
        client: Подменный SDK-клиент; его жизненным циклом владеет вызывающий.
    """

    def __init__(self, config: AskConfig, *, client: Any | None = None) -> None:
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
            instruction: Фиксированная инструкция сценария Ask.

        Returns:
            Непустой текст в пределах настроенного ограничения символов.

        Raises:
            AskModelTimeoutError: Истекло время ожидания API.
            AskModelUnavailableError: Запрос API завершился ошибкой.
            InvalidAskAnswerError: Ответ пуст, некорректен или превышает лимит.
        """
        try:
            response = self._client.chat.completions.create(
                model=self._config.model,
                messages=[{"role": "system", "content": instruction},
                          {"role": "user", "content": question}],
                max_tokens=self._config.max_response_tokens,
                timeout=self._config.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError):
            raise AskModelTimeoutError("Модель не ответила за отведённое время.") from None
        except Exception:
            # SDK может включать credentials или тело ответа в исключение.
            # Подавляем цепочку, чтобы обычный traceback тоже был безопасным.
            raise AskModelUnavailableError("Не удалось получить ответ модели.") from None
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            raise InvalidAskAnswerError("Модель вернула некорректный ответ.") from None
        if (not isinstance(text, str) or not text.strip()
                or len(text) > self._config.max_response_chars):
            raise InvalidAskAnswerError("Модель вернула пустой или недопустимый текст ответа.")
        return text

    def close(self) -> None:
        """Закрывает только SDK-клиент, созданный этим adapter."""
        if self._owns_client:
            self._client.close()
