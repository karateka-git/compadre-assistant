"""OpenAI-compatible adapter для внешнего embedding API."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Generator
from dataclasses import dataclass
from decimal import Decimal
from numbers import Real
from typing import Any
from uuid import uuid4

from obs_chat_bot.capabilities.obsidian.application.search.errors import EmbeddingProviderError
from obs_chat_bot.capabilities.obsidian.application.search.models import EmbeddingCallContext
from obs_chat_bot.capabilities.obsidian.application.search.ports import EmbeddingProvider
from obs_chat_bot.capabilities.obsidian.data.embeddings.dtos import EmbeddingResponseItemDto
from obs_chat_bot.capabilities.obsidian.data.embeddings.mappers import embedding_vector_from_dto
from obs_chat_bot.capabilities.obsidian.domain.search.entities import EmbeddingVector


DEFAULT_EMBEDDING_BATCH_SIZE = 64
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBEDDING_MAX_RETRIES = 2
TOKENS_PER_MILLION = Decimal(1_000_000)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ResponseTelemetry:
    """Содержит безопасные provider metadata одного успешного response."""

    input_tokens: int | None
    total_tokens: int | None
    request_id: str | None


@dataclass(slots=True)
class _OperationTotals:
    """Накапливает usage и latency batches одной embedding-операции."""

    batches: int = 0
    input_tokens: int = 0
    total_tokens: int = 0
    usage_batches: int = 0
    latency_seconds: float = 0.0


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """Получает embeddings пакетами через совместимый `/v1/embeddings`.

    Args:
        base_url: Базовый URL API без завершающего `/`.
        api_key: Отдельный ключ embedding provider.
        document_model: ID модели для corpus documents.
        query_model: ID совместимой модели для поисковых запросов.
        batch_size: Максимальное число текстов в одном HTTP-запросе.
        timeout_seconds: Timeout одного запроса в секундах.
        max_retries: Число встроенных повторов SDK после первой попытки.
        price_per_million_tokens: Optional цена миллиона входных токенов.
        price_currency: Валюта оценочной цены либо `None` без тарифа.
        tariff_version: Версия тарифа либо `None` без тарифа.
        client: Необязательный готовый SDK-клиент для тестов.

    Raises:
        ValueError: Если конфигурация adapter некорректна.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        document_model: str,
        query_model: str,
        batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_EMBEDDING_MAX_RETRIES,
        price_per_million_tokens: Decimal | None = None,
        price_currency: str | None = None,
        tariff_version: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not document_model.strip():
            raise ValueError("document_model must not be empty")
        if not query_model.strip():
            raise ValueError("query_model must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        _validate_pricing(
            price_per_million_tokens=price_per_million_tokens,
            price_currency=price_currency,
            tariff_version=tariff_version,
        )
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key
        self._document_model = document_model.strip()
        self._query_model = query_model.strip()
        self._batch_size = batch_size
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._price_per_million_tokens = price_per_million_tokens
        self._price_currency = price_currency
        self._tariff_version = tariff_version
        self._client = client
        self._known_dimension: int | None = None

    @property
    def document_model(self) -> str:
        """Возвращает URI модели, которой векторизуется corpus."""
        return self._document_model

    @property
    def query_model(self) -> str:
        """Возвращает URI модели, которой векторизуется поисковый запрос."""
        return self._query_model

    def iter_document_batches(
        self,
        texts: tuple[str, ...],
        *,
        context: EmbeddingCallContext | None = None,
    ) -> Generator[tuple[EmbeddingVector, ...], None, None]:
        """Векторизует corpus chunks пакетами, сохраняя исходный порядок.

        Args:
            texts: Тексты chunks; пустой tuple не создаёт HTTP-запрос.

        Yields:
            Проверенные пакеты в исходном порядке. Следующий HTTP-запрос
            выполняется только после обработки пакета вызывающим кодом.

        Raises:
            ValueError: Если какой-либо текст пуст.
            EmbeddingProviderError: Если запрос или проверка ответа неуспешны.
        """
        _validate_texts(texts)
        if not texts:
            return
        expected_dimension: int | None = None
        operation_id = _operation_id(context)
        batch_count = (len(texts) + self._batch_size - 1) // self._batch_size
        totals = _OperationTotals()
        operation_started_at = time.monotonic()
        self._log_operation_started(
            operation_id=operation_id,
            operation="documents",
            context=context,
            model=self._document_model,
            text_count=len(texts),
            batch_count=batch_count,
            input_chars=sum(len(text) for text in texts),
        )
        try:
            for batch_index, start in enumerate(
                range(0, len(texts), self._batch_size),
                start=1,
            ):
                batch = texts[start : start + self._batch_size]
                batch_vectors, telemetry, latency = self._request(
                    batch,
                    model=self._document_model,
                    operation="documents",
                    operation_id=operation_id,
                    context=context,
                    batch_index=batch_index,
                    batch_count=batch_count,
                )
                _add_telemetry(totals, telemetry=telemetry, latency=latency)
                for vector in batch_vectors:
                    if expected_dimension is None:
                        expected_dimension = vector.dimension
                    elif vector.dimension != expected_dimension:
                        raise EmbeddingProviderError(
                            "Embedding response contains inconsistent dimensions"
                        )
                yield batch_vectors
        except Exception as error:
            if isinstance(error, EmbeddingProviderError):
                error.operation_id = operation_id
            self._log_operation_failed(
                operation_id=operation_id,
                operation="documents",
                context=context,
                model=self._document_model,
                text_count=len(texts),
                totals=totals,
                duration=time.monotonic() - operation_started_at,
                error=error,
            )
            raise
        self._log_operation_completed(
            operation_id=operation_id,
            operation="documents",
            context=context,
            model=self._document_model,
            text_count=len(texts),
            totals=totals,
            dimension=expected_dimension,
            duration=time.monotonic() - operation_started_at,
        )

    def embed_query(
        self,
        text: str,
        *,
        context: EmbeddingCallContext | None = None,
    ) -> EmbeddingVector:
        """Векторизует один поисковый запрос совместимой моделью.

        Args:
            text: Непустой поисковый запрос.

        Returns:
            Проверенный semantic-вектор.

        Raises:
            ValueError: Если запрос пуст.
            EmbeddingProviderError: Если provider недоступен или ответ неверен.
        """
        _validate_texts((text,))
        operation_id = _operation_id(context)
        started_at = time.monotonic()
        totals = _OperationTotals()
        self._log_operation_started(
            operation_id=operation_id,
            operation="query",
            context=context,
            model=self._query_model,
            text_count=1,
            batch_count=1,
            input_chars=len(text),
        )
        try:
            vectors, telemetry, latency = self._request(
                (text,),
                model=self._query_model,
                operation="query",
                operation_id=operation_id,
                context=context,
                batch_index=1,
                batch_count=1,
            )
            _add_telemetry(totals, telemetry=telemetry, latency=latency)
        except Exception as error:
            if isinstance(error, EmbeddingProviderError):
                error.operation_id = operation_id
            self._log_operation_failed(
                operation_id=operation_id,
                operation="query",
                context=context,
                model=self._query_model,
                text_count=1,
                totals=totals,
                duration=time.monotonic() - started_at,
                error=error,
            )
            raise
        vector = vectors[0]
        self._log_operation_completed(
            operation_id=operation_id,
            operation="query",
            context=context,
            model=self._query_model,
            text_count=1,
            totals=totals,
            dimension=vector.dimension,
            duration=time.monotonic() - started_at,
        )
        return vector

    def _request(
        self,
        texts: tuple[str, ...],
        *,
        model: str,
        operation: str,
        operation_id: str,
        context: EmbeddingCallContext | None,
        batch_index: int,
        batch_count: int,
    ) -> tuple[tuple[EmbeddingVector, ...], _ResponseTelemetry, float]:
        """Выполняет один HTTP-запрос и проверяет порядок response items."""
        started_at = time.monotonic()
        try:
            raw_response = self._get_client().embeddings.with_raw_response.create(
                model=model,
                input=list(texts),
                encoding_format="float",
            )
        except Exception as error:
            latency = time.monotonic() - started_at
            self._log_request_failed(
                operation_id=operation_id,
                operation=operation,
                context=context,
                model=model,
                batch_index=batch_index,
                batch_count=batch_count,
                batch_size=len(texts),
                input_chars=sum(len(text) for text in texts),
                latency=latency,
                error=error,
                status=("http_error" if getattr(error, "status_code", None) is not None
                        else "transport_error" if type(error).__name__ in
                        {"APIConnectionError", "APITimeoutError", "TimeoutError", "ConnectionError"}
                        else "request_error"),
            )
            raise EmbeddingProviderError(
                f"Embedding request failed: {type(error).__name__}",
                operation_id=operation_id,
            ) from error
        latency = time.monotonic() - started_at
        http_response = raw_response.http_response
        response = None
        telemetry = _response_telemetry(response, http_response.headers.get("x-request-id"))
        try:
            response = http_response.json()
            telemetry = _response_telemetry(response, http_response.headers.get("x-request-id"))
            items = _response_items(response)
            if len(items) != len(texts):
                raise ValueError("response item count does not match input count")
            ordered = sorted(items, key=lambda item: item.index)
            if [item.index for item in ordered] != list(range(len(texts))):
                raise ValueError("response indices do not match input order")
            vectors = tuple(
                embedding_vector_from_dto(item, model=model) for item in ordered
            )
            self._validate_compatible_dimension(vectors)
            self._log_request_completed(
                operation_id=operation_id,
                operation=operation,
                context=context,
                model=model,
                batch_index=batch_index,
                batch_count=batch_count,
                batch_size=len(texts),
                input_chars=sum(len(text) for text in texts),
                vectors=vectors,
                telemetry=telemetry,
                latency=latency,
            )
            return vectors, telemetry, latency
        except (
            AttributeError,
            TypeError,
            ValueError,
            OverflowError,
            EmbeddingProviderError,
        ) as error:
            self._log_request_failed(
                operation_id=operation_id,
                operation=operation,
                context=context,
                model=model,
                batch_index=batch_index,
                batch_count=batch_count,
                batch_size=len(texts),
                input_chars=sum(len(text) for text in texts),
                latency=latency,
                error=error,
                status="invalid_response",
                telemetry=telemetry,
                http_status=http_response.status_code,
                response_shape=_response_shape(response, expected_count=len(texts)),
            )
            if isinstance(error, EmbeddingProviderError):
                error.operation_id = operation_id
                raise
            raise EmbeddingProviderError(
                "Embedding response has unexpected format",
                operation_id=operation_id,
            ) from error

    def _log_operation_started(self, **values: Any) -> None:
        """Логирует безопасное начало логической embedding-операции."""
        batch_count = values.pop("batch_count")
        input_chars = values.pop("input_chars")
        LOGGER.info(
            "Embedding operation started: event=embedding_operation_started %s "
            "batch_count=%s input_chars=%s status=started",
            self._operation_fields(**values),
            batch_count,
            input_chars,
        )

    def _log_operation_completed(
        self,
        *,
        totals: _OperationTotals,
        dimension: int | None,
        duration: float,
        **values: Any,
    ) -> None:
        """Логирует итоговые usage, latency и оценочную стоимость операции."""
        LOGGER.info(
            "Embedding operation completed: event=embedding_operation_completed "
            "%s batches=%s usage_batches=%s input_tokens=%s total_tokens=%s "
            "dimension=%s duration_seconds=%.3f estimated_cost=%s currency=%s "
            "tariff_version=%s sdk_latency_seconds=%.3f errors=0 status=success",
            self._operation_fields(**values),
            totals.batches,
            totals.usage_batches,
            totals.input_tokens if totals.usage_batches else "none",
            totals.total_tokens if totals.usage_batches else "none",
            dimension if dimension is not None else "none",
            duration,
            self._estimated_cost(totals.input_tokens, totals.usage_batches),
            self._price_currency or "none",
            self._tariff_version or "none",
            totals.latency_seconds,
        )

    def _log_operation_failed(
        self,
        *,
        totals: _OperationTotals,
        duration: float,
        error: Exception,
        **values: Any,
    ) -> None:
        """Логирует окончательный тип ошибки без текста запроса и exception."""
        LOGGER.error(
            "Embedding operation failed: event=embedding_operation_failed %s "
            "completed_batches=%s duration_seconds=%.3f status=failed "
            "sdk_latency_seconds=%.3f usage_batches=%s input_tokens=%s "
            "total_tokens=%s estimated_cost=%s currency=%s tariff_version=%s "
            "errors=1 error_type=%s cause_type=%s",
            self._operation_fields(**values),
            totals.batches,
            duration,
            totals.latency_seconds,
            totals.usage_batches,
            totals.input_tokens if totals.usage_batches else "none",
            totals.total_tokens if totals.usage_batches else "none",
            self._estimated_cost(totals.input_tokens, totals.usage_batches),
            self._price_currency or "none",
            self._tariff_version or "none",
            type(error).__name__,
            _root_cause_type(error),
        )

    def _log_request_completed(
        self,
        *,
        vectors: tuple[EmbeddingVector, ...],
        telemetry: _ResponseTelemetry,
        latency: float,
        **values: Any,
    ) -> None:
        """Логирует один успешный SDK batch без исходных данных."""
        LOGGER.info(
            "Embedding request completed: event=embedding_request_completed %s "
            "dimension=%s input_tokens=%s total_tokens=%s request_id=%s "
            "latency_seconds=%.3f estimated_cost=%s currency=%s "
            "tariff_version=%s status=success",
            self._request_fields(**values),
            vectors[0].dimension,
            telemetry.input_tokens if telemetry.input_tokens is not None else "none",
            telemetry.total_tokens if telemetry.total_tokens is not None else "none",
            telemetry.request_id or "none",
            latency,
            self._estimated_cost(telemetry.input_tokens, 1),
            self._price_currency or "none",
            self._tariff_version or "none",
        )

    def _log_request_failed(
        self,
        *,
        latency: float,
        error: Exception,
        status: str = "failed",
        telemetry: _ResponseTelemetry | None = None,
        http_status: int | None = None,
        response_shape: str = "",
        **values: Any,
    ) -> None:
        """Логирует финальный batch failure после внутренних retries SDK."""
        LOGGER.error(
            "Embedding request failed: event=embedding_request_failed %s "
            "latency_seconds=%.3f status=%s http_status=%s request_id=%s "
            "error_type=%s cause_type=%s sdk_max_retries=%s %s",
            self._request_fields(**values),
            latency,
            status,
            http_status or getattr(error, "status_code", None) or "none",
            (
                telemetry.request_id
                if telemetry is not None and telemetry.request_id is not None
                else _safe_request_id(getattr(error, "request_id", None)) or "none"
            ),
            type(error).__name__,
            _root_cause_type(error),
            self._max_retries,
            response_shape,
        )

    def _operation_fields(
        self,
        *,
        operation_id: str,
        operation: str,
        context: EmbeddingCallContext | None,
        model: str,
        text_count: int,
        **values: Any,
    ) -> str:
        """Формирует общие стабильные поля одной операции."""
        return (
            f"operation_id={operation_id} operation={operation} "
            f"app_user_id={_context_value(context, 'app_user_id')} "
            f"vault_id={_context_value(context, 'vault_id')} "
            f"article_id={_context_value(context, 'article_id')} "
            f"provider=openai_compatible model={model} text_count={text_count} "
            f"sdk_max_retries={self._max_retries}"
        )

    def _request_fields(self, **values: Any) -> str:
        """Добавляет к operation scope безопасные batch counters."""
        batch_index = values.pop("batch_index")
        batch_count = values.pop("batch_count")
        batch_size = values.pop("batch_size")
        input_chars = values.pop("input_chars")
        return (
            f"{self._operation_fields(text_count=batch_size, **values)} "
            f"batch_index={batch_index} batch_count={batch_count} "
            f"batch_size={batch_size} input_chars={input_chars}"
        )

    def _estimated_cost(
        self,
        input_tokens: int | None,
        usage_batches: int,
    ) -> str:
        """Возвращает оценку по входным токенам либо `none` без usage/тарифа."""
        if (
            input_tokens is None
            or usage_batches <= 0
            or self._price_per_million_tokens is None
        ):
            return "none"
        cost = (
            Decimal(input_tokens)
            * self._price_per_million_tokens
            / TOKENS_PER_MILLION
        )
        return format(cost.normalize(), "f")

    def _validate_compatible_dimension(
        self,
        vectors: tuple[EmbeddingVector, ...],
    ) -> None:
        """Проверяет, что document/query модели создают совместимые векторы."""
        dimensions = {vector.dimension for vector in vectors}
        if len(dimensions) != 1:
            raise EmbeddingProviderError(
                "Embedding response contains inconsistent dimensions"
            )
        for vector in vectors:
            if self._known_dimension is None:
                self._known_dimension = vector.dimension
            elif vector.dimension != self._known_dimension:
                raise EmbeddingProviderError(
                    "Document and query embedding dimensions are incompatible"
                )

    def _get_client(self) -> Any:
        """Лениво создаёт SDK-клиент с ограниченным timeout и retry."""
        if self._client is None:
            try:
                from openai import OpenAI
            except ModuleNotFoundError as error:
                raise EmbeddingProviderError(
                    "openai package is not installed"
                ) from error
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                timeout=self._timeout_seconds,
                max_retries=self._max_retries,
            )
        return self._client


def _validate_texts(texts: tuple[str, ...]) -> None:
    """Не допускает запросов с пустыми или нестроковыми элементами."""
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("embedding texts must contain only non-empty strings")


def _validate_pricing(
    *,
    price_per_million_tokens: Decimal | None,
    price_currency: str | None,
    tariff_version: str | None,
) -> None:
    """Проверяет optional all-or-none тариф без привязки adapter к валюте."""
    values = (price_per_million_tokens, price_currency, tariff_version)
    if all(value is None for value in values):
        return
    if any(value is None for value in values):
        raise ValueError("embedding pricing must be configured as one complete group")
    if (
        price_per_million_tokens is None
        or not price_per_million_tokens.is_finite()
        or price_per_million_tokens < 0
    ):
        raise ValueError("price_per_million_tokens must be finite and not negative")
    if (
        price_currency is None
        or len(price_currency) != 3
        or not price_currency.isalpha()
        or price_currency != price_currency.upper()
    ):
        raise ValueError("price_currency must contain three uppercase letters")
    if tariff_version is None or not tariff_version.strip():
        raise ValueError("tariff_version must not be empty")


def _operation_id(context: EmbeddingCallContext | None) -> str:
    """Возвращает correlation ID caller либо создаёт локальный ID adapter."""
    return context.operation_id if context is not None else uuid4().hex


def _add_telemetry(
    totals: _OperationTotals,
    *,
    telemetry: _ResponseTelemetry,
    latency: float,
) -> None:
    """Добавляет метрики завершённого SDK batch в итог логической операции."""
    totals.batches += 1
    totals.latency_seconds += latency
    if telemetry.input_tokens is None and telemetry.total_tokens is None:
        return
    totals.usage_batches += 1
    totals.input_tokens += telemetry.input_tokens or 0
    totals.total_tokens += telemetry.total_tokens or 0


def _safe_request_id(value: Any) -> str | None:
    """Ограничивает metadata request ID одной безопасной строкой."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
        return value
    return None


def _response_telemetry(response: Any, request_id: Any) -> _ResponseTelemetry:
    """Читает только валидные usage counters и request ID из HTTP metadata."""
    usage = response.get("usage") if isinstance(response, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _non_negative_int(usage.get("prompt_tokens"))
    if input_tokens is None:
        input_tokens = _non_negative_int(usage.get("input_tokens"))
    return _ResponseTelemetry(
        input_tokens=input_tokens,
        total_tokens=_non_negative_int(usage.get("total_tokens")),
        request_id=_safe_request_id(request_id),
    )


def _response_shape(response: Any, *, expected_count: int) -> str:
    """Описывает форму JSON фиксированными полями без значений и исходных ключей."""
    data = response.get("data") if isinstance(response, dict) else None
    items = data if isinstance(data, list) else []
    def types(values: Any) -> str:
        """Сводит JSON-типы к ограниченному набору имён без значений."""
        return ",".join(sorted({type(value).__name__ for value in values})) or "none"
    return (
        f"expected_vectors={expected_count} actual_vectors={len(items) if isinstance(data, list) else 'unknown'} "
        f"response_type={type(response).__name__} "
        f"data_present={int(isinstance(response, dict) and 'data' in response)} "
        f"data_type={type(data).__name__} item_types={types(items)} "
        f"index_present_count={sum(isinstance(item, dict) and 'index' in item for item in items)} "
        f"embedding_present_count={sum(isinstance(item, dict) and 'embedding' in item for item in items)} "
        f"index_types={types(item.get('index') for item in items if isinstance(item, dict))} "
        f"embedding_types={types(item.get('embedding') for item in items if isinstance(item, dict))} "
        f"value_types={types(value for item in items if isinstance(item, dict) and isinstance(item.get('embedding'), list) for value in item['embedding'])}"
    )


def _non_negative_int(value: Any) -> int | None:
    """Принимает только целочисленное неотрицательное usage-значение."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _context_value(
    context: EmbeddingCallContext | None,
    attribute: str,
) -> int | str:
    """Возвращает безопасный scope field либо `none` без контекста caller."""
    if context is None:
        return "none"
    value = getattr(context, attribute)
    return value if value is not None else "none"


def _root_cause_type(error: BaseException) -> str:
    """Возвращает только тип глубинной причины, не раскрывая её сообщение."""
    current = error
    visited: set[int] = set()
    while current.__cause__ is not None and id(current) not in visited:
        visited.add(id(current))
        current = current.__cause__
    return type(current).__name__


def _response_items(response: Any) -> tuple[EmbeddingResponseItemDto, ...]:
    """Извлекает и типизирует items SDK-ответа без утечки исходных текстов."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise ValueError("embedding data must be an array")
    raw_items = response["data"]
    items: list[EmbeddingResponseItemDto] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("embedding item must be an object")
        index = raw_item.get("index")
        raw_values = raw_item.get("embedding")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("embedding index must be a non-negative integer")
        if not isinstance(raw_values, (list, tuple)) or not raw_values:
            raise ValueError("embedding values must be a non-empty sequence")
        if any(
            isinstance(value, bool) or not isinstance(value, Real)
            for value in raw_values
        ):
            raise ValueError("embedding values must be numeric")
        items.append(
            EmbeddingResponseItemDto(
                index=index,
                values=tuple(float(value) for value in raw_values),
            )
        )
    return tuple(items)
