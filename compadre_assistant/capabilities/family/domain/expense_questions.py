"""Выделяет товар и месяц из короткого вопроса к отчёту."""

import re
from datetime import date, timedelta

_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь",
           "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
_QUESTION = re.compile(r"сколько\s+(?:(?:денег|мы)\s+)?(?:ушло|потрачено|потратили|потратил[аи]?)"
                       r"\s+на\s+(.+?)(?:\s+за\s+(.+))?", re.I)


def parse_expense_question(text: str, selected: date, today: date) -> tuple[str, date] | None:
    """Разбирает вопрос о сумме по товару, используя календарь семьи.

    Без периода используется открытый месяц. Для названия месяца без года
    выбирается год открытого отчёта, явный год имеет приоритет.
    """
    match = _QUESTION.fullmatch(text.strip().rstrip("?.!"))
    if not match:
        return None
    product, period = match.groups()
    if len(product) > 256:
        return None
    month = selected
    if period:
        period = " ".join(period.casefold().split())
        if period in ("этот месяц", "текущий месяц"):
            month = today.replace(day=1)
        elif period in ("прошлый месяц", "предыдущий месяц"):
            month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        else:
            parts = period.split()
            if parts[0] not in _MONTHS or len(parts) > 2:
                return None
            if len(parts) == 2 and not re.fullmatch(r"[0-9]{4}", parts[1]):
                return None
            year = int(parts[1]) if len(parts) == 2 else selected.year
            if not 1 <= year <= 9998:
                return None
            month = date(year, _MONTHS.index(parts[0]) + 1, 1)
    return product.strip(), month
