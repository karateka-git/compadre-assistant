"""Ведёт семейные диалоги в активном режиме личного чата."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime

from compadre_assistant.capabilities.family.domain.invitations import (
    InvalidInvitation, InvitationExpired, InvitationUnavailable, InvitationUsed, ManageInvitations,
)
from compadre_assistant.capabilities.family.domain.manage_family import ManageFamily
from compadre_assistant.capabilities.family.domain.expenses import ExpenseAccess
from compadre_assistant.capabilities.family.presentation.expense_entry import EXPENSES, ExpenseEntry
from compadre_assistant.capabilities.family.domain.membership import (
    AlreadyInFamily, Family, FamilyAccessEnded, FamilyHasOtherMembers, FamilyMember, InvalidFamilyName,
    OtherMemberRequired, OwnerCannotLeave, OwnerRequired,
)
from compadre_assistant.capabilities.family.domain.shopping import (
    InvalidShoppingNames, ItemNumbersExhausted, ManageShopping, ShoppingAddMessage, ShoppingItem,
)
from compadre_assistant.capabilities.family.presentation.notifications import shopping_reference
from compadre_assistant.capabilities.family.presentation.confirmations import MemberConfirmation, MemberConfirmations
from compadre_assistant.capabilities.family.presentation.members import (
    CLOSE_COMMAND, LEAVE_COMMAND, MEMBERS_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND, MemberDisplayNames, MembersView,
)
from compadre_assistant.capabilities.family.presentation.shopping import (
    ADD_COMMAND, BUY_COMMAND, CLEAR_BOUGHT_COMMAND, DELETE_COMMAND, SHOPPING_COMMAND,
    ShoppingChoice, ShoppingSection, ShoppingSections, ShoppingSelection, choice_page_for_list, parse_item_number,
    selection_page_size, shopping_list, shopping_notice, shopping_page, shopping_section_actions,
)
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.action_contracts import (
    ActionPurpose, ActionScope, ScreenDestination, SectionMenuDelivery,
)
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.menu import HELP_SECTION_ACTION
from compadre_assistant.core.presentation.incoming.ports import (
    CANCEL_SECTION_ACTION, CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult,
    CommandParameter, ConfirmationQueryResult,
    ContextualCommandQueryPort, HandlerContext, HelpSection,
    IncomingCapabilityPort, IncomingResponse, NotificationEntryPort, PendingConfirmationQueryPort, PendingInputContinuationPort,
    PendingInputOutcome, PendingInputResult, ReplyAction, ReplyActionRecoveryPort, ResponseView, SectionActionsQueryPort,
)
from compadre_assistant.core.presentation.message_content import code_span, compose_content, text_content


_CREATE = "/family_create"
_JOIN = "/family_join"
_INVITE = "/family_invite"
_MEMBER_ACTIONS = (LEAVE_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND, CLOSE_COMMAND)
_SHOPPING_ACTIONS = (ADD_COMMAND, BUY_COMMAND, DELETE_COMMAND, CLEAR_BOUGHT_COMMAND)
_EXIT_ACTION = ReplyAction(
    "Выйти из режима", "/exit", scope=ActionScope.SECTION, purpose=ActionPurpose.NAVIGATION,
    destination=ScreenDestination("main"),
)


def _family_actions(family: Family | None) -> tuple[ReplyAction, ...]:
    if family is None:
        return (
            ReplyAction("Создать семью", _CREATE, scope=ActionScope.SECTION),
            ReplyAction("Вступить по коду", _JOIN, scope=ActionScope.SECTION),
            _EXIT_ACTION,
            HELP_SECTION_ACTION,
        )
    entries = (
        ("Покупки", SHOPPING_COMMAND, ScreenDestination("shopping-list", 1)),
        ("Расходы", EXPENSES, ScreenDestination("family-expenses")),
        ("Участники", MEMBERS_COMMAND, ScreenDestination("family-members", 1)),
        ("Пригласить", _INVITE, ScreenDestination("family-invite")),
    )
    return (
        *(ReplyAction(label, f"{command} open {family.id}", scope=ActionScope.SECTION,
                      purpose=ActionPurpose.NAVIGATION, destination=destination)
          for label, command, destination in entries),
        ReplyAction("Помощь", "/help", scope=ActionScope.SECTION),
        _EXIT_ACTION,
    )


class FamilyCapability(
    IncomingCapabilityPort, NotificationEntryPort, ContextualCommandQueryPort, SectionActionsQueryPort,
    PendingInputContinuationPort, PendingConfirmationQueryPort, ReplyActionRecoveryPort,
):
    """Открывает семейный режим и обрабатывает команды зарегистрированного профиля."""

    descriptor = CapabilityDescriptor(
        "family",
        CapabilityCommand("/family", (), "открыть раздел «Семья»"),
        CapabilityHelp(sections=(HelpSection("Семья", (
            CapabilityCommand(_CREATE, (), "создать семью"),
            CapabilityCommand(_JOIN, (), "вступить по коду"),
            CapabilityCommand(_INVITE, (CommandParameter("action", "create", False),), "пригласить в семью"),
            CapabilityCommand(SHOPPING_COMMAND, (), "открыть покупки"),
            CapabilityCommand(EXPENSES, (), "открыть расходы семьи"),
        )), HelpSection("Участники", (
            CapabilityCommand(MEMBERS_COMMAND, (CommandParameter("member", "<member>", False),), "посмотреть участников"),
            CapabilityCommand(LEAVE_COMMAND, (), "выйти из семьи"),
            CapabilityCommand(REMOVE_COMMAND, (CommandParameter("member", "<member>", False),), "исключить участника"),
            CapabilityCommand(TRANSFER_COMMAND, (CommandParameter("member", "<member>", False),), "передать роль владельца"),
            CapabilityCommand(CLOSE_COMMAND, (), "закрыть семью"),
        ), instructions=(
            compose_content(code_span(f"{MEMBERS_COMMAND} commands [<page>]"),
                            " — показать коды участников для ручных команд"),
        )), HelpSection(
            "Команды после открытия списка «Покупки»",
            (
                CapabilityCommand(
                    ADD_COMMAND, (CommandParameter("items", "[<items>]", False),),
                    "добавить покупки. Без аргументов бот попросит прислать названия, по одному на строку",
                ),
                CapabilityCommand(BUY_COMMAND, (), "выбрать покупку кнопкой или номером и отметить её купленной"),
                CapabilityCommand(DELETE_COMMAND, (), "выбрать покупку кнопкой или номером и удалить её из «Нужно купить»"),
                CapabilityCommand(CLEAR_BOUGHT_COMMAND, (), "удалить все купленные позиции"),
            ),
            instructions=(
                compose_content(code_span(f"{SHOPPING_COMMAND} page <number>"), " — открыть страницу списка"),
                compose_content(code_span(f"{SHOPPING_COMMAND} buy <page>"), " — открыть страницу выбора, что куплено"),
                compose_content(code_span(f"{SHOPPING_COMMAND} delete <page>"), " — открыть страницу выбора для удаления"),
                compose_content(code_span(f"{SHOPPING_COMMAND} back"), " — вернуться из выбора товара к списку"),
                compose_content(code_span(f"{SHOPPING_COMMAND} home"), " — вернуться из списка в меню семьи"),
            ),
            menu_help_only=True,
        ))),
        enabled_by_default=True,
        accepts_images=True,
        display_name="«Семья»",
        private_chat_hint="Открой личный чат с ботом, чтобы перейти в раздел «Семья».",
    )

    def __init__(
        self, open_scenarios: Callable[[], AbstractContextManager[tuple[ManageFamily, ManageInvitations, ManageShopping]]],
        display_names: MemberDisplayNames,
        *, expense_entry: ExpenseEntry,
    ) -> None:
        """Принимает фабрику предметных сценариев на одно событие."""
        self._open_scenarios = open_scenarios
        self._expense_entry = expense_entry
        self._members_view = MembersView(display_names)
        self._confirmations = MemberConfirmations()
        self._shopping_sections = ShoppingSections()
        # Снимок шага справки привязан к ревизии режима и конкретному членству.
        self._help_screens: dict[tuple[int, ReplyAddress], tuple[int | None, int, datetime, str]] = {}

    def notification_destination(self, app_user_id: int, address: ReplyAddress, reference: str) -> str | None:
        """Проверяет, что адрес всё ещё принадлежит участнику указанного состава семьи."""
        with self._open_scenarios() as (families, _, _):
            family = families.find_for_member(app_user_id)
            if family is None:
                return None
            try:
                members = families.list_members(app_user_id, family.id)
            except FamilyAccessEnded:
                return None
            member = next((member for member in members if member.app_user_id == app_user_id), None)
            if member and reference == shopping_reference(family.id, app_user_id, member.joined_at, address):
                return f"{SHOPPING_COMMAND} notice {reference}"
        return None

    def enter(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> CapabilityResult:
        """Открывает начальный экран или показывает текущую семью.

        Args:
            context: Допущенный профиль и доступные порты ядра.
            event: Событие команды входа.
            arguments: Текст после команды, которая вызывается без аргументов.

        Returns:
            Текущий экран или подсказка без изменения режима при неверном синтаксисе.
        """
        if arguments:
            return CapabilityResult(
                IncomingResponse(text_content("Открой раздел «Семья» кнопкой ниже."),
                                 suggested_actions=(ReplyAction("Семья", "/family"), HELP_SECTION_ACTION)),
                CapabilityMode.COMPLETE,
            )
        with self._open_scenarios() as (families, _, _):
            self._help_screens.pop((context.app_user.id, event.reply_address), None)
            return CapabilityResult(self._home(families.find_for_member(context.app_user.id)))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Выполняет команду семьи после проверки режима ядром.

        Args:
            context: Допущенный профиль и ожидание ввода в текущем чате.
            event: Текстовая команда или действие ответа из этого чата.

        Returns:
            Экран семьи, запрос ввода, приглашение или результат действия с участниками.
        """
        result = self._handle(context, event)
        response = result.response
        if event.text.split()[:1] == [EXPENSES]:
            return result
        if event.text.split()[:2] == [MEMBERS_COMMAND, "commands"]:
            return result
        actions = response.suggested_actions
        if not actions and response.view is None and not response.actions:
            actions = self.section_actions(context.app_user, event.reply_address, context.mode_revision, None)
        if not any(action.command == "/help" for action in actions):
            actions = (*actions, HELP_SECTION_ACTION)
        return replace(result, response=replace(response, suggested_actions=actions))

    def _handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        parts = event.text.split(maxsplit=1)
        command = parts[0] if parts else ""
        arguments = parts[1] if len(parts) > 1 else ""
        app_user_id = context.app_user.id
        with self._open_scenarios() as (families, invitations, shopping):
            family = families.find_for_member(app_user_id)
            if family is not None:
                members = families.list_members(app_user_id, family.id)
                actor = next(member for member in members if member.app_user_id == app_user_id)
                if command == EXPENSES:
                    self._shopping_sections.close(app_user_id, event.reply_address)
                    return CapabilityResult(self._expense_entry.handle(
                        context, event, ExpenseAccess(family.id, app_user_id, actor.joined_at), arguments))
                self._expense_entry.leave(event.reply_address)
                screen = command if command in (_INVITE, MEMBERS_COMMAND) else ""
                if command == MEMBERS_COMMAND and len(arguments.split()) == 1 and arguments != "commands":
                    screen = "member:" + arguments
                self._help_screens[app_user_id, event.reply_address] = (
                    context.mode_revision, family.id, actor.joined_at, screen,
                )
            if command == SHOPPING_COMMAND or command in _SHOPPING_ACTIONS:
                return CapabilityResult(self._shopping(context, event, families, family, shopping, command, arguments))
            self._shopping_sections.close(app_user_id, event.reply_address)
            if command in (_CREATE, _JOIN):
                return CapabilityResult(replace(
                    self._begin_input(context, event, family, command, arguments), close_managed_views=True,
                ))
            if command == _INVITE:
                return CapabilityResult(replace(
                    self._invite(context, invitations, family, arguments),
                    close_managed_views=True,
                ))
            if command == MEMBERS_COMMAND:
                return CapabilityResult(replace(
                    self._members(context, event, families, family, arguments),
                    close_managed_views=True,
                ))
            if command in _MEMBER_ACTIONS:
                response = self._begin_member_action(
                    context, event.reply_address, families, family, command, arguments,
                )
                return CapabilityResult(replace(response, close_managed_views=True))
            if command == "/confirm" and arguments and arguments.split(maxsplit=1)[0] == "family":
                return CapabilityResult(replace(self._confirm_member_action(
                    context, event.reply_address, families, family, " ".join(event.text.split()),
                ), close_managed_views=True))
            return CapabilityResult(self._home(family))

    def visible_mode_commands(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
    ) -> frozenset[str]:
        """Отбирает разделы справки по членству профиля и открытому экрану."""
        with self._open_scenarios() as (families, _, _):
            family = families.find_for_member(app_user.id)
            if family is None:
                return frozenset((_CREATE, _JOIN))
            try:
                members = families.list_members(app_user.id, family.id)
            except FamilyAccessEnded:
                return frozenset((_CREATE, _JOIN))
            actor = next(member for member in members if member.app_user_id == app_user.id)
            current = self._shopping_sections.current(app_user.id, address, mode_revision, touch=False)
            if current is not None and current.matches(family.id, actor.joined_at):
                return frozenset((SHOPPING_COMMAND, *_SHOPPING_ACTIONS))
            snapshot = self._help_screens.get((app_user.id, address))
            screen = snapshot[3] if snapshot and snapshot[:3] == (mode_revision, family.id, actor.joined_at) else ""
            if screen == _INVITE:
                return frozenset((_INVITE, "/family"))
            if screen == MEMBERS_COMMAND or screen.startswith("member:"):
                commands = self._member_commands(actor, len(members)) - {_INVITE}
                if screen.startswith("member:"):
                    context = HandlerContext(app_user, mode_revision=mode_revision)
                    target = self._members_view.resolve(context, address, family.id, members, screen[7:])
                    if target is None or target.app_user_id == actor.app_user_id:
                        commands -= {REMOVE_COMMAND, TRANSFER_COMMAND}
                return commands | {"/family"}
            return frozenset((_INVITE, MEMBERS_COMMAND, SHOPPING_COMMAND, EXPENSES))

    def section_actions(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[ReplyAction, ...]:
        """Читает доступные действия открытого раздела или меню семьи.

        Args:
            app_user: Профиль с проверенным доступом к семейному режиму.
            address: Личный чат, в котором открыт режим.
            mode_revision: Ревизия режима в текущем чате.
            pending: Действующее ожидание, которое уже проверило ядро.

        Returns:
            Действия текущего раздела без изменения семьи, вида или ожидания.
        """
        if pending is not None and pending.cancel_only_menu:
            return (CANCEL_SECTION_ACTION,)
        app_user_id = app_user.id
        with self._open_scenarios() as (families, _, _):
            family = families.find_for_member(app_user_id)
            if family is None:
                return _family_actions(None)
            section = self._shopping_sections.current(app_user_id, address, mode_revision, touch=False)
            if section is None:
                return _family_actions(family)
            try:
                actor = next(member for member in families.list_members(app_user_id, family.id)
                             if member.app_user_id == app_user_id)
            except FamilyAccessEnded:
                return _family_actions(None)
            if not section.matches(family.id, actor.joined_at):
                return _family_actions(family)
        return shopping_section_actions(section, from_selection=section.selection_shown.is_set())

    def reply_action_recovery(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int | None,
    ) -> ReplyAction:
        """Предлагает покупки только для ещё открытого и доступного списка."""
        home = ReplyAction("Семья", self.descriptor.entry.name)
        section = (self._shopping_sections.current(app_user.id, address, mode_revision)
                   if mode_revision is not None else None)
        if section is None:
            return home
        with self._open_scenarios() as (families, _, _):
            try:
                members = families.list_members(app_user.id, section.family_id)
            except FamilyAccessEnded:
                return home
        return (ReplyAction("Покупки", SHOPPING_COMMAND) if any(
            member.app_user_id == app_user.id and section.joined_at == member.joined_at
            for member in members
        ) else home)

    @staticmethod
    def _member_commands(actor: FamilyMember, member_count: int) -> frozenset[str]:
        if not actor.is_owner:
            return frozenset((_INVITE, MEMBERS_COMMAND, LEAVE_COMMAND))
        if member_count == 1:
            return frozenset((_INVITE, MEMBERS_COMMAND, CLOSE_COMMAND))
        return frozenset((_INVITE, MEMBERS_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND))

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Возвращает адресное подтверждение, пока его исходные условия действуют."""
        pending = self._confirmations.current(context, origin)
        if pending is None:
            return ConfirmationQueryResult("complete")
        with self._open_scenarios() as (families, _, _):
            if not self._member_action_is_current(families, pending):
                return ConfirmationQueryResult("complete")
        return ConfirmationQueryResult("complete", (pending.confirmation,))

    @staticmethod
    def _member_action_is_current(families: ManageFamily, pending: MemberConfirmation) -> bool:
        # Снимок для меню и отмены. При выполнении записи репозиторий проверит условия в транзакции.
        try:
            members = families.list_members(pending.actor.app_user_id, pending.family_id)
        except FamilyAccessEnded:
            return False
        return (
            pending.actor in members
            and (pending.target is None or pending.target in members)
            and (pending.confirmation.operation != "close" or len(members) == 1)
        )

    def _begin_member_action(
        self, context: HandlerContext, address: ReplyAddress, families: ManageFamily,
        family: Family | None, command: str, arguments: str,
    ) -> IncomingResponse:
        if family is None or (command in (LEAVE_COMMAND, CLOSE_COMMAND) and arguments and arguments != str(family.id)):
            return self._family_access_ended()
        try:
            members = families.list_members(context.app_user.id, family.id)
        except FamilyAccessEnded:
            return self._family_access_ended()
        actor = next(member for member in members if member.app_user_id == context.app_user.id)
        target = None
        if command == LEAVE_COMMAND:
            if actor.is_owner:
                return self._owner_cannot_leave(len(members) > 1)
            question, label = f"Выйти из семьи «{family.name}»? Ты потеряешь доступ к общим покупкам.", "Выйти из семьи"
        elif not actor.is_owner:
            return IncomingResponse(text_content("Это действие доступно только владельцу семьи."))
        elif command == CLOSE_COMMAND:
            if len(members) > 1:
                return self._cannot_close()
            question = f"Закрыть семью «{family.name}»? Общий список покупок удалится, а коды перестанут действовать."
            label = "Закрыть семью"
        else:
            if not arguments:
                return self._members_view.show(context, address, family, members, 1)
            target = self._members_view.resolve(context, address, family.id, members, arguments)
            if target is None or target.app_user_id == actor.app_user_id:
                return self._target_changed()
            name = self._members_view.display_name(target)
            if command == REMOVE_COMMAND:
                question, label = f"Исключить участника «{name}» из семьи «{family.name}»?", "Исключить"
            else:
                question, label = f"Передать роль владельца семьи «{family.name}» участнику «{name}»?", "Передать роль"
        pending = self._confirmations.begin(
            context, address, command.removeprefix("/family_"), family.id, actor, target, label,
        )
        return IncomingResponse(text_content(
            f"{question}\n\nПодтверждение действует 10 минут.",
        ), actions=pending.actions, suggested_actions=(HELP_SECTION_ACTION,))

    def _confirm_member_action(
        self, context: HandlerContext, address: ReplyAddress, families: ManageFamily,
        family: Family | None, command: str,
    ) -> IncomingResponse:
        taken = self._confirmations.take(context, address, command)
        if taken is None:
            return self._confirmation_unavailable()
        pending, approved = taken
        if family is None or family.id != pending.family_id:
            return self._family_access_ended()
        if not approved:
            if not self._member_action_is_current(families, pending):
                return self._confirmation_unavailable()
            return self._home(family, prefix="Действие отменено.")
        actor_id = pending.actor.app_user_id
        actor_joined_at = pending.actor.joined_at
        operation = pending.confirmation.operation
        try:
            if operation == "leave":
                families.leave(actor_id, pending.family_id, actor_joined_at=actor_joined_at)
                return self._home(None, prefix="Ты вышел из семьи.")
            if operation == "close":
                families.close(actor_id, pending.family_id, actor_joined_at=actor_joined_at)
                return self._home(None, prefix="Семья закрыта.")
            target = pending.target
            if target is None:
                raise ValueError("Для этого подтверждения нужен другой участник")
            if operation == "remove":
                families.remove_member(
                    actor_id, pending.family_id, target.app_user_id,
                    actor_joined_at=actor_joined_at, target_joined_at=target.joined_at,
                )
                return self._home(family, prefix="Участник исключён из семьи.")
            families.transfer_ownership(
                actor_id, pending.family_id, target.app_user_id,
                actor_joined_at=actor_joined_at, target_joined_at=target.joined_at,
            )
            return self._home(family, prefix="Роль владельца передана.")
        except FamilyAccessEnded:
            return self._family_access_ended()
        except OwnerCannotLeave as error:
            return self._owner_cannot_leave(error.has_other_members)
        except FamilyHasOtherMembers:
            return self._cannot_close()
        except OwnerRequired:
            return IncomingResponse(text_content("Это действие доступно только владельцу семьи."))
        except OtherMemberRequired:
            return self._target_changed()

    @staticmethod
    def _owner_cannot_leave(has_other_members: bool) -> IncomingResponse:
        if has_other_members:
            return IncomingResponse(compose_content(
                "Перед выходом передай роль владельца. Открой участников и выбери нового владельца.",
            ))
        return IncomingResponse(compose_content(
            "Ты единственный участник. Семью можно закрыть из списка участников.",
        ))

    @staticmethod
    def _cannot_close() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Закрыть семью может единственный участник. Открой состав семьи.",
        ))

    @staticmethod
    def _target_changed() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Выбери другого действующего участника в составе семьи.",
        ))

    @staticmethod
    def _confirmation_unavailable() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Подтверждение недоступно. Открой семью и начни действие заново.",
        ))

    def continue_input(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent,
    ) -> PendingInputResult:
        """Принимает название, код, список покупок или номер из показанного списка.

        Args:
            context: Повторно допущенный профиль.
            pending: Действующее ожидание с исходным сроком.
            event: Сообщение с ожидаемым значением.

        Returns:
            Результат и признак того, может ли пользователь исправить ввод.
        """
        app_user_id = context.app_user.id
        command = pending.input_kind
        if command.startswith("expense:"):
            with self._open_scenarios() as (families, _, _):
                family = families.find_for_member(app_user_id)
                if family is not None:
                    members = families.list_members(app_user_id, family.id)
                    actor = next((member for member in members if member.app_user_id == app_user_id), None)
                    if actor is not None:
                        access = ExpenseAccess(family.id, app_user_id, actor.joined_at)
                        return self._expense_entry.continue_input(context, event, access, pending)
            return PendingInputResult(self._family_access_ended(), PendingInputOutcome.COMPLETE)
        if command == ADD_COMMAND:
            return self._continue_shopping_add(context, pending, event)
        if command in (BUY_COMMAND, DELETE_COMMAND):
            return self._continue_shopping_choice(context, pending, event)
        if command not in (_CREATE, _JOIN):
            return PendingInputResult(IncomingResponse(compose_content(
                "Открой семью и начни действие заново.",
            ), suggested_actions=(ReplyAction("Семья", "/family"), HELP_SECTION_ACTION)), PendingInputOutcome.COMPLETE)
        with self._open_scenarios() as (families, invitations, _):
            try:
                if command == _CREATE:
                    family = families.create(app_user_id, event.text)
                    prefix = "Семья создана."
                else:
                    family = invitations.join(app_user_id, event.text)
                    prefix = "Ты вступил в семью."
            except AlreadyInFamily:
                return PendingInputResult(self._home(
                    families.find_for_member(app_user_id),
                    prefix="Твой профиль уже состоит в семье.",
                ), PendingInputOutcome.COMPLETE)
            except InvalidFamilyName:
                return self._retry_input("Напиши непустое название семьи.")
            except InvitationUsed:
                return self._retry_input("Код уже использован. Попроси новый у того, кто тебя пригласил.")
            except InvitationExpired:
                return self._retry_input("Код просрочен. Попроси новый у того, кто тебя пригласил.")
            except InvalidInvitation:
                return self._retry_input("Код недействителен. Проверь его или попроси новый у того, кто тебя пригласил.")
        return PendingInputResult(self._home(family, prefix=prefix), PendingInputOutcome.COMPLETE)

    @staticmethod
    def _begin_input(
        context: HandlerContext, event: IncomingEvent, family: Family | None, command: str, arguments: str,
    ) -> IncomingResponse:
        if family is not None:
            return FamilyCapability._home(family, prefix="Твой профиль уже состоит в семье.")
        if arguments:
            return IncomingResponse(compose_content(
                "Начни ввод кнопкой в меню семьи. Значение пришли следующим сообщением.",
            ))
        if context.pending_inputs is None:
            return IncomingResponse(text_content("Не удалось начать ввод. Попробуй ещё раз."))
        label = "название семьи" if command == _CREATE else "код приглашения"
        context.pending_inputs.begin(
            event.reply_address, FamilyCapability.descriptor.capability_id, command, app_user_id=context.app_user.id,
            required_mode=FamilyCapability.descriptor.capability_id, value_label=label, retry_command=command,
            cancel_only_menu=True, cancel_return_command="/family",
        )
        return IncomingResponse(compose_content(
            f"Пришли {label}. На ввод 10 минут. Отменить ввод можно кнопкой «Отмена».",
        ), suggested_actions=(CANCEL_SECTION_ACTION,), command_menu_delivery=SectionMenuDelivery.RECONCILE)

    @staticmethod
    def _retry_input(answer: str) -> PendingInputResult:
        return PendingInputResult(IncomingResponse(compose_content(
            answer, " Для отмены нажми «Отмена».",
        ), suggested_actions=(CANCEL_SECTION_ACTION,)), PendingInputOutcome.RETRY)

    @staticmethod
    def _home(family: Family | None, *, prefix: str = "") -> IncomingResponse:
        introduction = prefix + "\n\n" if prefix else ""
        title = "Семья" if family is None else f"Семья «{family.name}»"
        return IncomingResponse(
            text_content(introduction + title), suggested_actions=_family_actions(family),
            command_menu_delivery=SectionMenuDelivery.RECONCILE, close_managed_views=True,
        )

    def _shopping(
        self, context: HandlerContext, event: IncomingEvent, families: ManageFamily,
        family: Family | None, shopping: ManageShopping, command: str, arguments: str,
    ) -> IncomingResponse:
        app_user_id = context.app_user.id
        address = event.reply_address
        revision = context.mode_revision
        parts = arguments.split()
        if family is None or revision is None:
            return self._family_access_ended()
        if command == SHOPPING_COMMAND and len(parts) == 2 and parts[0] == "open" and parts[1] != str(family.id):
            return self._family_access_ended()
        try:
            actor = next(member for member in families.list_members(app_user_id, family.id)
                         if member.app_user_id == app_user_id)
            if command == SHOPPING_COMMAND and parts[:1] == ["notice"]:
                if parts != ["notice", shopping_reference(family.id, app_user_id, actor.joined_at, address)]:
                    return self._family_access_ended()
                parts = []
            if command == SHOPPING_COMMAND and (not parts or parts == ["open", str(family.id)]):
                section = ShoppingSection(revision, family.id, actor.joined_at)
                items = shopping.list_items(app_user_id, family.id, actor_joined_at=section.joined_at)
                section = section.ordinary_view(items, 1)
                self._shopping_sections.open(app_user_id, address, section)
                return shopping_list(items, section)
            current = self._shopping_sections.current(app_user_id, address, revision)
            if current is not None and not current.matches(family.id, actor.joined_at):
                return self._family_access_ended()
            if current is None:
                return self._open_shopping_hint()
            if command in _SHOPPING_ACTIONS:
                return self._shopping_action(context, event, shopping, current, command, arguments)
            if len(parts) >= 2 and parts[0] in ("click", "section"):
                if parts[0] == "click" and event.callback_message_id is None:
                    return self._open_shopping_hint()
                if parts[1] != current.action_key:
                    return IncomingResponse(compose_content(
                        "Эта кнопка устарела. Открой покупки заново.",
                    ))
                if parts[0] == "click":
                    return self._shopping_click(context, address, shopping, current, parts[2:])
                if parts[2:] != ["home"]:
                    return self._shopping_section_action(context, event, shopping, current, parts[2:])
                parts = ["home"]
            if parts == ["home"]:
                self._shopping_sections.close(app_user_id, address)
                return self._home(family)
            if parts == ["back"]:
                items = shopping.list_items(app_user_id, family.id, actor_joined_at=current.joined_at)
                return self._show_shopping_list(context, address, current, items, current.ordinary_page, update=True)
            if len(parts) != 2 or parts[0] not in ("page", "item", *ShoppingSelection):
                return self._open_shopping_hint()
            number = parse_item_number(parts[1])
            if number is None:
                return self._open_shopping_hint()
            items = shopping.list_items(app_user_id, family.id, actor_joined_at=current.joined_at)
        except FamilyAccessEnded:
            return self._family_access_ended()
        if parts[0] == "page":
            return self._show_shopping_list(context, address, current, items, number)
        if parts[0] in tuple(ShoppingSelection):
            return self._begin_shopping_choice(context, address, current, items, ShoppingSelection(parts[0]), number)
        item = next((item for item in items if item.number == number), None)
        if item is None:
            return IncomingResponse(text_content(f"№{number} нет в списке."))
        return IncomingResponse(text_content(f"{item.number}. {item.name}"))

    def _shopping_section_action(
        self, context: HandlerContext, event: IncomingEvent, shopping: ManageShopping, section: ShoppingSection,
        action: list[str],
    ) -> IncomingResponse:
        app_user_id = context.app_user.id
        address = event.reply_address
        family_id = section.family_id
        joined_at = section.joined_at
        update = event.callback_message_id is not None
        match action:
            case ["add"]:
                return self._begin_shopping_add(context, address, section)
            case ["clear"]:
                items = shopping.clear_bought(app_user_id, family_id, actor_joined_at=joined_at)
                return self._show_shopping_list(context, address, section, items, section.ordinary_page, update=update)
            case ["choose", "buy" | "delete" as view, origin] if origin == str(section.ordinary_page):
                items = shopping.list_items(app_user_id, family_id, actor_joined_at=joined_at)
                page = choice_page_for_list(section.ordinary_page, selection_page_size(context.action_layout))
                return self._begin_shopping_choice(context, address, section, items, ShoppingSelection(view), page)
            case ["page", value] if (page := parse_item_number(value)) is not None:
                items = shopping.list_items(app_user_id, family_id, actor_joined_at=joined_at)
                return self._show_shopping_list(context, address, section, items, page, update=update)
            case ["back"]:
                items = shopping.list_items(app_user_id, family_id, actor_joined_at=joined_at)
                return self._show_shopping_list(context, address, section, items, section.ordinary_page, update=update)
            case _:
                return self._open_shopping_hint()

    def _shopping_click(
        self, context: HandlerContext, address: ReplyAddress, shopping: ManageShopping, section: ShoppingSection,
        action: list[str],
    ) -> IncomingResponse:
        # У ядра уже есть адрес и подтверждённое поколение сообщения. Ключ раздела
        # дополнительно исключает нажатия до повторного вступления или открытия раздела.
        if len(action) < 2 or (page := parse_item_number(action[1])) is None:
            return self._open_shopping_hint()
        app_user_id = context.app_user.id
        family_id = section.family_id
        joined_at = section.joined_at
        match action:
            case ["pick", _, "buy" | "delete" as view, position_text, value]:
                number = parse_item_number(value)
                position = parse_item_number(position_text)
                if number is None or position is None:
                    return self._open_shopping_hint()
                selection = ShoppingSelection(view)
                change = shopping.buy if selection == ShoppingSelection.BUY else shopping.delete
                result = change(app_user_id, family_id, (number,), actor_joined_at=joined_at)
                return self._begin_shopping_choice(
                    context, address, section, result.items, selection, page,
                    notice=shopping_notice(result.changes[0], position),
                )
            case ["show", _, "page" | "buy" | "delete" as view]:
                items = shopping.list_items(app_user_id, family_id, actor_joined_at=joined_at)
                if view == "page":
                    return self._show_shopping_list(context, address, section, items, page, update=True)
                return self._begin_shopping_choice(context, address, section, items, ShoppingSelection(view), page)
            case _:
                return self._open_shopping_hint()

    def _shopping_action(
        self, context: HandlerContext, event: IncomingEvent, shopping: ManageShopping,
        section: ShoppingSection, command: str, arguments: str,
    ) -> IncomingResponse:
        app_user_id = context.app_user.id
        family_id = section.family_id
        address = event.reply_address
        if command == ADD_COMMAND:
            if not arguments:
                return self._begin_shopping_add(context, address, section)
            try:
                items = shopping.add(
                    app_user_id, family_id, arguments, actor_joined_at=section.joined_at,
                    source=ShoppingAddMessage(event.channel, event.chat_id, event.message_id),
                )
                return self._show_shopping_list(context, address, section, items)
            except ItemNumbersExhausted:
                return self._shopping_numbers_exhausted()
        if command == CLEAR_BOUGHT_COMMAND:
            if arguments:
                return IncomingResponse(text_content("Для очистки нажми «Очистить купленное» в списке покупок."))
            items = shopping.clear_bought(app_user_id, family_id, actor_joined_at=section.joined_at)
            return self._show_shopping_list(context, address, section, items)

        if arguments:
            return IncomingResponse(compose_content(
                "Открой выбор кнопкой «Купить» или «Удалить». Затем выбери товар или пришли его номер.",
            ))
        selection = ShoppingSelection.BUY if command == BUY_COMMAND else ShoppingSelection.DELETE
        items = shopping.list_items(app_user_id, family_id, actor_joined_at=section.joined_at)
        return self._begin_shopping_choice(context, address, section, items, selection, 1)

    def _begin_shopping_choice(
        self, context: HandlerContext, address: ReplyAddress, section: ShoppingSection, items: tuple[ShoppingItem, ...],
        selection: ShoppingSelection, page: int, *, notice: str | None = None,
    ) -> IncomingResponse:
        page_size = selection_page_size(context.action_layout)
        snapshot = shopping_page(items, page, selection_size=page_size)
        pending_inputs = context.pending_inputs
        if pending_inputs is None:
            return self._open_shopping_hint()
        app_user_id = context.app_user.id
        command = BUY_COMMAND if selection is ShoppingSelection.BUY else DELETE_COMMAND
        with pending_inputs.lock(address):
            current = self._shopping_sections.current(app_user_id, address, section.revision)
            if current is not section:
                return self._open_shopping_hint()
            choice = None
            if snapshot.visible:
                pending = pending_inputs.begin(
                    address, self.descriptor.capability_id, command, app_user_id=app_user_id,
                    required_mode=self.descriptor.capability_id, value_label="номер покупки", retry_command=command,
                    cancel_return_command=SHOPPING_COMMAND,
                )
                choice = ShoppingChoice(pending.request_id, selection, snapshot.number,
                                        tuple((position, item.number) for position, item in snapshot.visible))
            selection_view = replace(section, choice=choice, add_request_id=None)
            if not self._shopping_sections.replace(app_user_id, address, section, selection_view):
                if choice is not None:
                    pending_inputs.clear(address, choice.request_id)
                return self._open_shopping_hint()
        return shopping_list(
            items, selection_view, update=True, selection=selection, selection_page=snapshot, choice=choice,
            notice=notice, layout=context.action_layout,
        )

    def _show_shopping_list(
        self, context: HandlerContext, address: ReplyAddress, section: ShoppingSection, items: tuple[ShoppingItem, ...],
        page: int = 1, *, update: bool = False,
    ) -> IncomingResponse:
        ordinary = section.ordinary_view(items, page)
        if not self._shopping_sections.replace(context.app_user.id, address, section, ordinary):
            return self._open_shopping_hint()
        return shopping_list(items, ordinary, update=update)

    def _continue_shopping_choice(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent,
    ) -> PendingInputResult:
        revision = context.mode_revision
        app_user_id = context.app_user.id
        address = event.reply_address
        section = None if revision is None else self._shopping_sections.current(app_user_id, address, revision)
        choice = section.choice if section is not None else None
        if section is None or choice is None or choice.request_id != pending.request_id:
            return PendingInputResult(self._open_shopping_hint(), PendingInputOutcome.COMPLETE)
        if not choice.delivered.is_set():
            return PendingInputResult(IncomingResponse(compose_content(
                "Список для выбора недоступен. Открой покупки и выбери действие заново.",
            ), suggested_actions=(ReplyAction("Покупки", SHOPPING_COMMAND), HELP_SECTION_ACTION)), PendingInputOutcome.RETRY)
        retry_actions = (
            ReplyAction("Назад", f"{SHOPPING_COMMAND} back", purpose=ActionPurpose.NAVIGATION,
                        destination=ScreenDestination("shopping-list", section.ordinary_page)),
            HELP_SECTION_ACTION,
        )
        if event.message_id in section.number_message_ids:
            return PendingInputResult(IncomingResponse(text_content(
                "Это сообщение уже обработано. Для следующего выбора отправь новый номер.",
            ), suggested_actions=retry_actions,
                preserve_source_actions=True), PendingInputOutcome.RETRY)
        position = parse_item_number(event.text.strip())
        number = dict(choice.numbers).get(position) if position is not None else None
        if position is None or number is None:
            return PendingInputResult(IncomingResponse(compose_content(
                "Пришли один номер с текущей страницы. Чтобы завершить выбор, нажми «Назад».",
            ), suggested_actions=retry_actions,
                preserve_source_actions=True), PendingInputOutcome.RETRY)
        claimed = section.record_number_message(event.message_id)
        if not self._shopping_sections.replace(app_user_id, address, section, claimed):
            return PendingInputResult(self._open_shopping_hint(), PendingInputOutcome.COMPLETE)
        with self._open_scenarios() as (_, _, shopping):
            change = shopping.buy if choice.selection is ShoppingSelection.BUY else shopping.delete
            try:
                result = change(
                    app_user_id, section.family_id, (number,), actor_joined_at=section.joined_at,
                    expected_position=position,
                )
            except FamilyAccessEnded:
                return PendingInputResult(self._family_access_ended(), PendingInputOutcome.COMPLETE)
        response = self._begin_shopping_choice(
            context, address, claimed, result.items, choice.selection, choice.page,
            notice=shopping_notice(result.changes[0], position),
        )
        return PendingInputResult(response, PendingInputOutcome.COMPLETE)

    def _begin_shopping_add(
        self, context: HandlerContext, address: ReplyAddress, section: ShoppingSection,
    ) -> IncomingResponse:
        if context.pending_inputs is None:
            return IncomingResponse(text_content("Не удалось начать ввод. Попробуй ещё раз."))
        app_user_id = context.app_user.id
        with context.pending_inputs.lock(address):
            current = self._shopping_sections.current(app_user_id, address, section.revision)
            if current is None or current != section:
                return self._family_access_ended()
            pending = context.pending_inputs.begin(
                address, self.descriptor.capability_id, ADD_COMMAND, app_user_id=app_user_id,
                required_mode=self.descriptor.capability_id, value_label="список покупок", retry_command=ADD_COMMAND,
                cancel_only_menu=True, cancel_return_command=SHOPPING_COMMAND,
            )
            self._shopping_sections.replace(
                app_user_id, address, current,
                replace(current, add_request_id=pending.request_id, choice=None),
            )
        prompt = "Напиши, что купить, по одному товару на строку. На ввод 10 минут."
        content = text_content(prompt)
        return IncomingResponse(
            content, suggested_actions=(CANCEL_SECTION_ACTION,),
            view=ResponseView(f"shopping-add:{section.action_key}", on_delivered=section.selection_shown.clear,
                              cleanup_previous=True),
            command_menu_delivery=SectionMenuDelivery.RECONCILE,
        )

    def _continue_shopping_add(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent,
    ) -> PendingInputResult:
        revision = context.mode_revision
        app_user_id = context.app_user.id
        section = None if revision is None else self._shopping_sections.current(app_user_id, event.reply_address, revision)
        if section is None or section.add_request_id != pending.request_id:
            return PendingInputResult(self._family_access_ended(), PendingInputOutcome.COMPLETE)
        with self._open_scenarios() as (_, _, shopping):
            try:
                items = shopping.add(
                    app_user_id, section.family_id, event.text, actor_joined_at=section.joined_at,
                    source=ShoppingAddMessage(event.channel, event.chat_id, event.message_id),
                )
            except FamilyAccessEnded:
                return PendingInputResult(self._family_access_ended(), PendingInputOutcome.COMPLETE)
            except InvalidShoppingNames:
                return self._retry_input("Напиши хотя бы одну покупку.")
            except ItemNumbersExhausted:
                return PendingInputResult(self._shopping_numbers_exhausted(), PendingInputOutcome.COMPLETE)
        return PendingInputResult(self._show_shopping_list(context, event.reply_address, section, items),
                                  PendingInputOutcome.COMPLETE)

    @staticmethod
    def _shopping_numbers_exhausted() -> IncomingResponse:
        return IncomingResponse(text_content("Новые покупки добавить не удалось, номера списка закончились."),
                                suggested_actions=(ReplyAction("Покупки", SHOPPING_COMMAND), HELP_SECTION_ACTION))

    @staticmethod
    def _open_shopping_hint() -> IncomingResponse:
        return IncomingResponse(text_content("Открой список покупок."),
                                suggested_actions=(ReplyAction("Покупки", SHOPPING_COMMAND), HELP_SECTION_ACTION))

    def _members(
        self, context: HandlerContext, event: IncomingEvent, families: ManageFamily,
        family: Family | None, arguments: str,
    ) -> IncomingResponse:
        if family is None:
            return self._family_access_ended()
        parts = arguments.split()
        page = 1
        reference = None
        show_commands = bool(parts and parts[0] == "commands")
        if len(parts) == 2 and parts[0] == "open":
            if parts[1] != str(family.id):
                return self._family_access_ended()
        elif (2 <= len(parts) <= 3 and parts[0] == "page") or (show_commands and len(parts) == 2):
            if len(parts) == 3 and parts[2] != str(family.id):
                return self._family_access_ended()
            number = parts[1]
            if len(number) > 10 or not number.isascii() or not number.isdecimal() or int(number) < 1:
                return IncomingResponse(compose_content(
                    "Открой участников и выбери страницу кнопками.",
                ))
            page = int(number)
        elif len(parts) == 1 and not show_commands:
            reference = parts[0]
        elif len(parts) > 1:
            return IncomingResponse(text_content("Открой список участников."))
        try:
            members = families.list_members(context.app_user.id, family.id)
        except FamilyAccessEnded:
            return self._family_access_ended()
        if reference is None:
            update = bool(parts and parts[0] == "page" and event.callback_message_id is not None)
            return self._members_view.show(
                context, event.reply_address, family, members, page, show_commands=show_commands, update=update,
            )
        return self._members_view.details(context, event.reply_address, family, members, reference)

    @staticmethod
    def _family_access_ended() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Доступ к семье завершён. Открой семью для актуальных действий.",
        ), suggested_actions=(ReplyAction("Семья", "/family"), HELP_SECTION_ACTION))

    @staticmethod
    def _invite(
        context: HandlerContext, invitations: ManageInvitations, family: Family | None, arguments: str,
    ) -> IncomingResponse:
        parts = arguments.split()
        action = parts[0] if parts else "open"
        if action not in ("open", "create") or len(parts) > 2:
            return IncomingResponse(text_content("Открой приглашение, чтобы создать код."))
        # Кнопка привязана к семье, показанной в ответе. Ручная команда открывает текущую.
        if family is None or (len(parts) == 2 and parts[1] != str(family.id)):
            return FamilyCapability._family_access_ended()
        view = ResponseView(
            "family-invite", unavailable=text_content("Открой приглашение заново."),
            cleanup_previous=True, action_scope=ActionScope.SCREEN,
        )
        layout = context.action_layout
        if action == "open":
            title = f"Приглашение в семью «{family.name}»"
            if layout.selection_capacity() < 2 or layout.max_label_length < len("Создать код"):
                return IncomingResponse(text_content(title + "\n\nКнопки недоступны. Открой помощь для ручного управления."),
                                        suggested_actions=(HELP_SECTION_ACTION,))
            return IncomingResponse(text_content(title), suggested_actions=(
                ReplyAction("Создать код", f"{_INVITE} create {family.id}"),
                ReplyAction("Назад", "/family", purpose=ActionPurpose.NAVIGATION,
                            destination=ScreenDestination("family-home")),
                HELP_SECTION_ACTION,
            ), view=view)
        try:
            issued = invitations.issue(context.app_user.id, family.id)
        except FamilyAccessEnded:
            return FamilyCapability._family_access_ended()
        except InvitationUnavailable:
            return IncomingResponse(compose_content(
                "Не удалось создать код. Открой приглашение и попробуй ещё раз.",
            ))
        deadline = issued.expires_at.astimezone(UTC).strftime("%d.%m.%Y, %H:%M UTC")
        parts = (
            f"Приглашение в семью «{issued.family.name}»\n\nКод: ", code_span(issued.code),
            f"\nДействует до {deadline}. По коду можно вступить один раз.",
        )
        if not layout.selection_capacity() or layout.max_label_length < len("Назад"):
            return IncomingResponse(
                compose_content(*parts, "\n\nКнопки недоступны. Открой помощь для ручного управления."),
                suggested_actions=(HELP_SECTION_ACTION,),
            )
        return IncomingResponse(compose_content(*parts), suggested_actions=(
            ReplyAction("Назад", f"{_INVITE} open {issued.family.id}", purpose=ActionPurpose.NAVIGATION,
                        destination=ScreenDestination("family-invite")),
            HELP_SECTION_ACTION,
        ), view=view)
