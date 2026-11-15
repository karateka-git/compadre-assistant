"""Преобразует идентификаторы возможностей на границе SQLite."""

from collections.abc import Mapping


class SQLiteCapabilityIds:
    """Сопоставляет идентификаторы приложения с форматом хранения.

    Args:
        storage_ids: Отличающиеся от формата хранения идентификаторы приложения.
    """

    def __init__(self, storage_ids: Mapping[str, str] | None = None) -> None:
        """Фиксирует прямое и обратное сопоставления при сборке приложения."""
        self._storage_ids = dict(storage_ids or {})
        self._app_ids = {stored: app for app, stored in self._storage_ids.items()}

    def encode(self, capability_id: str) -> str:
        """Возвращает значение для записи в SQLite или сравнения ключей."""
        return self._storage_ids.get(capability_id, capability_id)

    def decode(self, capability_id: str) -> str:
        """Восстанавливает идентификатор приложения из записи SQLite."""
        return self._app_ids.get(capability_id, capability_id)
