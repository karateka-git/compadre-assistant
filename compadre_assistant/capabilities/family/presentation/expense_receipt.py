"""Форматирование сумм, заголовка и пояснений к строке семейного чека."""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from ..domain.expenses import ExpenseItem


_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря")


def day_label(day: date) -> str:
    """Возвращает день и месяц покупки словами для чека и истории."""
    return f"{day.day} {_MONTHS[day.month - 1]}"


def money(amount: int) -> str:
    """Форматирует целые копейки, включая отрицательную разницу."""
    whole, cents = divmod(abs(amount), 100)
    sign = "−" if amount < 0 else ""
    return f"{sign}{whole},{cents:02} ₽" if cents else f"{sign}{whole} ₽"


def receipt_header(title: str, day: date, subtotal: int, total: int | None, label: str) -> list[str]:
    """Показывает итог один раз и раскрывает сверку при расхождении."""
    lines = [f"{title} · {day_label(day)} {day.year}"]
    if total is None or subtotal == total:
        lines.append(f"{label} {money(subtotal)}")
    else:
        lines.extend((f"По позициям — {money(subtotal)}", f"Без детализации — {money(total - subtotal)}",
                      f"Итог чека — {money(total)}"))
    return lines


def item_details(item: ExpenseItem, *, show_category: bool) -> list[str]:
    """Поясняет фасовку, количество и известную скидку, сохраняя оплаченную сумму строки."""
    details = [item.category] if show_category and item.category else []
    if item.package_size:
        details.append(f"упаковка {item.package_size}")
    quantity = item.quantity
    unit = item.unit or "шт."
    if quantity is not None and (quantity != 1 or unit.casefold().rstrip(".") not in ("шт", "уп", "упак")):
        display_quantity, display_unit = quantity, unit
        if unit.casefold() == "кг" and quantity < 1:
            display_quantity, display_unit = quantity * 1000, "г"
        text = f"{format(display_quantity.normalize(), 'f').replace('.', ',')} {display_unit}"
        if item.unit_price is not None:
            extended = (quantity * item.unit_price).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            if extended == item.amount:
                text += f" × {money(item.unit_price)}/{unit}"
        details.append(text)
    lines = [" · ".join(details)] if details else []
    before = item.amount_before_discount
    if before is not None and before > item.amount:
        lines.append(f"До скидки {money(before)} · скидка {money(before - item.amount)}")
    return lines
