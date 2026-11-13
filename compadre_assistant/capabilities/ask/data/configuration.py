"""Независимый профиль окружения для запросов Ask."""

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from compadre_assistant.capabilities.ask.domain.usage_pricing import UsageDetail, UsagePricing, UsageTariff


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
        usage_provider: Стабильное имя профиля для сопоставления с каталогом.
        usage_tariffs: Снимки настроенных оператором тарифов.

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
    usage_provider: str = "ask"
    usage_tariffs: tuple[UsageTariff, ...] = ()

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
        if not isinstance(self.usage_provider, str) or not self.usage_provider.strip():
            raise AskConfigError("ASK_USAGE_PROVIDER должен содержать имя профиля.")
        try:
            UsagePricing(self.usage_tariffs)
        except ValueError as error:
            raise AskConfigError(str(error)) from None


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
        usage_provider=source.get("ASK_USAGE_PROVIDER", "ask").strip() or "ask",
        usage_tariffs=_load_usage_tariffs(source.get("ASK_USAGE_TARIFFS_JSON", "")),
        **values,
    )


def _load_usage_tariffs(raw: str) -> tuple[UsageTariff, ...]:
    if not raw.strip():
        return ()
    try:
        catalog = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise AskConfigError("ASK_USAGE_TARIFFS_JSON должен содержать корректный JSON без повторов полей.") from None
    if (not isinstance(catalog, dict) or catalog.keys() != {"version", "tariffs"}
            or type(catalog["version"]) is not int or catalog["version"] != 1
            or not isinstance(catalog["tariffs"], list)):
        raise AskConfigError("В ASK_USAGE_TARIFFS_JSON ожидаются version=1 и список tariffs.")
    tariffs = []
    for index, entry in enumerate(catalog["tariffs"], start=1):
        try:
            tariffs.append(_parse_usage_tariff(entry))
        except (TypeError, ValueError):
            raise AskConfigError(f"Запись {index} в ASK_USAGE_TARIFFS_JSON некорректна.") from None
    return tuple(tariffs)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повтор поля JSON.")
        result[key] = value
    return result


def _parse_usage_tariff(entry: object) -> UsageTariff:
    required = {"provider", "model", "source", "effective_from", "prompt_rate", "completion_rate"}
    optional = {"effective_until", "image_rate", "cached_rate", "reasoning_rate",
                "unpriced_details", "image_cache_disjoint"}
    if not isinstance(entry, dict) or not required <= entry.keys() <= required | optional:
        raise ValueError("Некорректные поля тарифа.")
    if not all(isinstance(entry[key], str) for key in ("provider", "model", "source", "effective_from")):
        raise ValueError("Идентификаторы, источник и время начала должны быть строками.")
    until = entry.get("effective_until")
    details = entry.get("unpriced_details", [])
    disjoint = entry.get("image_cache_disjoint", False)
    if not isinstance(details, list) or type(disjoint) is not bool:
        raise ValueError("Некорректны правила детализации.")
    parsed_details = frozenset(UsageDetail(detail) for detail in details)
    if len(parsed_details) != len(details):
        raise ValueError("Категории детализации дублируются.")
    return UsageTariff(
        provider=entry["provider"].strip(), model=entry["model"].strip(), source=entry["source"].strip(),
        effective_from=datetime.fromisoformat(entry["effective_from"]),
        effective_until=datetime.fromisoformat(until) if until is not None else None,
        prompt_rate=entry["prompt_rate"], completion_rate=entry["completion_rate"],
        image_rate=entry.get("image_rate"), cached_rate=entry.get("cached_rate"),
        reasoning_rate=entry.get("reasoning_rate"), unpriced_details=parsed_details,
        image_cache_disjoint=disjoint,
    )
