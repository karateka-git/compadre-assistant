"""Явные преобразования domain-режима и SQLite DTO."""

from compadre_assistant.core.data.sqlite.capability_ids import SQLiteCapabilityIds
from compadre_assistant.core.data.sqlite.chat_mode_dtos import ChatModeDto
from compadre_assistant.core.domain.chat_modes import ChatMode, ChatModeKey, ChatModeState


def chat_mode_to_dto(mode: ChatMode, capability_ids: SQLiteCapabilityIds) -> ChatModeDto:
    """Преобразует режим в параметры записи SQLite."""
    return ChatModeDto(
        mode.key.app_user_id, mode.key.channel, mode.key.chat_id, capability_ids.encode(mode.capability_id)
    )


def chat_mode_from_dto(dto: ChatModeDto, capability_ids: SQLiteCapabilityIds) -> ChatMode:
    """Преобразует SQLite DTO в предметную модель маршрутизации."""
    if dto.capability_id is None:
        raise ValueError("An inactive chat mode cannot be mapped to ChatMode")
    return ChatMode(
        ChatModeKey(dto.app_user_id, dto.channel, dto.chat_id), capability_ids.decode(dto.capability_id)
    )


def chat_mode_state_from_dto(dto: ChatModeDto, capability_ids: SQLiteCapabilityIds) -> ChatModeState:
    """Преобразует запись, включая tombstone, в состояние маршрутизации."""
    return ChatModeState(
        ChatModeKey(dto.app_user_id, dto.channel, dto.chat_id),
        capability_ids.decode(dto.capability_id) if dto.capability_id is not None else None,
        dto.revision,
    )
