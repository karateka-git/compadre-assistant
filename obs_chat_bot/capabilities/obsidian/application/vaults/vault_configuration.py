from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any

import yaml


VAULT_CONFIGURATION_PATH = ".knowledge-catcher.yml"
MAX_INSTRUCTION_FILES = 32


class VaultConfigurationErrorCode(StrEnum):
    """Классифицирует ошибки обязательной конфигурации правил vault."""

    MISSING = "missing"  # Служебный YAML отсутствует в корне vault.
    INVALID = "invalid"  # YAML или его структура некорректны.
    INSTRUCTION_MISSING = "instruction_missing"  # Обязательный файл не найден.


class VaultConfigurationError(RuntimeError):
    """Сообщает безопасную причину отказа загрузить правила vault."""

    def __init__(
        self,
        code: VaultConfigurationErrorCode,
        *,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class VaultConfiguration:
    """Содержит упорядоченный список обязательных instruction-файлов."""

    instruction_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.instruction_paths:
            raise ValueError("instruction_paths must not be empty")
        if len(self.instruction_paths) > MAX_INSTRUCTION_FILES:
            raise ValueError("too many instruction paths")
        if len(set(self.instruction_paths)) != len(self.instruction_paths):
            raise ValueError("instruction paths must be unique")
        for path in self.instruction_paths:
            _validate_instruction_path(path)


def parse_vault_configuration(content: str) -> VaultConfiguration:
    """Разбирает и строго проверяет пользовательский `.knowledge-catcher.yml`.

    Args:
        content: Полный UTF-8 текст конфигурационного файла.

    Returns:
        Проверенную конфигурацию с путями относительно корня vault.

    Raises:
        VaultConfigurationError: YAML некорректен или имеет неверную структуру.
    """
    try:
        payload: Any = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise VaultConfigurationError(
            VaultConfigurationErrorCode.INVALID
        ) from error
    if not isinstance(payload, dict) or set(payload) != {"version", "instructions"}:
        raise VaultConfigurationError(VaultConfigurationErrorCode.INVALID)
    if payload["version"] != 1 or isinstance(payload["version"], bool):
        raise VaultConfigurationError(VaultConfigurationErrorCode.INVALID)
    raw_paths = payload["instructions"]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise VaultConfigurationError(VaultConfigurationErrorCode.INVALID)
    if any(not isinstance(path, str) for path in raw_paths):
        raise VaultConfigurationError(VaultConfigurationErrorCode.INVALID)
    try:
        return VaultConfiguration(tuple(raw_paths))
    except ValueError as error:
        raise VaultConfigurationError(
            VaultConfigurationErrorCode.INVALID
        ) from error


def load_vault_configuration_example() -> str:
    """Возвращает готовый пример конфигурации для ответа пользователю."""
    return (
        files("obs_chat_bot.resources")
        .joinpath(VAULT_CONFIGURATION_PATH)
        .read_text(encoding="utf-8")
        .strip()
    )


def _validate_instruction_path(path: str) -> None:
    if not path or path != path.strip() or "\\" in path:
        raise ValueError("instruction path must be normalized")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or "." in parsed.parts or ".." in parsed.parts:
        raise ValueError("instruction path must stay inside vault")
    if path == VAULT_CONFIGURATION_PATH or parsed.as_posix() != path:
        raise ValueError("instruction path is not allowed")
