"""Преобразования предложения Obsidian между domain и SQLite."""

from __future__ import annotations

import sqlite3

from obs_chat_bot.capabilities.obsidian.domain.pending import PendingOrigin
from datetime import UTC, datetime

from obs_chat_bot.capabilities.obsidian.data.sqlite.proposal_dtos import (
    ObsidianProposalDto,
)
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
    ObsidianProposalStatus,
)


def proposal_dto_from_domain(proposal: ObsidianProposal) -> ObsidianProposalDto:
    """Преобразует доменное предложение в форму SQLite.

    Args:
        proposal: Валидированное предложение пользователя.

    Returns:
        DTO с текстовыми статусом, действием и временными метками.
    """
    return ObsidianProposalDto(
        id=proposal.id,
        origin_channel=proposal.origin.channel,
        origin_chat_id=proposal.origin.chat_id,
        version=proposal.version,
        execution_expires_at=_format_optional_timestamp(proposal.execution_expires_at),
        app_user_id=proposal.app_user_id,
        article_id=proposal.article_id,
        analysis_id=proposal.analysis_id,
        vault_id=proposal.vault_id,
        action=proposal.action.value,
        reasoning=proposal.reasoning,
        target_path=proposal.target_path,
        proposed_markdown=proposal.proposed_markdown,
        base_commit_sha=proposal.base_commit_sha,
        base_tree_sha=proposal.base_tree_sha,
        target_blob_sha=proposal.target_blob_sha,
        status=proposal.status.value,
        applied_commit_sha=proposal.applied_commit_sha,
        created_at=_format_optional_timestamp(proposal.created_at),
        updated_at=_format_optional_timestamp(proposal.updated_at),
        completed_at=_format_optional_timestamp(proposal.completed_at),
    )


def proposal_dto_from_row(row: sqlite3.Row) -> ObsidianProposalDto:
    """Читает значения предложения из SQLite row в DTO.

    Args:
        row: Строка из таблицы `obsidian_proposals`.

    Returns:
        DTO без предметной интерпретации статуса и действия.
    """
    return ObsidianProposalDto(
        id=row["id"],
        origin_channel=row["origin_channel"],
        origin_chat_id=row["origin_chat_id"],
        version=row["version"],
        execution_expires_at=row["execution_expires_at"],
        app_user_id=row["app_user_id"],
        article_id=row["article_id"],
        analysis_id=row["analysis_id"],
        vault_id=row["vault_id"],
        action=row["action"],
        reasoning=row["reasoning"],
        target_path=row["target_path"],
        proposed_markdown=row["proposed_markdown"],
        base_commit_sha=row["base_commit_sha"],
        base_tree_sha=row["base_tree_sha"],
        target_blob_sha=row["target_blob_sha"],
        status=row["status"],
        applied_commit_sha=row["applied_commit_sha"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def proposal_from_dto(dto: ObsidianProposalDto) -> ObsidianProposal:
    """Восстанавливает предметное предложение из SQLite DTO.

    Args:
        dto: Сохранённое предложение с заполненными временными метками.

    Returns:
        Валидированная доменная модель предложения.

    Raises:
        ValueError: Если запись не содержит обязательных временных меток.
    """
    if dto.created_at is None or dto.updated_at is None:
        raise ValueError("Saved proposal must contain timestamps")
    return ObsidianProposal(
        id=dto.id,
        origin=PendingOrigin(dto.origin_channel, dto.origin_chat_id),
        version=dto.version,
        execution_expires_at=(
            _parse_timestamp(dto.execution_expires_at) if dto.execution_expires_at else None
        ),
        app_user_id=dto.app_user_id,
        article_id=dto.article_id,
        analysis_id=dto.analysis_id,
        vault_id=dto.vault_id,
        action=ObsidianProposalAction(dto.action),
        reasoning=dto.reasoning,
        target_path=dto.target_path,
        proposed_markdown=dto.proposed_markdown,
        base_commit_sha=dto.base_commit_sha,
        base_tree_sha=dto.base_tree_sha,
        target_blob_sha=dto.target_blob_sha,
        status=ObsidianProposalStatus(dto.status),
        applied_commit_sha=dto.applied_commit_sha,
        created_at=_parse_timestamp(dto.created_at),
        updated_at=_parse_timestamp(dto.updated_at),
        completed_at=(_parse_timestamp(dto.completed_at) if dto.completed_at is not None else None),
    )


def _format_optional_timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
