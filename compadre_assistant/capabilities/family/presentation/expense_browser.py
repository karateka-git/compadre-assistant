"""Навигация от семейной сводки до строк одной покупки."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..domain.expense_reports import ExpensePeriod, ProductSelection, ReportPage
from ..domain.expenses import ExpenseAccess, ExpenseRepository
from ..domain.expense_categories import ExpenseCategory
from .expense_chart import render_chart
from .expense_receipt import day_label, item_details, money, receipt_header, receipt_pages
from compadre_assistant.core.presentation.incoming.ports import ReplyAction, IncomingResponse
from compadre_assistant.core.presentation.message_content import text_content


_MONTH_TITLES = ("январь", "февраль", "март", "апрель", "май", "июнь",
                 "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


@dataclass(frozen=True, slots=True)
class ReportScreen:
    """Узел навигации с выбранными фильтрами и номером страницы."""

    kind: str
    page: int = 0
    category: int | None = 0
    product: ProductSelection | None = None
    purchase: int | None = None
    title: str = ""
    selected_category: ExpenseCategory | None = None


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

    def render(self, repo: ExpenseRepository, access: ExpenseAccess, token: str, *, can_resume: bool) -> IncomingResponse:
        """Выдаёт действия нового снимка, включая возврат к доступному черновику в сводке."""
        self.targets.clear()
        self.revision += 1
        screen = self.stack[-1]
        heading = f"{_MONTH_TITLES[self.month.month - 1]} {self.month.year}"
        actions: list[ReplyAction] = []

        def button(label: str, target: ReportScreen, row: int) -> None:
            index = len(self.targets)
            self.targets.append(target)
            cmd = f"/family_expenses browse {token} {self.revision} {index}"
            actions.append(ReplyAction(label[:40], cmd, row=row))

        def pages(last: int, row: int) -> str:
            for delta, label in ((-1, "← Ранее"), (1, "Далее →")):
                page = screen.page + delta
                if 0 <= page < last:
                    button(label, replace(screen, page=page), row)
            return f"Страница {screen.page + 1} из {last}"

        start = screen.page * self.size
        if screen.kind in ("home", "chart"):
            summary = repo.summary(access, self.period)
            lines = [f"Расходы семьи · {heading}"]
            rows = [(item.name, item.amount) for item in summary.categories[:4]]
            if len(summary.categories) > 4:
                rows.append(("Остальные", sum(x.amount for x in summary.categories[4:])))
            if rows:
                lines.extend((f"Всего {money(summary.total)}", ""))
                lines.extend(f"{name} — {money(amount)}" for name, amount in rows)
            else:
                lines.extend(("", "За этот месяц расходов пока нет."))
            if screen.kind == "chart":
                png = None
                if rows:
                    try:
                        png = render_chart(heading, money(summary.total), [
                            (name, amount, money(amount)) for name, amount in rows
                        ])
                    except OSError:
                        lines.append("Не удалось построить изображение.")
                button("По категориям", ReportScreen("groups"), 0)
                button("Назад", ReportScreen("back"), 1)
                return IncomingResponse(text_content("\n".join(lines)), suggested_actions=tuple(actions), png_image=png)
            actions.append(ReplyAction("Добавить расход", f"/family_expenses add {token}", row=0))
            if can_resume:
                button("Продолжить черновик", ReportScreen("resume"), 0)
            button("По категориям", ReportScreen("groups"), 1)
            button("История покупок", ReportScreen("history"), 1)
            button("Выбрать период", ReportScreen("period"), 2)
            button("Часовой пояс", ReportScreen("zone"), 2)
            button("Найти товар", ReportScreen("search_input"), 3)
            button("Диаграмма", ReportScreen("chart"), 3)
            button("Настроить категории", ReportScreen("catalog"), 4)
            return IncomingResponse(text_content("\n".join(lines)), suggested_actions=tuple(actions))

        if screen.kind in ("catalog", "hidden_catalog"):
            hidden = screen.kind == "hidden_catalog"
            catalog = repo.categories(access, hidden=hidden)
            last = max(1, (len(catalog) + self.size - 1) // self.size)
            if screen.page >= last:
                screen = self.stack[-1] = replace(screen, page=last - 1)
                start = screen.page * self.size
            lines = ["Скрытые категории" if hidden else "Категории семьи", ""]
            if not catalog:
                lines.append("Скрытых категорий пока нет.")
            for index, category in enumerate(catalog[start:start + self.size], start + 1):
                label = f"{index}. {category.label}"
                lines.append(label)
                button(label, ReportScreen("category_settings", selected_category=category), index - start - 1)
            row = len(actions)
            if last > 1:
                lines.extend(("", pages(last, row)))
                row += 1
            if not hidden:
                button("Создать категорию", ReportScreen("create_category"), row)
                button("Скрытые", ReportScreen("hidden_catalog"), row)
            button("Назад", ReportScreen("back"), row)
        elif screen.kind == "category_settings":
            category = screen.selected_category
            lines = [category.label]
            if category.is_fallback:
                lines.extend(("", "Бот выбирает эту категорию, если не может определить другую."))
            else:
                lines.append("Скрыта из списка для новых трат." if category.hidden else "Доступна для новых трат.")
                button("Переименовать", ReportScreen("rename_category", selected_category=category), 0)
                button("Вернуть" if category.hidden else "Скрыть", ReportScreen("toggle_category",
                       selected_category=category), 1)
            button("Назад", ReportScreen("back"), 2 if actions else 0)
        elif screen.kind == "period":
            lines = ["Период отчёта", f"Сейчас выбран {heading}."]
            for row, (text, kind) in enumerate((("Текущий месяц", "current"), ("Предыдущий месяц", "previous"),
                                                ("Другой месяц", "month_input"))):
                button(text, ReportScreen(kind), row)
            button("Назад", ReportScreen("back"), 3)
        elif screen.kind == "zone":
            lines = ["Часовой пояс семьи", repo.timezone(access), "",
                     "Этот пояс действует для всех участников семьи."]
            button("Изменить пояс", ReportScreen("zone_input"), 0)
            button("Назад", ReportScreen("back"), 1)
        elif screen.kind == "card":
            card = repo.card(access, screen.purchase)
            if card is None:
                lines = ["Покупка недоступна."]
            else:
                # Разницу до итога уже показывает шапка. Строка без товара внутри
                # известной категории остаётся частью этой категории.
                groups = {}
                for item in card.items:
                    if item.category is not None:
                        groups.setdefault(item.category, []).append(item)
                totals = {name: sum(item.amount for item in items) for name, items in groups.items()}
                header = receipt_header(card.description or "Покупка", card.purchase_date,
                                        sum(totals.values()), card.receipt_total, "Всего")
                rows = []
                for category, items in groups.items():
                    group = f"{category} — {money(totals[category])}"
                    for item in items:
                        body = [f"  {item.product or 'Без детализации'} — {money(item.amount)}"]
                        body.extend(f"  {detail}" for detail in item_details(item, show_category=False))
                        rows.append((group, "\n".join(body)))
                card_pages = receipt_pages(header, rows)
                lines = [card_pages[screen.page]]
                if len(card_pages) > 1:
                    lines.extend(("", pages(len(card_pages), 0)))
            button("Назад", ReportScreen("back"), int(bool(actions)))
        else:
            total_label = "Всего"
            if screen.kind == "groups":
                summary = repo.summary(access, self.period)
                results = ReportPage(summary.categories[start:start + self.size], len(summary.categories), summary.total)
                title = "Категории"
            elif screen.kind == "products":
                results = repo.products(access, self.period, screen.category, start, self.size)
                title = screen.title
                total_label = "В категории"
            elif screen.kind == "search":
                results = repo.search(access, self.period, screen.title, start, self.size)
                title = f"Поиск «{screen.title}»"
                total_label = "По найденным товарам"
            else:
                results = repo.history(access, self.period, start, self.size, screen.product)
                title = screen.title or "История покупок"
                if screen.product is not None:
                    total_label = "По этой позиции"
            lines = [f"{title} · {heading}"]
            if results.count:
                lines.extend((f"{total_label} {money(results.total)}", ""))
            for index, item in enumerate(results.rows):
                if screen.kind == "groups":
                    target = ReportScreen("products", category=item.id, title=item.name)
                elif screen.kind in ("products", "search"):
                    target = ReportScreen("history", product=ProductSelection(item.id, screen.category), title=item.name)
                else:
                    target = ReportScreen("card", purchase=item.id)
                prefix = f"{item.purchase_date:%d.%m} " if item.purchase_date else ""
                label = f"{prefix}{item.name} · {money(item.amount)}"
                day = f"{day_label(item.purchase_date)} · " if item.purchase_date else ""
                lines.append(f"{start + index + 1}. {day}{item.name} — {money(item.amount)}")
                button(f"{start + index + 1}. {label}", target, index)
            if not results.count:
                empty = ("Товаров с таким названием за этот месяц нет."
                         if screen.kind == "search" else "За этот месяц покупок нет.")
                lines.extend(("", empty))
            nav = len(actions)
            if results.count > self.size:
                lines.extend(("", pages((results.count + self.size - 1) // self.size, nav)))
            button("Назад", ReportScreen("back"), nav + int(len(actions) > nav))
        return IncomingResponse(text_content("\n".join(lines)), suggested_actions=tuple(actions))
