"""Adapter Ask к OpenAI-compatible Chat Completions API."""

from collections.abc import Mapping
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import UUID, uuid4

from openai import APITimeoutError, OpenAI

from compadre_assistant.capabilities.ask.data.configuration import AskConfig
from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelTimeoutError,
    AskModelUnavailableError,
    InvalidAskAnswerError,
)
from compadre_assistant.capabilities.ask.domain.ports.model import AskModel
from compadre_assistant.capabilities.ask.domain.usage import (
    MAX_USAGE_INTEGER, TokenUsage, UsageObservation, UsageRecorder,
)


_logger = logging.getLogger(__name__)


class OpenAIAskModel(AskModel):
    """Отправляет независимые запросы и скрывает детали ошибок провайдера.

    Args:
        config: Проверенные настройки только возможности Ask.
        usage_recorder: Приём метаданных попыток вызова для фонового учёта.
        client: Подменный SDK-клиент; его жизненным циклом владеет вызывающий.
    """

    def __init__(self, config: AskConfig, *, usage_recorder: UsageRecorder, client: Any | None = None) -> None:
        """Собирает адаптер с процессным учётом и клиентом SDK."""
        self._config = config
        self._usage_recorder = usage_recorder
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
        response = None
        call_id = uuid4()
        started_at = datetime.now(UTC)
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
        finally:
            self._submit_usage(call_id, started_at, response)
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            raise InvalidAskAnswerError("Модель вернула некорректный ответ.") from None
        if (not isinstance(text, str) or not text.strip()
                or len(text) > self._config.max_response_chars):
            raise InvalidAskAnswerError("Модель вернула пустой или недопустимый текст ответа.")
        return text

    def _submit_usage(self, call_id: UUID, started_at: datetime, response: object) -> None:
        try:
            model = _field(response, "model")
            self._usage_recorder.submit(UsageObservation(
                call_id=call_id,
                started_at=started_at,
                provider=self._config.usage_provider,
                actual_model=model.strip() if isinstance(model, str) and model.strip() else None,
                tokens=_extract_token_usage(_field(response, "usage")),
            ))
        except Exception as error:
            # Учёт не должен подменять результат вызова или раскрывать тело ответа.
            _logger.error("AI usage observation failed: error_type=%s", type(error).__name__)

    def close(self) -> None:
        """Закрывает только SDK-клиент, созданный этим adapter."""
        if self._owns_client:
            self._client.close()


def _field(payload: object, name: str) -> object:
    if isinstance(payload, Mapping):
        return payload.get(name)
    return getattr(payload, name, None)


def _count(payload: object, name: str) -> int | None:
    value = _field(payload, name)
    return value if type(value) is int and 0 <= value <= MAX_USAGE_INTEGER else None


def _extract_token_usage(usage: object) -> TokenUsage | None:
    if usage is None:
        return None
    prompt = _field(usage, "prompt_tokens_details")
    completion = _field(usage, "completion_tokens_details")
    return TokenUsage(
        prompt_tokens=_count(usage, "prompt_tokens"),
        completion_tokens=_count(usage, "completion_tokens"),
        total_tokens=_count(usage, "total_tokens"),
        prompt_text_tokens=_count(prompt, "text_tokens"),
        image_tokens=_count(prompt, "image_tokens"),
        cached_tokens=_count(prompt, "cached_tokens"),
        completion_text_tokens=_count(completion, "text_tokens"),
        reasoning_tokens=_count(completion, "reasoning_tokens"),
    )
