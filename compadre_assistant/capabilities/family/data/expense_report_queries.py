"""Запросы отчётов внутри проверенного снимка семейной SQLite."""

import sqlite3
from dataclasses import dataclass
from datetime import date

from ..domain.expense_reports import ExpensePeriod, ProductSelection, ReportPage, ReportRow


@dataclass(frozen=True, slots=True)
class ReportRowDto:
    """Группа результата SQLite с календарной датой в ISO."""

    id: int | None
    name: str
    amount: int
    day: str | None

    def to_domain(self) -> ReportRow:
        """Преобразует строку результата в предметное представление."""
        return ReportRow(self.id, self.name, self.amount, date.fromisoformat(self.day) if self.day else None)


_FROM = """
    FROM family_expenses e
    JOIN family_expense_items i ON i.expense_id=e.id AND i.family_id=e.family_id
    LEFT JOIN family_expense_products p ON p.id=i.product_id
    LEFT JOIN family_expense_categories c ON c.id=i.category_id
    WHERE e.family_id=:family AND e.purchase_date>=:start AND e.purchase_date<:end
"""


def query_rows(db: sqlite3.Connection, family_id: int, period: ExpensePeriod, *, group: str,
               category_id: int | None = 0, search: str | None = None, product: ProductSelection | None = None,
               offset: int = 0, limit: int | None = None) -> ReportPage:
    """Возвращает отчёт по проверенным данным, включая полные итоги страницы.

    Вызывается внутри транзакции после проверки членства. Параметры выбора
    группы задаёт адаптер, пользовательские значения передаются через bindings.
    """
    if offset < 0 or (limit is not None and limit < 1):
        raise ValueError("Недопустимая страница")
    fields = {
        "category": ("i.category_id", "coalesce(c.name, 'Без детализации')", "NULL"),
        "product": ("i.product_id", "coalesce(p.name, 'Без детализации')", "NULL"),
        "purchase": ("e.id", "coalesce(e.description, min(p.name), min(c.name), 'Покупка')", "e.purchase_date"),
    }
    id_field, title, day = fields[group]
    params = {"family": family_id, "start": period.start.isoformat(), "end": period.end.isoformat(),
              "limit": limit if limit is not None else -1, "offset": offset}
    filters = []
    if product is not None:
        filters.append("i.product_id IS :product")
        params["product"] = product.product_id
        category_id = product.category_id
    if category_id != 0:
        filters.append("i.category_id IS :category")
        params["category"] = category_id
    if search is not None:
        filters.append("instr(p.name_key, :query) > 0 AND :query <> ''")
        params["query"] = " ".join(search.split()).casefold()
    where = "".join(" AND " + condition for condition in filters)
    grouped = (f"SELECT {id_field} AS id, {title} AS name, sum(i.amount) AS amount, {day} AS day "
               + _FROM + where + f" GROUP BY {id_field}")
    order = "day DESC, id DESC" if group == "purchase" else "amount DESC, id"
    rows = db.execute(f"SELECT * FROM ({grouped}) ORDER BY {order} LIMIT :limit OFFSET :offset", params)
    values = tuple(ReportRowDto(**row).to_domain() for row in rows)
    if limit is None:
        return ReportPage(values, len(values), sum(row.amount for row in values))
    totals = db.execute(f"SELECT count(*), coalesce(sum(amount),0) FROM ({grouped})", params).fetchone()
    return ReportPage(values, totals[0], totals[1])
