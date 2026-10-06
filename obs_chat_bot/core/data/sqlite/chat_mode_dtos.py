"""SQLite-формат сохранённого режима чата."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatModeDto:
    """Передаёт запись маршрутизации без состояния предметного сценария."""

    app_user_id: int
    channel: str
    chat_id: str
    capability_id: str
    updated_at: str | None = None
