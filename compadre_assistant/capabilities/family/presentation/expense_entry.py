"""Экраны проверки и исправления семейной покупки в исходном чате."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import RLock
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..domain.expenses import ExpenseAccess, ExpenseCategoryUnavailable, ExpenseItem, ExpensePurchase, ExpenseRepository, ExpenseSource, InvalidExpense
from ..domain.membership import FamilyAccessEnded
from ..domain.expense_categories import InvalidCategory, StaleCategory, category_name
from ..domain.recognition import ExpenseRecognitionError, ExpenseRecognizer
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, ScreenDestination
from compadre_assistant.core.presentation.incoming.events import ImageLoadError, IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, HandlerContext, IncomingResponse, PendingInputOutcome, PendingInputResult, ReplyAction,
    ResponseView,
)
from compadre_assistant.core.presentation.message_content import compose_content, text_content
from .expense_browser import ExpenseBrowser, ReportScreen
from .expense_receipt import item_details, money as _money, receipt_header, receipt_pages


EXPENSES = "/family_expenses"  # Один вход и адресные действия раздела.
_TTL = 600  # Десять минут после показа редакции.
_PAGE = 4  # Страница выбора с отдельной кнопкой для каждого варианта.
_MONTH_PROMPT = "Введи месяц и год цифрами, например 11.2026."


def _reply(text: str, *actions: ReplyAction, close_screen: bool = False) -> IncomingResponse:
    return IncomingResponse(text_content(text), suggested_actions=actions, close_managed_views=close_screen)


def expenses_action(family_id: int) -> ReplyAction:
    """Открывает расходы указанной семьи из меню или после завершения ввода."""
    return ReplyAction("Расходы", f"{EXPENSES} open {family_id}", scope=ActionScope.SECTION,
                       purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("family-expenses"))


def _action(label: str, arguments: str) -> ReplyAction:
    return ReplyAction(label, (EXPENSES + " " + arguments).strip())


def _cents(value: str) -> int:
    number = Decimal(value.strip().replace(",", "."))
    if not number.is_finite() or number < 0 or number > 10**12 or number.as_tuple().exponent < -2:
        raise ValueError
    return int(number * 100)


@dataclass(frozen=True, slots=True)
class _Draft:
    access: ExpenseAccess
    mode: int | None
    operation_id: str
    revision: int
    purchase: ExpensePurchase
    deadline: float


@dataclass(slots=True)
class _CategoryPicker:
    item: ExpenseItem
    position: int
    page: int = 0
    revision: str = ""
    allowed: tuple[str, ...] = ()


@dataclass(slots=True)
class _Session:
    access: ExpenseAccess
    mode: int | None
    token: str
    address: ReplyAddress
    review_page: int = 0
    review_page_count: int = 1
    draft: _Draft | None = None
    deadline: float = 0
    request: str | None = None
    manual_date: date | None = None
    manual_name: str | None = None
    browser: ExpenseBrowser | None = None
    picker: _CategoryPicker | None = None

    @property
    def purchase(self) -> ExpensePurchase | None:
        """Возвращает покупку открытой редакции на экране проверки."""
        return self.draft.purchase if self.draft else None

    def screen(self, response: IncomingResponse, *, update: bool = False) -> IncomingResponse:
        """Привязывает текст и кнопки к сообщению этой сессии расходов."""
        return replace(response, view=ResponseView(
            f"family-expenses:{self.token}", update=update, cleanup_previous=True,
            action_scope=ActionScope.SCREEN, allow_multiple_messages=True,
            unavailable=text_content("Не удалось обновить экран. Открой «Расходы» заново."),
        ))


class ExpenseEntry:
    """Ведёт подготовку покупки с проверкой текущего членства на каждом шаге."""

    def __init__(self, repository: Callable[[], AbstractContextManager[ExpenseRepository]],
                 recognizer: ExpenseRecognizer | None, clock: Callable[[], float] = monotonic) -> None:
        """Принимает фабрику хранилища, распознаватель и часы срока ввода."""
        self._repository = repository
        self._recognizer = recognizer
        self._clock = clock
        self._lock = RLock()
        self._sessions: dict[ReplyAddress, _Session] = {}
        self._drafts: dict[ReplyAddress, _Draft] = {}

    def _fresh(self, address: ReplyAddress, access: ExpenseAccess, mode: int | None) -> _Session:
        for key, old in list(self._sessions.items()):
            if old.deadline <= self._clock():
                del self._sessions[key]
        for key, draft in list(self._drafts.items()):
            if draft.deadline <= self._clock():
                del self._drafts[key]
        session = _Session(access, mode, uuid4().hex, address, deadline=self._clock() + _TTL)
        self._sessions[address] = session
        return session

    def leave(self, address: ReplyAddress) -> None:
        """Завершает навигацию расходов при переходе в другой раздел семьи."""
        with self._lock:
            self._sessions.pop(address, None)

    def _available_draft(self, session: _Session) -> _Draft | None:
        draft = self._drafts.get(session.address)
        if draft and (draft.access != session.access or draft.mode != session.mode or self._clock() >= draft.deadline):
            del self._drafts[session.address]
            return None
        return draft

    def _report(self, session, repo, notice=""):
        response = session.browser.render(repo, session.access, session.token,
                                      can_resume=self._available_draft(session) is not None)
        if notice:
            return replace(response, content=compose_content(text_content(notice), response.content))
        return response

    def draft_menu(self, address: ReplyAddress, mode: int, access: ExpenseAccess) -> tuple[ReplyAction, ...]:
        """Возвращает меню чека для текущего членства и действующей редакции в VK."""
        with self._lock:
            session = self._sessions.get(address)
            if (address.channel != "vk" or session is None or session.purchase is None or session.request or session.picker
                    or session.access != access or session.mode != mode or self._clock() >= session.deadline):
                return ()
            return self._review_actions(session)

    def _current(self, event: IncomingEvent, context: HandlerContext, access: ExpenseAccess) -> _Session | None:
        session = self._sessions.get(event.reply_address)
        if (session and session.mode == context.mode_revision and session.access == access
                and self._clock() < session.deadline):
            return session
        return None

    @staticmethod
    def _expired(access: ExpenseAccess) -> IncomingResponse:
        return _reply("Этот экран уже закрыт. Открой «Расходы».", expenses_action(access.family_id))

    def _ask(self, context, event, session, kind: str, prompt: str, *, images=False, return_command=EXPENSES):
        if context.pending_inputs is None:
            return self._expired(session.access)
        menu_actions = (ReplyAction("Ввести пошагово", f"{EXPENSES} manual {session.token}",
                                    scope=ActionScope.SECTION),) if images else ()
        waiting = context.pending_inputs.begin(event.reply_address, "family", "expense:" + kind,
            app_user_id=context.app_user.id, required_mode="family", value_label=prompt, retry_command=EXPENSES,
            accepts_images=images, cancel_only_menu=True, cancel_return_command=return_command, menu_actions=menu_actions)
        session.request = waiting.request_id
        return _reply(prompt, *menu_actions, CANCEL_SECTION_ACTION, close_screen=True)

    def handle(self, context: HandlerContext, event: IncomingEvent, access: ExpenseAccess,
               arguments: str) -> IncomingResponse:
        """Открывает раздел или выполняет действие показанной редакции."""
        with self._lock:
            args = arguments.split()
            if not args or args == ["open", str(access.family_id)]:
                session = self._fresh(event.reply_address, access, context.mode_revision)
                with self._repository() as repo:
                    session.browser = ExpenseBrowser.open(repo.timezone(access), event.reply_address.channel)
                    return session.screen(self._report(session, repo))
            session = self._current(event, context, access)
            if session is None or len(args) < 2 or args[1] != session.token:
                return self._expired(access)
            if session.picker is not None:
                if (args[0] != "pick" or len(args) != 4 or args[2] != session.picker.revision
                        or args[3] not in session.picker.allowed):
                    return self._expired(access)
                return self._picker_action(context, event, session, args[3])
            if args[0] == "catalog_return" and session.browser and args[2:] == [str(session.browser.revision)]:
                session.request = None
                with self._repository() as repo:
                    return session.screen(self._report(session, repo))
            if args[0] == "browse" and session.browser and session.purchase is None:
                browser = session.browser
                if (len(args) != 4 or args[2] != str(browser.revision) or not args[3].isascii()
                        or not args[3].isdigit() or len(args[3]) > 2 or int(args[3]) >= len(browser.targets)):
                    return self._expired(access)
                target = browser.targets[int(args[3])]
                if target.kind == "resume":
                    draft = self._available_draft(session)
                    if draft is None:
                        return self._expired(access)
                    session.draft = draft
                    session.deadline = draft.deadline
                    session.browser = None
                    return session.screen(self._review(session), update=session.address.channel != "vk")
                session.deadline = self._clock() + _TTL
                if target.kind in ("create_category", "rename_category"):
                    browser.targets.clear()
                    prompt = ("Введи название новой категории." if target.kind == "create_category"
                              else f"{target.selected_category.label}\n\nВведи новое название. Оно появится и в прошлых чеках.")
                    return self._ask(context, event, session, target.kind, prompt,
                                     return_command=f"{EXPENSES} catalog_return {session.token} {browser.revision}")
                if target.kind == "search_input":
                    return self._ask(context, event, session, target.kind,
                                     "Введи название товара или его часть, например «молоко».")
                if target.kind in ("month_input", "zone_input"):
                    prompt = (_MONTH_PROMPT if target.kind == "month_input"
                              else "Введи часовой пояс IANA, например Asia/Omsk.")
                    return self._ask(context, event, session, target.kind, prompt)
                with self._repository() as repo:
                    if target.kind == "apply_saved_category":
                        try:
                            repo.set_item_category(access, target.line, target.selected_category.id)
                            notice = "Категория позиции сохранена."
                        except StaleCategory as error:
                            notice = str(error)
                        except InvalidCategory as error:
                            return session.screen(self._report(session, repo, str(error)), update=True)
                        browser.navigate(ReportScreen("back"))
                        browser.navigate(ReportScreen("back"))
                        return session.screen(self._report(session, repo, notice), update=True)
                    if target.kind == "toggle_category":
                        category = target.selected_category
                        try:
                            changed = repo.set_category_hidden(access, category, not category.hidden)
                            notice = (f"«{changed.label}» скрыта. Прежние расходы остались в отчётах." if changed.hidden
                                      else f"«{changed.label}» снова доступна для новых трат.")
                        except InvalidCategory as error:
                            notice = str(error)
                        browser.navigate(ReportScreen("back"))
                        return session.screen(self._report(session, repo, notice), update=True)
                    if target.kind in ("current", "previous"):
                        today = datetime.now(ZoneInfo(repo.timezone(access))).date()
                        month = today if target.kind == "current" else today.replace(day=1) - timedelta(days=1)
                        browser.select_month(month)
                    else:
                        browser.navigate(target)
                    return session.screen(self._report(session, repo), update=target.kind != "chart")
            if args[0] == "manual" and session.purchase is None:
                session = self._fresh(event.reply_address, access, context.mode_revision)
                with self._repository() as repo:
                    session.manual_date = datetime.now(ZoneInfo(repo.timezone(access))).date()
                return self._ask(context, event, session, "manual_name",
                                 "Введи название покупки или услуги, например «Такси домой».")
            if args[0] == "add" and session.browser:
                session = self._fresh(event.reply_address, access, context.mode_revision)
                return self._ask(context, event, session, "recognize",
                                 "Пришли одно фото чека или опиши покупку сообщением в течение 10 минут.", images=True)
            if session.draft is None or len(args) < 3 or args[2] != str(session.draft.revision):
                return self._expired(access)
            try:
                return self._draft_action(context, event, session, args[0], args[3:])
            except (InvalidExpense, ValueError, IndexError):
                return self._expired(access)

    def _draft_action(self, context, event, session, verb, args):
        purchase = session.purchase
        if verb == "cancel":
            self._drafts.pop(event.reply_address)
            self._sessions.pop(event.reply_address)
            return _reply("Ввод расхода отменён.", expenses_action(session.access.family_id), close_screen=True)
        if verb == "save":
            try:
                with self._repository() as repo:
                    repo.save(session.access, session.draft.operation_id, session.draft.revision, purchase)
            except ExpenseCategoryUnavailable as error:
                position = error.position - 1
                session.picker = _CategoryPicker(purchase.items[position], position)
                return self._categories(session, notice=str(error), update=True)
            self._drafts.pop(event.reply_address)
            self._sessions.pop(event.reply_address)
            total = purchase.receipt_total if purchase.receipt_total is not None else sum(i.amount for i in purchase.items)
            name = purchase.description or (purchase.items[0].product if len(purchase.items) == 1 else None)
            summary = f"{name} — {_money(total)}" if name else f"Всего {_money(total)}"
            return _reply(f"Расход сохранён.\n{summary}", expenses_action(session.access.family_id), close_screen=True)
        if verb == "view":
            return session.screen(self._review(session, int(args[0]) if args else 0), update=True)
        if verb == "edit":
            options = [self._button(session, label, cmd) for label, cmd in (
                ("Позиции", "positions"), ("Дата", "date"), ("Магазин", "shop"),
                ("Итог чека", "total"), ("Назад", "view"))]
            header = receipt_header(purchase.description or "Расход", purchase.purchase_date,
                                    sum(i.amount for i in purchase.items), purchase.receipt_total, "К оплате")
            return session.screen(_reply("\n".join((*header, "", "Что исправить?")), *options), update=True)
        if verb == "positions":
            return session.screen(self._positions(session, int(args[0]) if args else 0), update=True)
        if verb == "row":
            index = int(args[0])
            if not 0 <= index < len(purchase.items):
                raise ValueError
            item = purchase.items[index]
            lines = [f"{index + 1}. {item.product or 'Без детализации'} — {_money(item.amount)}",
                     *item_details(item, show_category=True)]
            details = _reply("\n".join(lines),
                *(self._button(session, label, cmd, str(index)) for label, cmd in (
                    ("Название", "name"), ("Сумма", "amount"), ("Категория", "category"),
                    ("Удалить позицию", "delete"))), self._button(session, "Назад", "positions", str(index // _PAGE)))
            return session.screen(details, update=True)
        if verb == "delete":
            index = int(args[0])
            if not 0 <= index < len(purchase.items):
                raise ValueError
            if len(purchase.items) == 1:
                details = _reply("В расходе должна остаться хотя бы одна позиция.", self._button(session, "Назад", "view"))
                return session.screen(details, update=True)
            return self._update(session, replace(purchase, items=purchase.items[:index] + purchase.items[index + 1:]),
                                update_screen=True)
        if verb == "category":
            index = int(args[0])
            if not 0 <= index < len(purchase.items):
                raise ValueError
            session.picker = _CategoryPicker(purchase.items[index], index)
            return self._categories(session, update=True)
        prompts = {"name": "Введи новое название позиции (до 256 символов).",
                   "amount": "Введи сумму за всю позицию в рублях, например 71,99.",
                   "date": "Введи дату покупки цифрами, например 23.11.2026.",
                   "shop": "Введи магазин или описание покупки (до 256 символов).",
                   "total": "Введи итог к оплате в рублях, например 556,76.",
                   "insert": "Введи название и сумму в рублях через вертикальную черту.\nХлеб | 59"}
        if verb in prompts:
            index = ""
            if verb in ("name", "amount"):
                index = args[0]
                if not 0 <= int(index) < len(purchase.items):
                    raise ValueError
            return self._ask(context, event, session, f"{verb}:{index}", prompts[verb])
        return self._expired(session.access)

    @staticmethod
    def _button(session, label, verb, argument=""):
        return _action(label, f"{verb} {session.token} {session.draft.revision} {argument}".strip())

    def _page_buttons(self, session, page, count, verb):
        if not 0 <= page < count:
            raise ValueError
        actions = []
        for target, label in ((page - 1, "← Ранее"), (page + 1, "Далее →")):
            if 0 <= target < count:
                actions.append(replace(self._button(session, label, verb, str(target)), row=0))
        return actions

    def _positions(self, session, page):
        pages = (len(session.purchase.items) + _PAGE - 1) // _PAGE
        nav = self._page_buttons(session, page, pages, "positions")
        items = session.purchase.items[page * _PAGE:(page + 1) * _PAGE]
        actions = [self._button(session, f"{i + 1}. {(item.product or 'Без детализации')[:24]}", "row", str(i))
                   for i, item in enumerate(items, page * _PAGE)]
        # В VK зарезервированы ряды для раздела навигации и возврата.
        actions = [replace(action, row=i) for i, action in enumerate(actions)]
        actions.extend(replace(action, row=len(items)) for action in nav)
        actions.append(replace(self._button(session, "Добавить позицию", "insert"), row=len(items)+bool(nav)))
        actions.append(replace(self._button(session, "Назад", "view"), row=len(items)+bool(nav)))
        lines = ["Выбери позицию для исправления.", ""]
        lines.extend(f"{i}. {item.product or 'Без детализации'} — {_money(item.amount)}"
                     for i, item in enumerate(items, page * _PAGE + 1))
        if pages > 1:
            lines.extend(("", f"Страница {page + 1} из {pages}"))
        return _reply("\n".join(lines), *actions)

    def _review(self, session, page=0):
        purchase = session.purchase
        header = receipt_header(purchase.description or "Расход", purchase.purchase_date,
                               sum(item.amount for item in purchase.items), purchase.receipt_total, "К оплате")
        rows = []
        for i, item in enumerate(purchase.items, 1):
            title = f"{i}. {item.product or 'Без детализации'} — {_money(item.amount)}"
            rows.append((None, "\n".join(("", title, *item_details(item, show_category=True)))))
        pages = receipt_pages(header, rows)
        nav = self._page_buttons(session, page, len(pages), "view")
        session.review_page = page
        session.review_page_count = len(pages)
        text = pages[page]
        if len(pages) > 1:
            text += f"\nСтраница {page + 1} из {len(pages)}"
        actions = self._review_actions(session)
        if session.address.channel != "vk":
            actions = (*nav, *actions)
        return _reply(text, *actions)

    def _review_actions(self, session):
        actions = (self._button(session, "Сохранить", "save"), self._button(session, "Исправить", "edit"),
                   self._button(session, "Отменить", "cancel"))
        if session.address.channel != "vk":
            return actions
        pages = session.review_page_count
        if pages > 1:
            # Одинаковая пара подписей листает по кругу без повторной публикации нижней клавиатуры.
            previous = (session.review_page - 1) % pages
            following = (session.review_page + 1) % pages
            actions = (self._button(session, "← Ранее", "view", str(previous)),
                       self._button(session, "Далее →", "view", str(following)), *actions)
        revision = f"{session.token}.{session.draft.revision}"
        return tuple(replace(action, scope=ActionScope.SECTION, menu_revision=revision) for action in actions)

    def _categories(self, session, *, notice="", update=False):
        picker = session.picker
        with self._repository() as repo:
            categories = repo.categories(session.access)
        pages = max(1, (len(categories) + _PAGE - 1) // _PAGE)
        picker.page = min(picker.page, pages - 1)
        picker.revision = uuid4().hex
        first = picker.page * _PAGE
        lines = [f"{picker.item.product or 'Без детализации'} — {_money(picker.item.amount)}", "",
                 notice or "Выбери категорию."]
        buttons = []
        for row, category in enumerate(categories[first:first + _PAGE]):
            label = category.label
            if len(label) > 40 or label in ("← Ранее", "Далее →", "Создать категорию", "Назад", "Отменить"):
                label = f"{first + row + 1}. {label}"
                lines.append(label)
            buttons.append((label[:40], str(category.id), row))
        nav = len(buttons)
        if picker.page:
            buttons.append(("← Ранее", "previous", nav))
        if picker.page < pages - 1:
            buttons.append(("Далее →", "next", nav))
        if pages > 1:
            lines.append(f"Страница {picker.page + 1} из {pages}")
            nav += 1
        buttons.extend((("Создать категорию", "new", nav),
                        ("Назад" if session.purchase else "Отменить", "back", nav)))
        picker.allowed = tuple(action for _, action, _ in buttons)
        actions = tuple(ReplyAction(label, f"{EXPENSES} pick {session.token} {picker.revision} {action}", row=row)
                        for label, action, row in buttons)
        return session.screen(_reply("\n".join(lines), *actions), update=update)

    def _picker_action(self, context, event, session, operation):
        picker = session.picker
        if operation == "new":
            picker.allowed = ("return",)
            return self._ask(context, event, session, "new_draft_category",
                             "Введи название категории. Семья увидит её после сохранения расхода.",
                             return_command=f"{EXPENSES} pick {session.token} {picker.revision} return")
        if operation == "back":
            session.picker = None
            if session.purchase:
                return session.screen(self._review(session, session.review_page), update=session.address.channel != "vk")
            self._sessions.pop(session.address)
            return _reply("Ввод расхода отменён.", expenses_action(session.access.family_id), close_screen=True)
        if operation in ("previous", "next", "return"):
            session.request = None
            picker.page += int(operation == "next") - int(operation == "previous")
            return self._categories(session, update=operation != "return")
        with self._repository() as repo:
            category = next((c for c in repo.categories(session.access) if str(c.id) == operation), None)
        if category is None:
            return self._categories(session, notice="Категорию скрыли. Выбери другую.", update=True)
        return self._apply_category(session, category.name, category.id, update=True)

    def _apply_category(self, session, name, category_id, *, update):
        picker = session.picker
        item = replace(picker.item, category=name, category_id=category_id)
        if session.purchase is None:
            purchase = ExpensePurchase(session.manual_date, ExpenseSource.MANUAL, (item,))
        else:
            items = list(session.purchase.items)
            items[picker.position:picker.position + 1] = [item]
            purchase = replace(session.purchase, items=tuple(items))
        session.picker = None
        return self._update(session, purchase, update_screen=update)

    def _update(self, session, purchase, *, update_screen=False):
        purchase.reconciled_items()
        session.deadline = self._clock() + _TTL
        previous = session.draft
        session.draft = _Draft(session.access, session.mode, previous.operation_id if previous else session.token,
                               previous.revision + 1 if previous else 1, purchase, session.deadline)
        self._drafts[session.address] = session.draft
        session.request = None
        # В VK обновлённую нижнюю клавиатуру доставляет новое сообщение с исправленным чеком.
        return session.screen(self._review(session), update=update_screen and session.address.channel != "vk")

    def continue_input(self, context: HandlerContext, event: IncomingEvent, access: ExpenseAccess,
                       pending: PendingInput) -> PendingInputResult:
        """Принимает ответ на запрос или запускает распознавание вне блокировки."""
        with self._lock:
            session = self._current(event, context, access)
            if session is None or session.request != pending.request_id:
                return PendingInputResult(self._expired(access), PendingInputOutcome.COMPLETE)
            kind = pending.input_kind.removeprefix("expense:")
            if kind == "new_draft_category" and session.picker:
                try:
                    name = category_name(event.text)
                    with self._repository() as repo:
                        matched = next((c for c in repo.categories(access) if c.name.casefold() == name.casefold()), None)
                        if not matched and any(c.name.casefold() == name.casefold() for c in repo.categories(access, hidden=True)):
                            raise InvalidCategory("Эта категория скрыта. Введи другое название или верни её в настройках.")
                except InvalidCategory as error:
                    return PendingInputResult(_reply(str(error), CANCEL_SECTION_ACTION), PendingInputOutcome.RETRY)
                response = self._apply_category(session, matched.name if matched else name, matched.id if matched else None,
                                                update=False)
                return PendingInputResult(response, PendingInputOutcome.COMPLETE)
            if kind in ("create_category", "rename_category") and session.browser:
                browser = session.browser
                notice = ""
                with self._repository() as repo:
                    try:
                        if kind == "create_category":
                            category = repo.create_category(access, event.text)
                        else:
                            category = repo.rename_category(access, browser.stack[-1].selected_category, event.text)
                        browser.navigate(ReportScreen("category_settings", selected_category=category))
                    except StaleCategory as error:
                        notice = str(error)
                        browser.navigate(ReportScreen("back"))
                    except InvalidCategory as error:
                        return PendingInputResult(_reply(str(error) + "\nВведи другое название.", CANCEL_SECTION_ACTION),
                                                  PendingInputOutcome.RETRY)
                    response = self._report(session, repo, notice)
                session.request = None
                session.deadline = self._clock() + _TTL
                return PendingInputResult(session.screen(response), PendingInputOutcome.COMPLETE)
            if kind == "search_input" and session.browser:
                query = event.text.strip()
                if not 0 < len(query) <= 256:
                    return PendingInputResult(_reply("Введи название до 256 символов.", CANCEL_SECTION_ACTION),
                                              PendingInputOutcome.RETRY)
                with self._repository() as repo:
                    session.browser.find(repo, access, query)
                    session.request = None
                    session.deadline = self._clock() + _TTL
                    response = self._report(session, repo)
                return PendingInputResult(session.screen(response), PendingInputOutcome.COMPLETE)
            if kind in ("month_input", "zone_input") and session.browser:
                try:
                    with self._repository() as repo:
                        if kind == "month_input":
                            month = datetime.strptime(event.text.strip(), "%m.%Y").date()
                            if month.year == 9999 and month.month == 12:
                                raise ValueError
                            session.browser.select_month(month)
                        else:
                            repo.set_timezone(access, event.text.strip())
                            session.browser = ExpenseBrowser.open(repo.timezone(access), event.reply_address.channel)
                        response = self._report(session, repo)
                except (ValueError, InvalidExpense):
                    prompt = (_MONTH_PROMPT if kind == "month_input"
                              else "Пояс не найден. Введи название IANA, например Asia/Omsk.")
                    return PendingInputResult(_reply(prompt, CANCEL_SECTION_ACTION), PendingInputOutcome.RETRY)
                session.request = None
                return PendingInputResult(session.screen(response), PendingInputOutcome.COMPLETE)
            if kind == "manual_name":
                name = event.text.strip()
                if not name or len(name) > 256:
                    return PendingInputResult(_reply("Введи название до 256 символов.", CANCEL_SECTION_ACTION),
                                              PendingInputOutcome.RETRY)
                session.manual_name = name
                session.deadline = self._clock() + _TTL
                response = self._ask(context, event, session, "manual_amount",
                                     f"{name}\n\nВведи сумму в рублях, например 450,50.")
                return PendingInputResult(response, PendingInputOutcome.COMPLETE)
            if kind == "manual_amount":
                try:
                    amount = _cents(event.text)
                except (ValueError, InvalidOperation):
                    return PendingInputResult(_reply(pending.value_label, CANCEL_SECTION_ACTION),
                                              PendingInputOutcome.RETRY)
                session.request = None
                session.deadline = self._clock() + _TTL
                session.picker = _CategoryPicker(ExpenseItem(None, session.manual_name, amount), 0)
                return PendingInputResult(self._categories(session), PendingInputOutcome.COMPLETE)
            if kind == "recognize":
                if len(event.images) > 1 or not (event.images or event.text.strip()):
                    return PendingInputResult(_reply("Пришли одно фото или текст.", CANCEL_SECTION_ACTION),
                                              PendingInputOutcome.RETRY)
                with self._repository() as repo:
                    today = datetime.now(ZoneInfo(repo.timezone(access))).date()
                session.request = None
                return PendingInputResult(_reply("Разбираю расход."), PendingInputOutcome.COMPLETE,
                    cancellable_action=lambda cancelled: self._recognize(event, session, today, cancelled))
            try:
                result = self._edit(session, kind, event.text.strip())
            except (InvalidExpense, ValueError, InvalidOperation, IndexError):
                return PendingInputResult(_reply(pending.value_label, CANCEL_SECTION_ACTION),
                                          PendingInputOutcome.RETRY)
            return PendingInputResult(result, PendingInputOutcome.COMPLETE)

    def _edit(self, session, kind, text):
        field, index = kind.split(":")
        purchase = session.purchase
        if not text or len(text) > 256:
            raise ValueError
        if field == "date":
            purchase = replace(purchase, purchase_date=datetime.strptime(text, "%d.%m.%Y").date())
        elif field == "shop":
            purchase = replace(purchase, description=text)
        elif field == "total":
            purchase = replace(purchase, receipt_total=_cents(text))
        else:
            items = list(purchase.items)
            if field == "insert":
                name, amount = (part.strip() for part in text.split("|"))
                if not name:
                    raise ValueError
                session.picker = _CategoryPicker(ExpenseItem(None, name, _cents(amount)), len(items))
                session.request = None
                return self._categories(session)
            else:
                key = {"name": "product", "amount": "amount"}[field]
                value = _cents(text) if field == "amount" else text
                changes = {key: value}
                if field == "amount" and value != items[int(index)].amount:
                    changes["amount_before_discount"] = None
                items[int(index)] = replace(items[int(index)], **changes)
            purchase = replace(purchase, items=tuple(items))
        return self._update(session, purchase)

    def _recognize(self, event, session, today, cancelled):
        try:
            if self._recognizer is None or cancelled():
                raise ExpenseRecognitionError
            with self._repository() as repo:
                categories = repo.categories(session.access)
            value = event.images[0].read_bytes() if event.images else event.text
            purchase = self._recognizer.recognize(value, today, categories)
            with self._lock:
                if cancelled() or self._sessions.get(event.reply_address) is not session:
                    return self._expired(session.access)
                with self._repository() as repo:
                    current = {c.id: c.name for c in repo.categories(session.access)}
                purchase = replace(purchase, items=tuple(
                    replace(item, category=current[item.category_id]) if item.category_id in current else item
                    for item in purchase.items))
                return self._update(session, purchase)
        except (ImageLoadError, ExpenseRecognitionError, FamilyAccessEnded, InvalidExpense):
            return _reply("Не удалось разобрать расход.\n\n"
                          "Выбери «Ввести пошагово» или открой «Расходы» → «Добавить расход», чтобы отправить его ещё раз.",
                          _action("Ввести пошагово", "manual " + session.token), expenses_action(session.access.family_id))
