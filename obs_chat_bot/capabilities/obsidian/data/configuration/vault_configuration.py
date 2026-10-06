"""Разбирает внешний YAML-формат правил vault в data-слое."""

from dataclasses import dataclass

import yaml

from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_configuration import (
    VaultConfiguration,
    VaultConfigurationError,
    VaultConfigurationErrorCode,
)


@dataclass(frozen=True, slots=True)
class VaultConfigurationDto:
    """Описывает значения внешнего YAML до проверки предметных правил."""

    version: object
    instructions: object


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
        payload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise VaultConfigurationError(
            VaultConfigurationErrorCode.INVALID
        ) from error
    if not isinstance(payload, dict) or set(payload) != {"version", "instructions"}:
        raise VaultConfigurationError(VaultConfigurationErrorCode.INVALID)
    return vault_configuration_from_dto(
        VaultConfigurationDto(payload["version"], payload["instructions"])
    )


def vault_configuration_from_dto(dto: VaultConfigurationDto) -> VaultConfiguration:
    """Проверяет внешний формат и преобразует его в domain-конфигурацию.

    Args:
        dto: Значения внешнего YAML после безопасного разбора.

    Returns:
        Конфигурацию с проверенными путями внутри vault.

    Raises:
        VaultConfigurationError: Версия, список или пути правил некорректны.
    """
    if dto.version != 1 or isinstance(dto.version, bool):
        raise VaultConfigurationError(VaultConfigurationErrorCode.INVALID)
    raw_paths = dto.instructions
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
