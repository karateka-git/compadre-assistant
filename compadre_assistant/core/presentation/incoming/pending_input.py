from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4

from compadre_assistant.core.presentation.incoming.events import ReplyAddress


@dataclass(frozen=True, slots=True)
class PendingInput:
    """Описывает временный маршрут следующего значения в исходном чате."""

    chat_key: ReplyAddress
    owner_id: str
    input_kind: str
    request_id: str
    created_at: datetime
    expires_at: datetime
    app_user_id: int | None = None  # Профиль на момент начала ввода; смена отменяет действие.
    required_mode: str | None = None  # Предметный ввод допустим только в указанном активном режиме.


class PendingInputStore:
    """Хранит одно ожидание на чат в памяти текущего процесса.

    Args:
        clock: Источник timezone-aware времени; результат нормализуется в UTC.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        """Создаёт пустой реестр и ограниченный набор блокировок чатов."""
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[ReplyAddress, PendingInput] = {}
        # Полосы ограничивают память и сериализуют повторные события одного чата.
        self._locks = tuple(RLock() for _ in range(64))

    def lock(self, chat_key: ReplyAddress) -> AbstractContextManager:
        """Возвращает reentrant-блокировку для атомарной обработки события чата."""
        return self._locks[hash(chat_key) % len(self._locks)]

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("Часы ожидания должны возвращать timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def begin(self, chat_key: ReplyAddress, owner_id: str, input_kind: str, *, app_user_id: int | None = None, required_mode: str | None = None) -> PendingInput:
        """Заменяет ожидание новым ID и сроком ровно десять минут.

        Args:
            chat_key: Канал и чат, в том числе до создания AppUser.
            owner_id: Публичный ID владельца ввода.
            input_kind: Тип значения, интерпретируемый только владельцем.
            app_user_id: Ожидаемый профиль для повторной проверки ввода.
            required_mode: Обязательный активный режим; None не ограничивает режим.

        Returns:
            Новая неизменяемая запись.
        """
        with self.lock(chat_key):
            now = self._now()
            pending = PendingInput(chat_key, owner_id, input_kind, uuid4().hex,
                                   now, now + timedelta(minutes=10), app_user_id, required_mode)
            self._entries[chat_key] = pending
            return pending

    def get(self, chat_key: ReplyAddress) -> PendingInput | None:
        """Читает запись, сохраняя истёкшую до первого обрабатываемого события."""
        with self.lock(chat_key):
            return self._entries.get(chat_key)

    def expired(self, pending: PendingInput) -> bool:
        """Проверяет исходный срок, не продлевая его при повторной попытке."""
        return self._now() >= pending.expires_at

    def clear(self, chat_key: ReplyAddress, request_id: str | None = None) -> bool:
        """Удаляет запись, если её ID совпадает, либо безусловно при отмене.

        Args:
            chat_key: Исходный адрес ожидания.
            request_id: Ожидаемый ID; защищает замену от старого завершения.

        Returns:
            True, если существующая запись удалена.
        """
        with self.lock(chat_key):
            current = self._entries.get(chat_key)
            if current is None or (request_id is not None and current.request_id != request_id):
                return False
            del self._entries[chat_key]
            return True
