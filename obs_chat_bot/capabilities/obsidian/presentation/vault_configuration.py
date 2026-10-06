"""Предоставляет статический пример правил для ответа пользователю."""

from importlib.resources import files

from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_configuration import VAULT_CONFIGURATION_PATH


def load_vault_configuration_example() -> str:
    """Возвращает готовый пример конфигурации для ответа пользователю."""
    return (
        files("obs_chat_bot.resources")
        .joinpath(VAULT_CONFIGURATION_PATH)
        .read_text(encoding="utf-8")
        .strip()
    )
