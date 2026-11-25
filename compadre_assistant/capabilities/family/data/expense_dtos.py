"""Значения SQLite и явное преобразование в семейную покупку."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.expenses import ExpenseCard, ExpenseItem, ExpenseLineRef, ExpenseSource
from ..domain.expense_categories import ExpenseCategory


@dataclass(frozen=True)
class ExpenseCategoryDto:
    """Снимок категории и её редакции из SQLite."""

    id: int
    name: str
    hidden: int
    revision: int

    def to_domain(self) -> ExpenseCategory:
        """Преобразует состояние видимости в предметное значение."""
        return ExpenseCategory(self.id, self.name, bool(self.hidden), self.revision)


@dataclass(frozen=True)
class ExpenseItemDto:
    """Строка выборки позиции с названиями из справочников семьи."""

    category: str | None
    product: str | None
    amount: int
    quantity: str | None
    unit: str | None
    unit_price: int | None
    package_size: str | None
    amount_before_discount: int | None
    category_id: int | None = None
    expense_id: int | None = None
    position: int | None = None
    category_revision: int | None = None

    def to_domain(self) -> ExpenseItem:
        """Восстанавливает точное десятичное количество из SQLite TEXT."""
        reference = (ExpenseLineRef(self.expense_id, self.position, self.category_revision)
                     if self.category_id is not None and self.expense_id is not None else None)
        return ExpenseItem(self.category, self.product, self.amount,
                           Decimal(self.quantity) if self.quantity is not None else None,
                           self.unit, self.unit_price, self.package_size, self.amount_before_discount,
                           self.category_id, reference)

    @classmethod
    def from_domain(cls, item: ExpenseItem) -> "ExpenseItemDto":
        """Готовит точное количество и согласованные названия к записи."""
        return cls(item.category, item.product, item.amount,
                   str(item.quantity) if item.quantity is not None else None,
                   item.unit, item.unit_price, item.package_size, item.amount_before_discount, item.category_id)


@dataclass(frozen=True)
class ExpenseCardDto:
    """Проекция заголовка покупки из хранилища."""

    id: int
    author_id: int | None
    purchase_date: str
    source: str
    description: str | None
    receipt_total: int | None

    def to_domain(self, items: tuple[ExpenseItem, ...]) -> ExpenseCard:
        """Преобразует календарный день и источник, включая незнакомый код."""
        try:
            source = ExpenseSource(self.source)
        except ValueError:
            source = ExpenseSource.UNKNOWN
        return ExpenseCard(self.id, self.author_id, date.fromisoformat(self.purchase_date),
                           source, items, self.description, self.receipt_total)
