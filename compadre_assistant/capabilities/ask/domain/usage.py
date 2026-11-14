"""Модели и контракты записи метаданных вызовов ИИ."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID


# Максимум для отдельного счётчика токенов.
MAX_USAGE_INTEGER = 2**63 - 1


def _require_utc(value: datetime) -> None:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Время учёта должно быть задано в UTC.")


def _require_usage_integer(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_USAGE_INTEGER:
        raise ValueError("Значение учёта выходит за диапазон неотрицательного 64-битного целого.")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Хранит доступные счётчики одного ответа модели.

    None означает отсутствие счётчика, 0 — явное нулевое значение. Детали
    хранятся отдельно от общих счётчиков в том виде, в каком пришли от провайдера.

    Raises:
        ValueError: Счётчик не является неотрицательным 64-битным целым.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    prompt_text_tokens: int | None = None
    image_tokens: int | None = None
    cached_tokens: int | None = None
    completion_text_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        """Проверяет диапазон каждого предоставленного счётчика."""
        for count in (
            self.prompt_tokens, self.completion_tokens, self.total_tokens,
            self.prompt_text_tokens, self.image_tokens, self.cached_tokens,
            self.completion_text_tokens, self.reasoning_tokens,
        ):
            if count is not None:
                _require_usage_integer(count)


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """Хранит метаданные попытки вызова модели.

    Args:
        call_id: Внутренний ключ, созданный до сетевого вызова. При повторе
            записи одного результата ключ сохраняется.
        started_at: Время начала обращения в UTC.
        provider: Идентификатор профиля провайдера.
        actual_model: Модель из ответа провайдера либо None, если она неизвестна.
        tokens: Счётчики ответа либо None, если расход не был сообщён.

    Raises:
        ValueError: Время задано без UTC, провайдер или указанная модель пусты.
    """

    call_id: UUID
    started_at: datetime
    provider: str
    actual_model: str | None
    tokens: TokenUsage | None

    def __post_init__(self) -> None:
        """Проверяет время и идентификаторы наблюдения."""
        _require_utc(self.started_at)
        if not self.provider.strip() or (self.actual_model is not None and not self.actual_model.strip()):
            raise ValueError("Идентификаторы провайдера и указанной модели не могут быть пустыми.")


class UsageRecorder(Protocol):
    """Публичный приём наблюдений от сценариев, которые вызывают модель."""

    def submit(self, observation: UsageObservation) -> None:
        """Передаёт наблюдение на фоновую запись.

        Метод не ожидает записи в хранилище. Сбои учёта передаются в диагностику.

        Args:
            observation: Метаданные одного вызова с ключом, общим для повторов.
        """


class UsageObservationWriter(Protocol):
    """Записывает наблюдения с дедупликацией по ключу вызова."""

    def store(self, observation: UsageObservation) -> bool:
        """Атомарно сохраняет первое наблюдение по ключу вызова.

        Args:
            observation: Метаданные вызова с исходным ключом.

        Returns:
            True для новой строки или False, если ключ уже сохранён.
        """
