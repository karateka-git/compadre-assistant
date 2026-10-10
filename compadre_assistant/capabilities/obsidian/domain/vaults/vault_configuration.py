from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


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


def _validate_instruction_path(path: str) -> None:
    if not path or path != path.strip() or "\\" in path:
        raise ValueError("instruction path must be normalized")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or "." in parsed.parts or ".." in parsed.parts:
        raise ValueError("instruction path must stay inside vault")
    if path == VAULT_CONFIGURATION_PATH or parsed.as_posix() != path:
        raise ValueError("instruction path is not allowed")
