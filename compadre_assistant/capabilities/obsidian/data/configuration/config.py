"""Настройки окружения, принадлежащие возможности Obsidian."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

_DEFAULT_OBSIDIAN_CREATED_NOTE_TAG = "knowledge-catcher"
_OBSIDIAN_TAG_PATTERN = re.compile(r"(?=.*[^\d/])[\w-]+(?:/[\w-]+)*", re.UNICODE)


class ObsidianConfigError(ValueError):
    """Сообщает об ошибке настроек включённой возможности Obsidian."""


@dataclass(frozen=True)
class ChunkingConfig:
    """Содержит технические размеры chunks для composition root."""

    minimum_size_chars: int = 300
    target_size_chars: int = 3000
    maximum_size_chars: int = 6000

    def __post_init__(self) -> None:
        """Проверяет положительный и согласованный порядок размеров chunks."""
        if self.minimum_size_chars <= 0:
            raise ValueError("minimum_size_chars must be positive")
        if self.target_size_chars < self.minimum_size_chars:
            raise ValueError(
                "target_size_chars must not be less than minimum_size_chars"
            )
        if self.maximum_size_chars < self.target_size_chars:
            raise ValueError(
                "maximum_size_chars must not be less than target_size_chars"
            )


@dataclass(frozen=True)
class GitHubAppConfig:
    """Содержит безопасные настройки зарегистрированного GitHub App."""

    app_id: int
    client_id: str
    app_slug: str
    private_key_path: Path

    def __post_init__(self) -> None:
        """Проверяет форму идентификаторов GitHub App и путь к ключу."""
        if self.app_id <= 0:
            raise ValueError("app_id must be positive")
        if not self.client_id.strip():
            raise ValueError("client_id must not be empty")
        if any(character.isspace() for character in self.client_id):
            raise ValueError("client_id must not contain whitespace")
        if re.fullmatch(r"[a-z0-9-]+", self.app_slug) is None:
            raise ValueError("app_slug has unexpected format")
        if not str(self.private_key_path).strip():
            raise ValueError("private_key_path must not be empty")

    @property
    def installation_url(self) -> str:
        """Возвращает прямую ссылку установки GitHub App."""
        return f"https://github.com/apps/{self.app_slug}/installations/new"


@dataclass(frozen=True)
class EmbeddingConfig:
    """Содержит отдельные настройки semantic embedding provider."""

    base_url: str
    api_key: str
    document_model: str
    query_model: str
    price_per_million_tokens: Decimal | None = None
    price_currency: str | None = None
    tariff_version: str | None = None

    def __post_init__(self) -> None:
        """Проверяет обязательные поля провайдера и полноту тарифной группы."""
        if not self.base_url.strip():
            raise ValueError("base_url must not be empty")
        if not self.api_key.strip():
            raise ValueError("api_key must not be empty")
        if not self.document_model.strip():
            raise ValueError("document_model must not be empty")
        if not self.query_model.strip():
            raise ValueError("query_model must not be empty")
        pricing = (
            self.price_per_million_tokens,
            self.price_currency,
            self.tariff_version,
        )
        if all(value is None for value in pricing):
            return
        if any(value is None for value in pricing):
            raise ValueError(
                "embedding pricing must be configured as one complete group"
            )
        if (
            self.price_per_million_tokens is None
            or not self.price_per_million_tokens.is_finite()
            or self.price_per_million_tokens < 0
        ):
            raise ValueError(
                "price_per_million_tokens must be finite and not negative"
            )
        if self.price_currency is None or re.fullmatch(
            r"[A-Z]{3}", self.price_currency
        ) is None:
            raise ValueError("price_currency must contain three uppercase letters")
        if self.tariff_version is None or not self.tariff_version.strip():
            raise ValueError("tariff_version must not be empty")


@dataclass(frozen=True)
class ObsidianConfig:
    """Содержит независимый профиль провайдеров и правил Obsidian."""

    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    github_app: GitHubAppConfig | None = None
    embedding: EmbeddingConfig | None = None
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    obsidian_created_note_tag: str | None = _DEFAULT_OBSIDIAN_CREATED_NOTE_TAG

    def safe_summary(self) -> dict[str, str]:
        """Возвращает диагностику Obsidian без значений credentials."""
        return {
            "openai_base_url": self.openai_base_url,
            "openai_api_key": _presence(self.openai_api_key),
            "openai_model": self.openai_model,
            "chunk_minimum_size_chars": str(self.chunking.minimum_size_chars),
            "chunk_target_size_chars": str(self.chunking.target_size_chars),
            "chunk_maximum_size_chars": str(self.chunking.maximum_size_chars),
            "github_app": "configured" if self.github_app is not None else "missing",
            "github_private_key": (
                "set" if self.github_app is not None else "missing"
            ),
            "embedding_base_url": (
                self.embedding.base_url if self.embedding is not None else "missing"
            ),
            "embedding_api_key": (
                "set" if self.embedding is not None else "missing"
            ),
            "embedding_document_model": (
                self.embedding.document_model
                if self.embedding is not None
                else "missing"
            ),
            "embedding_query_model": (
                self.embedding.query_model
                if self.embedding is not None
                else "missing"
            ),
            "embedding_price_per_million_tokens": (
                str(self.embedding.price_per_million_tokens)
                if self.embedding is not None
                and self.embedding.price_per_million_tokens is not None
                else "missing"
            ),
            "embedding_price_currency": (
                self.embedding.price_currency
                if self.embedding is not None
                and self.embedding.price_currency is not None
                else "missing"
            ),
            "embedding_tariff_version": (
                self.embedding.tariff_version
                if self.embedding is not None
                and self.embedding.tariff_version is not None
                else "missing"
            ),
            "obsidian_created_note_tag": (
                self.obsidian_created_note_tag
                if self.obsidian_created_note_tag is not None
                else "disabled"
            ),
        }


def load_obsidian_config() -> ObsidianConfig:
    """Читает профиль Obsidian из окружения при включённой возможности.

    Returns:
        Проверенные группы настроек Obsidian.

    Raises:
        ObsidianConfigError: Настройки частичны либо некорректны.
    """
    llm = _load_obsidian_llm_settings()
    return ObsidianConfig(
        openai_base_url=llm[0], openai_api_key=llm[1], openai_model=llm[2],
        chunking=_load_chunking_config(),
        github_app=_load_github_app_config(),
        embedding=_load_embedding_config(),
        obsidian_created_note_tag=_get_obsidian_created_note_tag(),
    )


def _presence(value: str) -> str:
    return "set" if value else "missing"


def _load_obsidian_llm_settings() -> tuple[str, str, str]:
    names = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
    values = tuple(os.getenv(name, "").strip() for name in names)
    if any(values) and not all(values):
        missing = [name for name, value in zip(names, values) if not value]
        raise ObsidianConfigError(
            "Obsidian LLM configuration is incomplete; missing: " + ", ".join(missing)
        )
    return values[0].rstrip("/"), values[1], values[2]


def _get_obsidian_created_note_tag() -> str | None:
    """Читает тег заметок бота и проверяет поддерживаемый Obsidian-синтаксис.

    Returns:
        Имя тега без `#` или `None`, если маркер явно отключён значением `off`.

    Raises:
        ObsidianConfigError: Если задано пустое или некорректное имя тега.
    """
    value = os.getenv("OBSIDIAN_CREATED_NOTE_TAG")
    if value is None:
        return _DEFAULT_OBSIDIAN_CREATED_NOTE_TAG
    if value.casefold() == "off":
        return None
    if not value or _OBSIDIAN_TAG_PATTERN.fullmatch(value) is None:
        raise ObsidianConfigError(
            "Environment variable OBSIDIAN_CREATED_NOTE_TAG must be a valid "
            "Obsidian tag name without #"
        )
    return value


def _get_positive_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ObsidianConfigError(f"Environment variable {name} must be integer") from error
    if parsed <= 0:
        raise ObsidianConfigError(f"Environment variable {name} must be positive")
    return parsed


def _load_chunking_config() -> ChunkingConfig:
    try:
        return ChunkingConfig(
            minimum_size_chars=_get_positive_int(
                "CHUNK_MINIMUM_SIZE_CHARS",
                default=300,
            ),
            target_size_chars=_get_positive_int(
                "CHUNK_TARGET_SIZE_CHARS",
                default=3000,
            ),
            maximum_size_chars=_get_positive_int(
                "CHUNK_MAXIMUM_SIZE_CHARS",
                default=6000,
            ),
        )
    except ValueError as error:
        raise ObsidianConfigError(f"Invalid chunking configuration: {error}") from error


def _load_github_app_config() -> GitHubAppConfig | None:
    names = (
        "GITHUB_APP_ID",
        "GITHUB_CLIENT_ID",
        "GITHUB_APP_SLUG",
        "GITHUB_PRIVATE_KEY_PATH",
    )
    values = {name: os.getenv(name, "").strip() for name in names}
    configured = [name for name, value in values.items() if value]
    if not configured:
        return None

    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ObsidianConfigError(
            "GitHub App configuration is incomplete; missing: " + ", ".join(missing)
        )
    try:
        app_id = int(values["GITHUB_APP_ID"])
    except ValueError as error:
        raise ObsidianConfigError(
            "Environment variable GITHUB_APP_ID must be integer"
        ) from error
    if app_id <= 0:
        raise ObsidianConfigError("Environment variable GITHUB_APP_ID must be positive")

    try:
        return GitHubAppConfig(
            app_id=app_id,
            client_id=values["GITHUB_CLIENT_ID"],
            app_slug=values["GITHUB_APP_SLUG"],
            private_key_path=Path(values["GITHUB_PRIVATE_KEY_PATH"]),
        )
    except ValueError as error:
        raise ObsidianConfigError(f"Invalid GitHub App configuration: {error}") from error


def _load_embedding_config() -> EmbeddingConfig | None:
    """Загружает независимую all-or-none группу настроек embeddings."""
    provider_names = (
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_DOCUMENT_MODEL",
        "EMBEDDING_QUERY_MODEL",
    )
    pricing_names = (
        "EMBEDDING_PRICE_PER_MILLION_TOKENS",
        "EMBEDDING_PRICE_CURRENCY",
        "EMBEDDING_TARIFF_VERSION",
    )
    names = provider_names + pricing_names
    values = {name: os.getenv(name, "").strip() for name in names}
    configured = [name for name, value in values.items() if value]
    if not configured:
        return None
    provider_missing = [name for name in provider_names if not values[name]]
    if provider_missing:
        raise ObsidianConfigError(
            "Embedding configuration is incomplete; missing: "
            + ", ".join(provider_missing)
        )
    configured_pricing = [name for name in pricing_names if values[name]]
    if configured_pricing and len(configured_pricing) != len(pricing_names):
        pricing_missing = [name for name in pricing_names if not values[name]]
        raise ObsidianConfigError(
            "Embedding pricing configuration is incomplete; missing: "
            + ", ".join(pricing_missing)
        )
    price: Decimal | None = None
    if configured_pricing:
        try:
            price = Decimal(values["EMBEDDING_PRICE_PER_MILLION_TOKENS"])
        except InvalidOperation as error:
            raise ObsidianConfigError(
                "Environment variable EMBEDDING_PRICE_PER_MILLION_TOKENS "
                "must be decimal"
            ) from error
    try:
        return EmbeddingConfig(
            base_url=values["EMBEDDING_BASE_URL"].rstrip("/"),
            api_key=values["EMBEDDING_API_KEY"],
            document_model=values["EMBEDDING_DOCUMENT_MODEL"],
            query_model=values["EMBEDDING_QUERY_MODEL"],
            price_per_million_tokens=price,
            price_currency=(
                values["EMBEDDING_PRICE_CURRENCY"] if configured_pricing else None
            ),
            tariff_version=(
                values["EMBEDDING_TARIFF_VERSION"] if configured_pricing else None
            ),
        )
    except ValueError as error:
        raise ObsidianConfigError(f"Invalid embedding configuration: {error}") from error


