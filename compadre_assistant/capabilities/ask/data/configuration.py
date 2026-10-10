"""Независимый профиль окружения для запросов Ask."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class AskConfigError(ValueError):
    """Настройки Ask отсутствуют или не соответствуют допустимому формату."""


@dataclass(frozen=True, slots=True)
class AskConfig:
    """Хранит настройки API и ограничения одного вопроса и ответа Ask.

    Ограничения символов измеряются в Unicode-символах; лимит токенов
    передаётся провайдеру. API key исключён из текстового представления.

    Args:
        base_url: Базовый HTTP(S) endpoint OpenAI-compatible API.
        api_key: Отдельный credential Ask.
        model: Идентификатор модели у провайдера.
        timeout_seconds: Таймаут сетевых операций SDK в секундах.
        max_question_chars: Максимальная длина нормализованного вопроса.
        max_response_tokens: Лимит генерации, передаваемый API.
        max_response_chars: Максимальная допустимая длина текста ответа.

    Raises:
        AskConfigError: Параметры профиля некорректны.
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 60.0
    max_question_chars: int = 12_000
    max_response_tokens: int = 2_000
    max_response_chars: int = 16_000

    def __post_init__(self) -> None:
        """Проверяет профиль, включая программную сборку без окружения."""
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise AskConfigError("ASK_API_KEY обязателен.")
        if not isinstance(self.model, str) or not self.model.strip():
            raise AskConfigError("ASK_MODEL обязателен.")
        try:
            url = urlsplit(self.base_url)
            valid_url = (url.scheme in {"http", "https"} and bool(url.hostname)
                         and url.username is None and url.password is None
                         and not url.query and not url.fragment
                         and not any(char.isspace() for char in self.base_url))
            url.port
        except (ValueError, TypeError, AttributeError):
            valid_url = False
        if not valid_url:
            raise AskConfigError("ASK_BASE_URL должен быть HTTP(S) endpoint без credentials и query.")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise AskConfigError("ASK_TIMEOUT_SECONDS должен быть положительным конечным числом.")
        for name in ("max_question_chars", "max_response_tokens", "max_response_chars"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise AskConfigError(f"ASK_{name.upper()} должен быть положительным целым.")


def load_ask_config(environ: Mapping[str, str] | None = None) -> AskConfig:
    """Читает только ASK_* без обращения к настройкам Obsidian.

    Args:
        environ: Подменное окружение либо окружение текущего процесса.

    Returns:
        Проверенный профиль Ask.

    Raises:
        AskConfigError: Обязательные поля отсутствуют или значения некорректны.
    """
    source = os.environ if environ is None else environ
    values = {}
    for key, default, converter in (
        ("timeout_seconds", "60", float),
        ("max_question_chars", "12000", int),
        ("max_response_tokens", "2000", int),
        ("max_response_chars", "16000", int),
    ):
        env_key = f"ASK_{key.upper()}"
        try:
            values[key] = converter(source.get(env_key, default))
        except (TypeError, ValueError):
            raise AskConfigError(f"Некорректное значение {env_key}.") from None
    return AskConfig(
        base_url=source.get("ASK_BASE_URL", "").strip(),
        api_key=source.get("ASK_API_KEY", "").strip(),
        model=source.get("ASK_MODEL", "").strip(),
        **values,
    )
