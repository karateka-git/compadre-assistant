"""Навигация от семейной сводки до строк одной покупки."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..domain.expense_reports import ExpensePeriod, ProductSelection, ReportPage
from ..domain.expenses import ExpenseAccess, ExpenseRepository
from compadre_assistant.core.presentation.incoming.ports import ReplyAction, IncomingResponse
from compadre_assistant.core.presentation.message_content import text_content


def money(amount: int) -> str:
    """Форматирует целые копейки, включая отрицательную разницу."""
    whole, cents = divmod(abs(amount), 100)
    sign = "−" if amount < 0 else ""
    return f"{sign}{whole},{cents:02} ₽" if cents else f"{sign}{whole} ₽"


@dataclass(frozen=True, slots=True)
class ReportScreen:
    """Узел навигации с выбранными фильтрами и номером страницы."""

    kind: str
    page: int = 0
    category: int | None = 0
    product: ProductSelection | None = None
    purchase: int | None = None
    title: str = ""


@dataclass(slots=True)
class ExpenseBrowser:
    """Хранит выбранный месяц и путь открытых экранов исходного чата."""

    month: date
    size: int
    stack: list[ReportScreen] = field(default_factory=lambda: [ReportScreen("home")])
    targets: list[ReportScreen] = field(default_factory=list)
    revision: int = 0

    @classmethod
    def open(cls, zone: str, channel: str) -> "ExpenseBrowser":
        """Открывает текущий месяц семьи с учётом бюджета кнопок канала."""
        today = datetime.now(ZoneInfo(zone)).date()
        return cls(today.replace(day=1), 4 if channel == "vk" else 5)

    @property
    def period(self) -> ExpensePeriod:
        """Возвращает выбранный календарный месяц."""
        last = self.month.replace(day=28) + timedelta(days=4)
        return ExpensePeriod(self.month, last.replace(day=1))

    def select_month(self, month: date) -> None:
        """Возвращает в сводку за выбранный месяц."""
        self.month = month.replace(day=1)
        self.stack = [ReportScreen("home")]

    def navigate(self, target: ReportScreen) -> None:
        """Меняет страницу внутри экрана или переходит между уровнями."""
        if target.kind == "back":
            if len(self.stack) > 1:
                self.stack.pop()
        elif target.kind == self.stack[-1].kind:
            self.stack[-1] = target
        else:
            self.stack.append(target)

    def find(self, repo: ExpenseRepository, access: ExpenseAccess, query: str) -> None:
        """Открывает покупки единственного совпадения либо список результатов."""
        found = repo.search(access, self.period, query, 0, 1)
        if found.count == 1:
            item = found.rows[0]
            self.navigate(ReportScreen("history", product=ProductSelection(item.id), title=item.name))
        else:
            self.navigate(ReportScreen("search", title=query))

    def render(self, repo: ExpenseRepository, access: ExpenseAccess, token: str) -> IncomingResponse:
        """Выдаёт адресные действия только для нового снимка экрана."""
        self.targets.clear()
        self.revision += 1
        screen = self.stack[-1]
        heading = f"{self.month:%m.%Y}"
        actions: list[ReplyAction] = []

        def button(label: str, target: ReportScreen, row: int) -> None:
            index = len(self.targets)
            self.targets.append(target)
            cmd = f"/family_expenses browse {token} {self.revision} {index}"
            actions.append(ReplyAction(label[:40], cmd, row=row))

        def pages(count: int, row: int) -> str:
            last = max(1, (count + self.size - 1) // self.size)
            for delta, label in ((-1, "← Ранее"), (1, "Далее →")):
                page = screen.page + delta
                if 0 <= page < last:
                    button(label, replace(screen, page=page), row)
            return f"Страница {screen.page + 1} из {last}"

        start = screen.page * self.size
        if screen.kind == "home":
            summary = repo.summary(access, self.period)
            lines = [f"Расходы семьи · {heading}", f"Всего · {money(summary.total)}"]
            lines.extend(f"{item.name} · {money(item.amount)}" for item in summary.categories[:4])
            if len(summary.categories) > 4:
                lines.append(f"Остальные · {money(sum(x.amount for x in summary.categories[4:]))}")
            if not summary.categories:
                lines.append("За этот месяц расходов пока нет.")
            actions.append(ReplyAction("Добавить расход", f"/family_expenses add {token}", row=0))
            button("По категориям", ReportScreen("groups"), 1)
            button("История покупок", ReportScreen("history"), 1)
            button("Выбрать период", ReportScreen("period"), 2)
            button("Часовой пояс", ReportScreen("zone"), 2)
            button("Найти товар", ReportScreen("search_input"), 3)
            return IncomingResponse(text_content("\n".join(lines)), suggested_actions=tuple(actions))

        if screen.kind == "period":
            lines = [f"Период отчёта · {heading}"]
            for row, (text, kind) in enumerate((("Текущий месяц", "current"), ("Предыдущий месяц", "previous"),
                                                ("Другой месяц", "month_input"))):
                button(text, ReportScreen(kind), row)
            button("Назад", ReportScreen("back"), 3)
        elif screen.kind == "zone":
            lines = [f"Часовой пояс семьи · {repo.timezone(access)}",
                     "Выбранный пояс действует для всех участников."]
            button("Изменить пояс", ReportScreen("zone_input"), 0)
            button("Назад", ReportScreen("back"), 1)
        elif screen.kind == "card":
            card = repo.card(access, screen.purchase)
            if card is None:
                lines = ["Покупка недоступна."]
            else:
                lines = [f"{card.description or 'Покупка'} · {card.purchase_date:%d.%m.%Y}",
                         f"Итого · {money(sum(i.amount for i in card.items))}"]
                for i in card.items[start:start + self.size]:
                    qty = f"{i.quantity} {i.unit or 'шт.'} · " if i.quantity is not None else ""
                    price = f"{money(i.unit_price)} за ед. · " if i.unit_price is not None else ""
                    lines.append(f"{i.product or 'Без детализации'} · {qty}{price}{money(i.amount)} · "
                                 f"{i.category or 'Без детализации'}")
                lines.append(pages(len(card.items), 0))
            button("Назад", ReportScreen("back"), int(bool(actions)))
        else:
            if screen.kind == "groups":
                summary = repo.summary(access, self.period)
                results = ReportPage(summary.categories[start:start + self.size], len(summary.categories), summary.total)
                title = "Категории"
            elif screen.kind == "products":
                results = repo.products(access, self.period, screen.category, start, self.size)
                title = screen.title
            elif screen.kind == "search":
                results = repo.search(access, self.period, screen.title, start, self.size)
                title = f"Поиск · {screen.title}"
            else:
                results = repo.history(access, self.period, start, self.size, screen.product)
                title = screen.title or "История покупок"
            lines = [f"{title} · {heading}", f"Всего · {money(results.total)}"]
            for index, item in enumerate(results.rows):
                if screen.kind == "groups":
                    target = ReportScreen("products", category=item.id, title=item.name)
                elif screen.kind in ("products", "search"):
                    target = ReportScreen("history", product=ProductSelection(item.id, screen.category), title=item.name)
                else:
                    target = ReportScreen("card", purchase=item.id)
                prefix = f"{item.purchase_date:%d.%m} " if item.purchase_date else ""
                label = f"{prefix}{item.name} · {money(item.amount)}"
                lines.append(f"{start + index + 1}. {label}")
                button(f"{start + index + 1}. {label}", target, index)
            if not results.count:
                lines.append("За выбранный период записей нет.")
            nav = len(actions)
            lines.append(pages(results.count, nav))
            button("Назад", ReportScreen("back"), nav + int(len(actions) > nav))
        return IncomingResponse(text_content("\n".join(lines)), suggested_actions=tuple(actions))
