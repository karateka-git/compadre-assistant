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

from ..domain.expenses import ExpenseAccess, ExpenseItem, ExpensePurchase, ExpenseRepository, ExpenseSource, InvalidExpense
from ..domain.membership import FamilyAccessEnded
from ..domain.recognition import CATEGORIES, ExpenseRecognitionError, ExpenseRecognizer
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, ScreenDestination
from compadre_assistant.core.presentation.incoming.events import ImageLoadError, IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, HandlerContext, IncomingResponse, PendingInputOutcome, PendingInputResult, ReplyAction,
    ResponseView,
)
from compadre_assistant.core.presentation.message_content import text_content
from .expense_browser import ExpenseBrowser
from .expense_receipt import item_details, money as _money, receipt_header


EXPENSES = "/family_expenses"  # Один вход и адресные действия раздела.
_TTL = 600  # Десять минут после показа редакции.
_PAGE = 4  # Читаемая страница черновика на телефоне.
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
class _Session:
    access: ExpenseAccess
    mode: int | None
    token: str
    address: ReplyAddress
    review_page: int = 0
    draft: _Draft | None = None
    deadline: float = 0
    request: str | None = None
    manual_date: date | None = None
    manual_name: str | None = None
    manual_amount: int | None = None
    browser: ExpenseBrowser | None = None

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

    def _report(self, session, repo):
        return session.browser.render(repo, session.access, session.token,
                                      can_resume=self._available_draft(session) is not None)

    def draft_menu(self, address: ReplyAddress, mode: int, access: ExpenseAccess) -> tuple[ReplyAction, ...]:
        """Возвращает меню чека для текущего членства и действующей редакции в VK."""
        with self._lock:
            session = self._sessions.get(address)
            if (address.channel != "vk" or session is None or session.purchase is None or session.request
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

    def _ask(self, context, event, session, kind: str, prompt: str, *, images=False):
        if context.pending_inputs is None:
            return self._expired(session.access)
        menu_actions = (ReplyAction("Ввести пошагово", f"{EXPENSES} manual {session.token}",
                                    scope=ActionScope.SECTION),) if images else ()
        waiting = context.pending_inputs.begin(event.reply_address, "family", "expense:" + kind,
            app_user_id=context.app_user.id, required_mode="family", value_label=prompt, retry_command=EXPENSES,
            accepts_images=images, cancel_only_menu=True, cancel_return_command=EXPENSES, menu_actions=menu_actions)
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
                if target.kind == "search_input":
                    return self._ask(context, event, session, target.kind,
                                     "Введи название товара или его часть, например «молоко».")
                if target.kind in ("month_input", "zone_input"):
                    prompt = (_MONTH_PROMPT if target.kind == "month_input"
                              else "Введи часовой пояс IANA, например Asia/Omsk.")
                    return self._ask(context, event, session, target.kind, prompt)
                with self._repository() as repo:
                    if target.kind in ("current", "previous"):
                        today = datetime.now(ZoneInfo(repo.timezone(access))).date()
                        month = today if target.kind == "current" else today.replace(day=1) - timedelta(days=1)
                        browser.select_month(month)
                    else:
                        browser.navigate(target)
                    return session.screen(self._report(session, repo), update=target.kind != "chart")
            if args[0] == "discard" and session.purchase is None:
                self._sessions.pop(event.reply_address)
                return _reply("Ввод расхода отменён.", expenses_action(access.family_id), close_screen=True)
            if args[0] == "manual" and session.purchase is None:
                session = self._fresh(event.reply_address, access, context.mode_revision)
                with self._repository() as repo:
                    session.manual_date = datetime.now(ZoneInfo(repo.timezone(access))).date()
                return self._ask(context, event, session, "manual_name",
                                 "Введи название покупки или услуги, например «Такси домой».")
            if args[0] in ("categories", "choose") and session.manual_amount is not None and session.purchase is None:
                if len(args) != 3 or not args[2].isascii() or not args[2].isdigit() or len(args[2]) > 2:
                    return self._expired(access)
                index = int(args[2])
                if args[0] == "categories" and index * _PAGE < len(CATEGORIES):
                    return session.screen(self._categories(session, index), update=True)
                if args[0] == "choose" and index < len(CATEGORIES):
                    purchase = ExpensePurchase(session.manual_date, ExpenseSource.MANUAL,
                        (ExpenseItem(CATEGORIES[index], session.manual_name, session.manual_amount),))
                    return self._update(session, purchase, update_screen=True)
                return self._expired(access)
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
            with self._repository() as repo:
                repo.save(session.access, session.draft.operation_id, session.draft.revision, purchase)
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
        prompts = {"name": "Введи новое название позиции (до 256 символов).",
                   "amount": "Введи сумму за всю позицию в рублях, например 71,99.",
                   "category": "Введи название категории, например «Продукты».",
                   "date": "Введи дату покупки цифрами, например 23.11.2026.",
                   "shop": "Введи магазин или описание покупки (до 256 символов).",
                   "total": "Введи итог к оплате в рублях, например 556,76.",
                   "insert": "Введи название, сумму в рублях и категорию через вертикальную черту.\nХлеб | 59 | Продукты"}
        if verb in prompts:
            index = ""
            if verb in ("name", "amount", "category"):
                index = args[0]
                if not 0 <= int(index) < len(purchase.items):
                    raise ValueError
            return self._ask(context, event, session, f"{verb}:{index}", prompts[verb])
        return self._expired(session.access)

    @staticmethod
    def _button(session, label, verb, argument=""):
        return _action(label, f"{verb} {session.token} {session.draft.revision} {argument}".strip())

    def _pages(self, session, page, verb):
        count = (len(session.purchase.items) + _PAGE - 1) // _PAGE
        if not 0 <= page < count:
            raise ValueError
        actions = []
        for target, label in ((page - 1, "← Ранее"), (page + 1, "Далее →")):
            if 0 <= target < count:
                actions.append(replace(self._button(session, label, verb, str(target)), row=0))
        return session.purchase.items[page*_PAGE:(page+1)*_PAGE], actions, count

    def _positions(self, session, page):
        items, nav, pages = self._pages(session, page, "positions")
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
        items, nav, pages = self._pages(session, page, "view")
        session.review_page = page
        lines = receipt_header(purchase.description or "Расход", purchase.purchase_date,
                               sum(item.amount for item in purchase.items), purchase.receipt_total, "К оплате")
        for i, item in enumerate(items, page*_PAGE+1):
            lines.extend(("", f"{i}. {item.product or 'Без детализации'} — {_money(item.amount)}"))
            lines.extend(item_details(item, show_category=True))
        if pages > 1:
            lines.append(f"Страница {page+1} из {pages}")
        actions = self._review_actions(session)
        if session.address.channel != "vk":
            actions = (*nav, *actions)
        return _reply("\n".join(lines), *actions)

    def _review_actions(self, session):
        actions = (self._button(session, "Сохранить", "save"), self._button(session, "Исправить", "edit"),
                   self._button(session, "Отменить", "cancel"))
        if session.address.channel != "vk":
            return actions
        pages = (len(session.purchase.items) + _PAGE - 1) // _PAGE
        if pages > 1:
            # Одинаковая пара подписей листает по кругу без повторной публикации нижней клавиатуры.
            previous = (session.review_page - 1) % pages
            following = (session.review_page + 1) % pages
            actions = (self._button(session, "← Ранее", "view", str(previous)),
                       self._button(session, "Далее →", "view", str(following)), *actions)
        revision = f"{session.token}.{session.draft.revision}"
        return tuple(replace(action, scope=ActionScope.SECTION, menu_revision=revision) for action in actions)

    def _categories(self, session, page):
        first = page * _PAGE
        actions = [replace(_action(name, f"choose {session.token} {first + row}"), row=row)
                   for row, name in enumerate(CATEGORIES[first:first + _PAGE])]
        nav = len(actions)
        for target, label in ((page - 1, "← Ранее"), (page + 1, "Далее →")):
            if 0 <= target * _PAGE < len(CATEGORIES):
                actions.append(replace(_action(label, f"categories {session.token} {target}"), row=nav))
        actions.append(replace(_action("Отменить", "discard " + session.token), row=nav + 1))
        lines = [f"{session.manual_name} — {_money(session.manual_amount)}", "", "Выбери категорию."]
        if len(CATEGORIES) > _PAGE:
            lines.append(f"Страница {page + 1} из {(len(CATEGORIES) + _PAGE - 1) // _PAGE}")
        return _reply("\n".join(lines), *actions)

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
                    session.manual_amount = _cents(event.text)
                except (ValueError, InvalidOperation):
                    return PendingInputResult(_reply(pending.value_label, CANCEL_SECTION_ACTION),
                                              PendingInputOutcome.RETRY)
                session.request = None
                session.deadline = self._clock() + _TTL
                return PendingInputResult(session.screen(self._categories(session, 0)), PendingInputOutcome.COMPLETE)
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
                name, amount, category = (part.strip() for part in text.split("|"))
                items.append(ExpenseItem(category, name, _cents(amount)))
            else:
                key = {"name": "product", "amount": "amount", "category": "category"}[field]
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
            value = event.images[0].read_bytes() if event.images else event.text
            purchase = self._recognizer.recognize(value, today)
            with self._lock:
                if cancelled() or self._sessions.get(event.reply_address) is not session:
                    return self._expired(session.access)
                with self._repository() as repo:
                    repo.timezone(session.access)
                return self._update(session, purchase)
        except (ImageLoadError, ExpenseRecognitionError, FamilyAccessEnded, InvalidExpense):
            return _reply("Не удалось разобрать расход.\n\n"
                          "Выбери «Ввести пошагово» или открой «Расходы» → «Добавить расход», чтобы отправить его ещё раз.",
                          _action("Ввести пошагово", "manual " + session.token), expenses_action(session.access.family_id))
