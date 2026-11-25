"""Подготовка позиций, общих для ввода чека и свободного текста."""

from dataclasses import replace
from datetime import date
from typing import Protocol

from .expenses import ExpenseItem, ExpensePurchase, InvalidExpense
from .expense_categories import ExpenseCategory

class ExpenseRecognitionError(Exception):
    """Сервис недоступен или результат требует нового ввода."""


class ExpenseRecognizer(Protocol):
    """Предлагает один черновик по сообщению или байтам чека."""

    def recognize(self, value: str | bytes, today: date, categories: tuple[ExpenseCategory, ...]) -> ExpensePurchase:
        """Возвращает покупку с датой по календарю семьи и категориями из переданного снимка.

        Raises:
            ExpenseRecognitionError: Ошибка входа, провайдера или структуры ответа.
        """
        ...


def allocate_discount(items: tuple[ExpenseItem, ...], eligible: list[int], discount: int) -> tuple[ExpenseItem, ...]:
    """Распределяет общую скидку в копейках по индексам подходящих строк.

    Доли с равным остатком округляются по порядку чека.

    Raises:
        InvalidExpense: Скидка превышает стоимость подходящих строк.
    """
    if not discount:
        return items
    total = sum(items[index].amount for index in eligible)
    if discount < 0 or discount > total:
        raise InvalidExpense("Проверь общую скидку")
    portions = {index: divmod(discount * items[index].amount, total) for index in eligible}
    extra = discount - sum(part[0] for part in portions.values())
    winners = set(sorted(eligible, key=lambda index: (-portions[index][1], index))[:extra])
    return tuple(replace(item, amount=item.amount - portions[index][0] - int(index in winners))
                 if index in portions else item for index, item in enumerate(items))
