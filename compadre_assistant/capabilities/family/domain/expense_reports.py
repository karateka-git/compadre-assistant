"""Периоды и результаты чтения подтверждённых семейных расходов."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class ExpensePeriod:
    """Календарный интервал с включённым началом и исключённым концом."""

    start: date
    end: date

    def __post_init__(self) -> None:
        """Проверяет порядок дат."""
        if self.end <= self.start:
            raise ValueError("Конец периода должен следовать за началом")


@dataclass(frozen=True, slots=True)
class ReportRow:
    """Категория, товар или покупка и сумма её подтверждённых позиций в копейках."""

    id: int | None
    name: str
    amount: int
    purchase_date: date | None = None


@dataclass(frozen=True, slots=True)
class ExpenseSummary:
    """Общая сумма и полная разбивка для текста и диаграммы по одному снимку."""

    categories: tuple[ReportRow, ...]

    @property
    def total(self) -> int:
        """Возвращает сумму категорий с учётом знаковых корректировок."""
        return sum(row.amount for row in self.categories)


@dataclass(frozen=True, slots=True)
class ReportPage:
    """Страница отчёта, количество и сумма всех строк выборки."""

    rows: tuple[ReportRow, ...]
    count: int
    total: int


@dataclass(frozen=True, slots=True)
class ProductSelection:
    """Выбранный товар, при просмотре категории ограниченный её ID.

    ID категории 0 означает поиск по всем категориям, None обозначает разницу чека.
    ID товара None обозначает строки без детализации.
    """

    product_id: int | None
    category_id: int | None = 0
