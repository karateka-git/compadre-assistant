"""Явные преобразования domain-режима и SQLite DTO."""

from obs_chat_bot.core.data.sqlite.chat_mode_dtos import ChatModeDto
from obs_chat_bot.core.domain.chat_modes import ChatMode, ChatModeKey


def chat_mode_to_dto(mode: ChatMode) -> ChatModeDto:
    """Преобразует режим в параметры записи SQLite."""
    return ChatModeDto(
        mode.key.app_user_id, mode.key.channel, mode.key.chat_id, mode.capability_id
    )


def chat_mode_from_dto(dto: ChatModeDto) -> ChatMode:
    """Преобразует SQLite DTO в предметную модель маршрутизации."""
    return ChatMode(
        ChatModeKey(dto.app_user_id, dto.channel, dto.chat_id), dto.capability_id
    )
