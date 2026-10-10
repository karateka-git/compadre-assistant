"""Контекст владения vault lease для атомарной проверки data-записей."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta


class VaultLeaseLostError(RuntimeError):
    """Операция потеряла владение vault и не может публиковать состояние."""


@dataclass(frozen=True, slots=True)
class VaultLeaseScope:
    """Хранит идентификатор операции и часы для проверки lease при записи."""

    app_user_id: int
    vault_id: int
    owner: str
    clock: Callable[[], datetime]
    duration: timedelta


_ACTIVE_SCOPE: ContextVar[VaultLeaseScope | None] = ContextVar(
    "obsidian_vault_lease_scope", default=None
)


@contextmanager
def active_vault_lease(scope: VaultLeaseScope) -> Iterator[None]:
    """Привязывает lease только к текущей операции, включая вложенные вызовы."""
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield
    finally:
        _ACTIVE_SCOPE.reset(token)


def current_vault_lease() -> VaultLeaseScope | None:
    """Возвращает lease текущей операции для проверки в data-транзакции."""
    return _ACTIVE_SCOPE.get()
