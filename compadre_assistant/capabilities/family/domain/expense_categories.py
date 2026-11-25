"""Названия и состояние общего справочника категорий расходов."""

from dataclasses import dataclass

from .membership import FamilyError


FALLBACK_CATEGORY = "Прочее"  # Всегда доступная категория для неопределённого результата.
ADJUSTMENT_LABEL = "Без детализации"  # Подпись разницы чека, у которой нет категории.


class InvalidCategory(FamilyError):
    """Категория недоступна или её изменение нарушает правила справочника."""


class StaleCategory(InvalidCategory):
    """Правка адресована прежнему состоянию категории."""


def category_name(value: str) -> str:
    """Возвращает название без лишних пробелов или сообщает о недопустимом имени."""
    title = " ".join(value.split())
    if not title or len(title) > 256:
        raise InvalidCategory("Введи название от 1 до 256 символов.")
    if title.casefold() == ADJUSTMENT_LABEL.casefold():
        raise InvalidCategory("Название «Без детализации» используется для разницы чека.")
    return title


@dataclass(frozen=True, slots=True)
class ExpenseCategory:
    """Снимок категории для выбора или изменения по ожидаемой редакции."""

    id: int
    name: str
    hidden: bool
    revision: int

    @property
    def label(self) -> str:
        """Отличает прежнее пользовательское имя от служебной корректировки чека."""
        return category_label(self.name)

    @property
    def is_fallback(self) -> bool:
        """Возвращает признак постоянной запасной категории."""
        return self.name.casefold() == FALLBACK_CATEGORY.casefold()


def category_label(name: str) -> str:
    """Формирует однозначную подпись категории для участника семьи."""
    return f"{name} (категория)" if name.casefold() == ADJUSTMENT_LABEL.casefold() else name
