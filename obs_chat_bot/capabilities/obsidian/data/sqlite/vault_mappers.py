from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from obs_chat_bot.capabilities.obsidian.data.sqlite.vault_dtos import (
    GitHubInstallationDto,
    ObsidianVaultDto,
    VaultActionConfirmationDto,
    VaultInstructionDto,
    VaultNoteDto,
    VaultSyncLeaseDto,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.entities import (
    GitHubInstallation,
    ObsidianVault,
    VaultActionConfirmation,
    VaultConfirmationAction,
    VaultInstruction,
    VaultNote,
    VaultSyncLease,
)


def github_installation_dto_from_row(row: sqlite3.Row) -> GitHubInstallationDto:
    """Преобразует строку SQLite в DTO разрешённой installation."""
    return GitHubInstallationDto(
        app_user_id=row["app_user_id"],
        installation_id=row["installation_id"],
        created_at=row["created_at"],
    )


def github_installation_from_dto(dto: GitHubInstallationDto) -> GitHubInstallation:
    """Преобразует DTO разрешённой installation в доменную модель."""
    if dto.created_at is None:
        raise ValueError("Saved GitHub installation DTO must contain created_at")
    return GitHubInstallation(
        app_user_id=dto.app_user_id,
        installation_id=dto.installation_id,
        created_at=parse_utc_timestamp(dto.created_at),
    )


def github_installation_from_row(row: sqlite3.Row) -> GitHubInstallation:
    """Преобразует строку SQLite в разрешённую GitHub installation."""
    return github_installation_from_dto(github_installation_dto_from_row(row))


def obsidian_vault_dto_from_row(row: sqlite3.Row) -> ObsidianVaultDto:
    """Преобразует строку SQLite в DTO подключения vault."""
    return ObsidianVaultDto(
        id=row["id"],
        app_user_id=row["app_user_id"],
        installation_id=row["installation_id"],
        repository_id=row["repository_id"],
        owner=row["owner"],
        repository=row["repository"],
        branch=row["branch"],
        root_path=row["root_path"],
        head_commit_sha=row["head_commit_sha"],
        tree_sha=row["tree_sha"],
        head_etag=row["head_etag"],
        last_checked_at=row["last_checked_at"],
        last_synced_at=row["last_synced_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def obsidian_vault_from_dto(dto: ObsidianVaultDto) -> ObsidianVault:
    """Преобразует DTO подключения vault в доменную модель."""
    if dto.id is None or dto.created_at is None or dto.updated_at is None:
        raise ValueError("Saved Obsidian vault DTO must contain id and timestamps")
    return ObsidianVault(
        id=dto.id,
        app_user_id=dto.app_user_id,
        installation_id=dto.installation_id,
        repository_id=dto.repository_id,
        owner=dto.owner,
        repository=dto.repository,
        branch=dto.branch,
        root_path=dto.root_path,
        head_commit_sha=dto.head_commit_sha,
        tree_sha=dto.tree_sha,
        head_etag=dto.head_etag,
        last_checked_at=_parse_optional_timestamp(dto.last_checked_at),
        last_synced_at=_parse_optional_timestamp(dto.last_synced_at),
        created_at=parse_utc_timestamp(dto.created_at),
        updated_at=parse_utc_timestamp(dto.updated_at),
    )


def obsidian_vault_from_row(row: sqlite3.Row) -> ObsidianVault:
    """Преобразует строку SQLite в доменную модель vault."""
    return obsidian_vault_from_dto(obsidian_vault_dto_from_row(row))


def vault_note_dto_from_row(
    row: sqlite3.Row,
    *,
    tags: tuple[str, ...],
    wikilinks: tuple[str, ...],
) -> VaultNoteDto:
    """Преобразует строку SQLite и связанные metadata в DTO заметки."""
    return VaultNoteDto(
        id=row["id"],
        app_user_id=row["app_user_id"],
        vault_id=row["vault_id"],
        path=row["path"],
        blob_sha=row["blob_sha"],
        title=row["title"],
        markdown=row["markdown"],
        frontmatter=row["frontmatter"],
        tags=tags,
        wikilinks=wikilinks,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def vault_note_from_dto(dto: VaultNoteDto) -> VaultNote:
    """Преобразует DTO Markdown-заметки в доменную модель."""
    if dto.id is None or dto.created_at is None or dto.updated_at is None:
        raise ValueError("Saved vault note DTO must contain id and timestamps")
    return VaultNote(
        id=dto.id,
        app_user_id=dto.app_user_id,
        vault_id=dto.vault_id,
        path=dto.path,
        blob_sha=dto.blob_sha,
        title=dto.title,
        markdown=dto.markdown,
        frontmatter=dto.frontmatter,
        tags=dto.tags,
        wikilinks=dto.wikilinks,
        created_at=parse_utc_timestamp(dto.created_at),
        updated_at=parse_utc_timestamp(dto.updated_at),
    )


def vault_instruction_dto_from_row(row: sqlite3.Row) -> VaultInstructionDto:
    """Преобразует строку SQLite в DTO instruction-файла vault."""
    return VaultInstructionDto(
        id=row["id"],
        app_user_id=row["app_user_id"],
        vault_id=row["vault_id"],
        position=row["position"],
        path=row["path"],
        blob_sha=row["blob_sha"],
        content=row["content"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def vault_instruction_from_dto(dto: VaultInstructionDto) -> VaultInstruction:
    """Преобразует DTO instruction-файла в доменную модель."""
    if dto.id is None or dto.created_at is None or dto.updated_at is None:
        raise ValueError("Saved vault instruction DTO must contain id and timestamps")
    return VaultInstruction(
        id=dto.id,
        app_user_id=dto.app_user_id,
        vault_id=dto.vault_id,
        position=dto.position,
        path=dto.path,
        blob_sha=dto.blob_sha,
        content=dto.content,
        created_at=parse_utc_timestamp(dto.created_at),
        updated_at=parse_utc_timestamp(dto.updated_at),
    )


def vault_sync_lease_dto_from_row(row: sqlite3.Row) -> VaultSyncLeaseDto:
    """Преобразует строку SQLite в DTO lease синхронизации."""
    return VaultSyncLeaseDto(
        app_user_id=row["app_user_id"],
        vault_id=row["vault_id"],
        owner=row["owner"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
    )


def vault_sync_lease_from_dto(dto: VaultSyncLeaseDto) -> VaultSyncLease:
    """Преобразует DTO lease синхронизации в доменную модель."""
    return VaultSyncLease(
        app_user_id=dto.app_user_id,
        vault_id=dto.vault_id,
        owner=dto.owner,
        acquired_at=parse_utc_timestamp(dto.acquired_at),
        expires_at=parse_utc_timestamp(dto.expires_at),
    )


def vault_sync_lease_from_row(row: sqlite3.Row) -> VaultSyncLease:
    """Преобразует строку SQLite в доменный lease синхронизации."""
    return vault_sync_lease_from_dto(vault_sync_lease_dto_from_row(row))


def vault_confirmation_dto_from_row(row: sqlite3.Row) -> VaultActionConfirmationDto:
    """Преобразует строку SQLite в DTO подтверждения действия над vault."""
    return VaultActionConfirmationDto(
        app_user_id=row["app_user_id"],
        action=row["action"],
        installation_id=row["installation_id"],
        repository_id=row["repository_id"],
        owner=row["owner"],
        repository=row["repository"],
        branch=row["branch"],
        root_path=row["root_path"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def vault_confirmation_from_dto(
    dto: VaultActionConfirmationDto,
) -> VaultActionConfirmation:
    """Преобразует DTO подтверждения в доменную модель."""
    if dto.created_at is None:
        raise ValueError("Saved vault confirmation DTO must contain created_at")
    action = VaultConfirmationAction(dto.action)
    replacement = None
    if action is VaultConfirmationAction.REPLACE:
        required_values = (
            dto.installation_id,
            dto.repository_id,
            dto.owner,
            dto.repository,
            dto.branch,
            dto.root_path,
        )
        if any(value is None for value in required_values):
            raise ValueError("Replace confirmation DTO is incomplete")
        replacement = ObsidianVault(
            app_user_id=dto.app_user_id,
            installation_id=dto.installation_id,
            repository_id=dto.repository_id,
            owner=dto.owner,
            repository=dto.repository,
            branch=dto.branch,
            root_path=dto.root_path,
        )
    return VaultActionConfirmation(
        app_user_id=dto.app_user_id,
        action=action,
        replacement=replacement,
        expires_at=parse_utc_timestamp(dto.expires_at),
        created_at=parse_utc_timestamp(dto.created_at),
    )


def vault_confirmation_from_row(row: sqlite3.Row) -> VaultActionConfirmation:
    """Преобразует строку SQLite в доменное подтверждение действия."""
    return vault_confirmation_from_dto(vault_confirmation_dto_from_row(row))


def format_utc_timestamp(value: datetime) -> str:
    """Форматирует timestamp для сравнимого хранения в UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def parse_utc_timestamp(value: str) -> datetime:
    """Преобразует SQLite timestamp в timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_optional_timestamp(value: str | None) -> datetime | None:
    return parse_utc_timestamp(value) if value is not None else None
