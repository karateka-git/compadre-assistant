"""Предметные значения подтверждённой покупки и граница хранилища семьи."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from .membership import FamilyError
from .expense_reports import ExpensePeriod, ExpenseSummary, ProductSelection, ReportPage


class ExpenseSource(StrEnum):
    """Способ ввода покупки."""

    PHOTO = "photo"  # Распознавание изображения.
    TEXT = "text"  # Распознавание свободного текста.
    MANUAL = "manual"  # Пошаговый ввод.
    UNKNOWN = "unknown"  # Неизвестное значение при чтении.


class InvalidExpense(FamilyError):
    """Данные требуют исправления до подтверждения."""


@dataclass(frozen=True, slots=True)
class ExpenseAccess:
    """Привязывает доступ профиля к конкретному вступлению в семью."""

    family_id: int
    app_user_id: int
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class ExpenseItem:
    """Оплаченная позиция в копейках или разница до итога без категории."""

    category: str | None
    product: str | None
    amount: int
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: int | None = None
    package_size: str | None = None
    amount_before_discount: int | None = None


@dataclass(frozen=True, slots=True)
class ExpensePurchase:
    """Значения на экране проверки до добавления строки разницы."""

    purchase_date: date
    source: ExpenseSource
    items: tuple[ExpenseItem, ...]
    description: str | None = None
    receipt_total: int | None = None

    def reconciled_items(self) -> tuple[ExpenseItem, ...]:
        """Проверяет значения и возвращает строки с учётом согласованного итога.

        Raises:
            InvalidExpense: Пустой состав, неизвестный источник или некорректное значение строки.
        """
        if not self.items or self.source is ExpenseSource.UNKNOWN:
            raise InvalidExpense("Проверь источник и позиции покупки")
        total = 0
        for item in self.items:
            if not item.category or not item.category.strip() or item.product is not None and not item.product.strip():
                raise InvalidExpense("Проверь название и категорию")
            if type(item.amount) is not int or item.amount < 0:
                raise InvalidExpense("Проверь сумму позиции")
            if item.amount_before_discount is not None and (
                type(item.amount_before_discount) is not int or item.amount_before_discount < item.amount
            ):
                raise InvalidExpense("Проверь сумму позиции до скидки")
            if item.unit_price is not None and (type(item.unit_price) is not int or item.unit_price < 0):
                raise InvalidExpense("Проверь цену единицы")
            if item.quantity is not None and (not item.quantity.is_finite() or item.quantity <= 0):
                raise InvalidExpense("Проверь количество")
            total += item.amount
        if self.receipt_total is None:
            return self.items
        if type(self.receipt_total) is not int or self.receipt_total < 0:
            raise InvalidExpense("Проверь итог")
        difference = self.receipt_total - total
        return self.items + (ExpenseItem(None, None, difference),) if difference else self.items


@dataclass(frozen=True, slots=True)
class ExpenseCard:
    """Сохранённая покупка с подтверждёнными строками, включая разницу."""

    id: int
    author_id: int | None
    purchase_date: date
    source: ExpenseSource
    items: tuple[ExpenseItem, ...]
    description: str | None
    receipt_total: int | None


class ExpenseRepository(Protocol):
    """Доступ к карточке, записи покупки и общему календарю семьи."""

    def summary(self, access: ExpenseAccess, period: ExpensePeriod) -> ExpenseSummary:
        """Читает все категории периода в порядке убывания суммы."""
        ...

    def products(self, access: ExpenseAccess, period: ExpensePeriod, category_id: int | None,
                 offset: int, limit: int) -> ReportPage:
        """Читает товары категории, включая группу без детализации."""
        ...

    def search(self, access: ExpenseAccess, period: ExpensePeriod, query: str,
               offset: int, limit: int) -> ReportPage:
        """Ищет товары по части нормализованного названия в заданном периоде."""
        ...

    def history(self, access: ExpenseAccess, period: ExpensePeriod, offset: int, limit: int,
                product: ProductSelection | None = None) -> ReportPage:
        """Читает покупки в обратной хронологии, при выборе товара суммирует его строки."""
        ...

    def save(self, access: ExpenseAccess, operation_id: str, revision: int, purchase: ExpensePurchase) -> int:
        """Возвращает ID покупки, подтверждая одну редакцию операции атомарно."""
        ...

    def card(self, access: ExpenseAccess, expense_id: int) -> ExpenseCard | None:
        """Возвращает карточку своей семьи или None для отсутствующей покупки."""
        ...

    def timezone(self, access: ExpenseAccess) -> str:
        """Возвращает зону IANA текущей семьи."""
        ...

    def set_timezone(self, access: ExpenseAccess, zone: str) -> None:
        """Меняет общий пояс по запросу действующего участника."""
        ...
